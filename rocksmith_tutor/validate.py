"""Validate a Rocksmith PSARC for known failure modes.

Checks SNG internal consistency, XML well-formedness, manifest field
structure, cross-layer agreement, and cross-arrangement consistency.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from rocksmith.psarc import PSARC
from rocksmith.sng import Song

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationReport:
    path: Path
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=passed, detail=detail))


def validate_psarc(psarc_path: Path) -> ValidationReport:
    """Run all validation checks on a PSARC file."""
    report = ValidationReport(path=psarc_path)

    # --- 1. PSARC parse ---
    content: dict[str, bytes] | None = None
    try:
        with open(psarc_path, "rb") as f:
            content = PSARC(crypto=True).parse_stream(f)
        report.add("PSARC parse", True, f"{len(content)} entries")
    except Exception as e:
        report.add("PSARC parse", False, str(e))
        return report  # can't continue without parsed content

    # --- 2. Find arrangement SNGs ---
    sng_keys = [
        k for k in content
        if ("songs/bin/generic/" in k or "songs/bin/macos/" in k)
        and k.endswith(".sng")
    ]
    bass_sng_keys = [k for k in sng_keys if "bass" in k.lower()]
    report.add(
        "Bass SNG found",
        len(bass_sng_keys) > 0,
        f"{len(bass_sng_keys)} bass, {len(sng_keys)} total arrangement SNGs",
    )

    # --- 3-4. Parse and validate each SNG ---
    sng_section_counts: dict[str, int] = {}
    sng_pi_counts: dict[str, int] = {}
    sng_phrase_counts: dict[str, int] = {}

    for sng_key in sng_keys:
        short = sng_key.split("/")[-1]
        try:
            sng = Song.parse(content[sng_key])
        except Exception as e:
            report.add(f"SNG parse [{short}]", False, str(e))
            continue
        report.add(f"SNG parse [{short}]", True,
                   f"{len(sng.sections)}s/{len(sng.phraseIterations)}pi/{len(sng.phrases)}p")

        if not sng.sections:
            # Vocals SNG — no sections, skip consistency checks
            continue

        sng_section_counts[sng_key] = len(sng.sections)
        sng_pi_counts[sng_key] = len(sng.phraseIterations)
        sng_phrase_counts[sng_key] = len(sng.phrases)

        # SNG internal consistency
        errors = _check_sng_consistency(sng)
        if errors:
            report.add(f"SNG consistency [{short}]", False, "; ".join(errors))
        else:
            report.add(f"SNG consistency [{short}]", True)

    # --- 5-7. XML checks ---
    xml_keys = [
        k for k in content
        if k.startswith("songs/arr/") and k.endswith(".xml")
    ]
    xml_section_counts: dict[str, int] = {}
    xml_pi_counts: dict[str, int] = {}

    for xml_key in xml_keys:
        short = xml_key.split("/")[-1]
        try:
            xml_text = content[xml_key].decode("utf-8")
        except UnicodeDecodeError as e:
            report.add(f"XML decode [{short}]", False, str(e))
            continue

        # Well-formed check
        try:
            ET.fromstring(xml_text)
            report.add(f"XML well-formed [{short}]", True)
        except ET.ParseError as e:
            report.add(f"XML well-formed [{short}]", False, str(e))
            continue

        # Count sections and PIs in XML
        sec_count = len(re.findall(r"<section\s", xml_text))
        pi_count = len(re.findall(r"<phraseIteration\s", xml_text))
        xml_section_counts[xml_key] = sec_count
        xml_pi_counts[xml_key] = pi_count

    # --- Cross-check XML vs SNG for matching arrangements ---
    for xml_key in xml_keys:
        short_xml = xml_key.split("/")[-1]
        arr_name = _extract_arrangement_name(xml_key)

        # Find matching SNG
        matching_sng = None
        for sk in sng_keys:
            if _extract_arrangement_name(sk) == arr_name:
                matching_sng = sk
                break

        if matching_sng and matching_sng in sng_section_counts and xml_key in xml_section_counts:
            sng_sc = sng_section_counts[matching_sng]
            xml_sc = xml_section_counts[xml_key]
            if sng_sc == xml_sc:
                report.add(f"XML/SNG section count [{short_xml}]", True, f"{xml_sc}")
            else:
                report.add(f"XML/SNG section count [{short_xml}]", False,
                           f"XML={xml_sc} SNG={sng_sc}")

            sng_pc = sng_pi_counts[matching_sng]
            xml_pc = xml_pi_counts[xml_key]
            if sng_pc == xml_pc:
                report.add(f"XML/SNG PI count [{short_xml}]", True, f"{xml_pc}")
            else:
                report.add(f"XML/SNG PI count [{short_xml}]", False,
                           f"XML={xml_pc} SNG={sng_pc}")

    # --- 8-10. Manifest checks ---
    manifest_keys = [
        k for k in content
        if k.startswith("manifests/") and k.endswith(".json")
    ]
    manifest_section_counts: dict[str, int] = {}
    manifest_pi_counts: dict[str, int] = {}
    manifest_phrase_counts: dict[str, int] = {}

    for mkey in manifest_keys:
        short = mkey.split("/")[-1]
        try:
            manifest = json.loads(content[mkey])
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            report.add(f"Manifest parse [{short}]", False, str(e))
            continue

        for entry_val in manifest.get("Entries", {}).values():
            attrs = entry_val.get("Attributes", {})
            if not attrs:
                continue

            sections = attrs.get("Sections", [])
            pis = attrs.get("PhraseIterations", [])
            phrases = attrs.get("Phrases", [])

            if not sections:
                continue  # vocals or aggregate manifest

            manifest_section_counts[mkey] = len(sections)
            manifest_pi_counts[mkey] = len(pis)
            manifest_phrase_counts[mkey] = len(phrases)

            # Check section required fields
            sec_required = {"Name", "UIName", "Number", "StartTime", "EndTime",
                            "StartPhraseIterationIndex", "EndPhraseIterationIndex", "IsSolo"}
            sec_errors = _check_required_fields(sections, sec_required, "section")
            if sec_errors:
                report.add(f"Manifest sections [{short}]", False, "; ".join(sec_errors))
            else:
                report.add(f"Manifest sections [{short}]", True, f"{len(sections)} sections")

            # Check PI required fields
            pi_required = {"PhraseIndex", "MaxDifficulty", "Name", "StartTime", "EndTime"}
            pi_errors = _check_required_fields(pis, pi_required, "phraseIteration")
            if pi_errors:
                report.add(f"Manifest PIs [{short}]", False, "; ".join(pi_errors))
            else:
                report.add(f"Manifest PIs [{short}]", True, f"{len(pis)} PIs")

            # Check phrase required fields
            phrase_required = {"MaxDifficulty", "Name", "IterationCount"}
            phrase_errors = _check_required_fields(phrases, phrase_required, "phrase")
            if phrase_errors:
                report.add(f"Manifest phrases [{short}]", False, "; ".join(phrase_errors))
            else:
                report.add(f"Manifest phrases [{short}]", True, f"{len(phrases)} phrases")

    # --- 11. Cross-layer consistency (SNG vs manifest) ---
    for mkey in manifest_keys:
        short_m = mkey.split("/")[-1]
        arr_name = _extract_arrangement_name(mkey)

        matching_sng = None
        for sk in sng_keys:
            if _extract_arrangement_name(sk) == arr_name:
                matching_sng = sk
                break

        if matching_sng and matching_sng in sng_section_counts and mkey in manifest_section_counts:
            sng_sc = sng_section_counts[matching_sng]
            man_sc = manifest_section_counts[mkey]
            if sng_sc == man_sc:
                report.add(f"SNG/manifest section count [{short_m}]", True, f"{sng_sc}")
            else:
                report.add(f"SNG/manifest section count [{short_m}]", False,
                           f"SNG={sng_sc} manifest={man_sc}")

    # --- 12. Cross-arrangement consistency ---
    if len(sng_section_counts) > 1:
        counts = list(sng_section_counts.values())
        if len(set(counts)) == 1:
            report.add("Cross-arrangement sections", True,
                       f"all {len(sng_section_counts)} arrangements have {counts[0]} sections")
        else:
            detail_parts = [
                f"{k.split('/')[-1]}={v}" for k, v in sng_section_counts.items()
            ]
            report.add("Cross-arrangement sections", False, ", ".join(detail_parts))

    return report


def _check_sng_consistency(sng) -> list[str]:
    """Check SNG internal consistency. Returns list of error descriptions."""
    errors = []
    num_pi = len(sng.phraseIterations)

    # --- Global checks (SNG-wide, not per-level) ---

    # Beat PI indices monotonically non-decreasing
    prev_pi = -1
    for i, beat in enumerate(sng.beats):
        if beat.phraseIteration < prev_pi:
            errors.append(
                f"beat[{i}].phraseIteration={beat.phraseIteration} < prev={prev_pi} "
                "(not monotonic)"
            )
            break
        prev_pi = beat.phraseIteration

    # Section PI refs valid
    for i, sec in enumerate(sng.sections):
        if sec.startPhraseIterationId >= num_pi:
            errors.append(f"section[{i}].startPhraseIterationId={sec.startPhraseIterationId} >= {num_pi}")
        if sec.endPhraseIterationId > num_pi:
            errors.append(f"section[{i}].endPhraseIterationId={sec.endPhraseIterationId} > {num_pi}")

    # --- Per-level checks ---
    for lv_idx, level in enumerate(sng.levels):
        lv_tag = f"lv{lv_idx}"

        # Note phraseIterationId / phraseId validity
        for i, note in enumerate(level.notes):
            if note.phraseIterationId >= num_pi:
                errors.append(f"{lv_tag} note[{i}].phraseIterationId={note.phraseIterationId} >= {num_pi} PIs")
                if len(errors) > 10:
                    errors.append("(truncated)")
                    return errors
            elif note.phraseId != sng.phraseIterations[note.phraseIterationId].phraseId:
                errors.append(
                    f"{lv_tag} note[{i}].phraseId={note.phraseId} != "
                    f"PI[{note.phraseIterationId}].phraseId="
                    f"{sng.phraseIterations[note.phraseIterationId].phraseId}"
                )
                if len(errors) > 10:
                    errors.append("(truncated)")
                    return errors

        # notesInIterCount sums
        if hasattr(level, "notesInIterCount") and level.notesInIterCount:
            iter_sum = sum(level.notesInIterCount)
            total_notes = len(level.notes)
            if iter_sum != total_notes:
                errors.append(
                    f"{lv_tag} notesInIterCount sum={iter_sum} != total notes={total_notes}"
                )

    return errors


def _check_required_fields(
    items: list[dict], required: set[str], item_type: str,
) -> list[str]:
    """Check that all items have the required fields."""
    errors = []
    for i, item in enumerate(items):
        missing = required - set(item.keys())
        if missing:
            errors.append(f"{item_type}[{i}] missing: {', '.join(sorted(missing))}")
            if len(errors) > 3:
                errors.append("(truncated)")
                return errors
    return errors


def _extract_arrangement_name(key: str) -> str:
    """Extract arrangement name from a PSARC key for cross-layer matching.

    E.g. 'songs/bin/macos/songname_bass.sng' -> 'bass'
         'manifests/songs_dlc/songname_bass.json' -> 'bass'
         'songs/arr/songname_bass.xml' -> 'bass'
    """
    basename = key.split("/")[-1].rsplit(".", 1)[0]  # strip extension
    # Common patterns: songname_bass, songname_lead, songname_rhythm
    for suffix in ("_bass", "_lead", "_rhythm", "_vocals", "_combo"):
        if basename.lower().endswith(suffix):
            return suffix.lstrip("_")
    return basename
