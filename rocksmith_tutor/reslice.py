"""Re-segment Rocksmith PSARC files based on note density.

Dense sections get shorter Riff Repeater segments, sparse sections get longer ones.
"""

from __future__ import annotations

import bisect
import json
import logging
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from construct import Container, ListContainer

from rocksmith.crypto import WIN_KEY, decrypt_sng, encrypt_sng
from rocksmith.psarc import PSARC
from rocksmith.sng import Song

log = logging.getLogger(__name__)

SENTINEL = 65535  # uint16 sentinel for first/last iter note links


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DensityPoint:
    time: float
    notes_per_second: float


@dataclass
class SegmentBoundary:
    time: float
    name: str
    number: int


# ---------------------------------------------------------------------------
# PSARC key helpers
# ---------------------------------------------------------------------------

def _find_keys(content: dict[str, bytes], suffix: str) -> list[str]:
    """Find content keys ending with a suffix (case-insensitive on the key)."""
    return [k for k in content if k.lower().endswith(suffix.lower())]


def _find_bass_sng_key(content: dict[str, bytes]) -> str | None:
    for k in content:
        if ("songs/bin/generic/" in k or "songs/bin/macos/" in k) \
                and k.endswith(".sng") and "bass" in k.lower():
            return k
    return None


def _find_bass_xml_key(content: dict[str, bytes]) -> str | None:
    for k in content:
        if "songs/arr/" in k and k.endswith(".xml") and "bass" in k.lower():
            return k
    return None


def _find_bass_manifest_key(content: dict[str, bytes]) -> str | None:
    for k in content:
        if k.startswith("manifests/") and k.endswith("_bass.json"):
            return k
    return None


# ---------------------------------------------------------------------------
# Density analysis
# ---------------------------------------------------------------------------

def compute_density_curve(
    notes: list,
    window_size: float = 2.0,
    step: float = 0.5,
) -> list[DensityPoint]:
    """Sliding-window density: notes/second across the timeline.

    Args:
        notes: SNG note containers (must have .time attribute).
        window_size: Window width in seconds.
        step: Step size in seconds.
    """
    if not notes:
        return []

    times = sorted(n.time for n in notes)
    start = times[0]
    end = times[-1]
    curve = []

    t = start
    while t <= end:
        lo = t
        hi = t + window_size
        count = bisect.bisect_right(times, hi) - bisect.bisect_left(times, lo)
        nps = count / window_size if window_size > 0 else 0
        curve.append(DensityPoint(time=t, notes_per_second=nps))
        t += step

    return curve


# ---------------------------------------------------------------------------
# Boundary computation
# ---------------------------------------------------------------------------

def _snap_to_beat(time: float, beats: list, prefer_measure_start: bool = True) -> float:
    """Snap a time to the nearest ebeat, preferring measure starts (beat==0)."""
    if not beats:
        return time

    beat_times = [b.time for b in beats]
    idx = bisect.bisect_left(beat_times, time)
    candidates = []
    for i in range(max(0, idx - 1), min(len(beats), idx + 2)):
        candidates.append(i)

    if not candidates:
        return time

    # Score: prefer measure starts, then closest
    best_i = candidates[0]
    best_score = float("inf")
    for i in candidates:
        dist = abs(beats[i].time - time)
        # Bonus for measure start (beat == 0)
        penalty = 0 if (prefer_measure_start and beats[i].beat == 0) else 0.05
        score = dist + penalty
        if score < best_score:
            best_score = score
            best_i = i

    return beats[best_i].time


def determine_boundaries(
    density_curve: list[DensityPoint],
    beats: list,
    song_length: float,
    min_segment: float = 3.0,
    max_segment: float = 15.0,
) -> list[SegmentBoundary]:
    """Compute segment boundaries based on density curve.

    Target duration = max_segment - (max_segment - min_segment) * (local_density / max_density)
    Dense regions get shorter segments, sparse get longer.
    """
    if not density_curve:
        return [
            SegmentBoundary(time=0.0, name="COUNT", number=1),
            SegmentBoundary(time=song_length, name="END", number=1),
        ]

    max_density = max(dp.notes_per_second for dp in density_curve)
    if max_density == 0:
        max_density = 1.0  # avoid division by zero

    # Build a lookup: time -> density
    density_at: dict[float, float] = {dp.time: dp.notes_per_second for dp in density_curve}
    density_times = sorted(density_at.keys())

    def local_density(t: float) -> float:
        idx = bisect.bisect_right(density_times, t) - 1
        idx = max(0, min(idx, len(density_times) - 1))
        return density_at[density_times[idx]]

    boundaries = [SegmentBoundary(time=0.0, name="COUNT", number=1)]

    cursor = 0.0
    while cursor < song_length:
        ld = local_density(cursor)
        ratio = ld / max_density
        target = max_segment - (max_segment - min_segment) * ratio
        next_time = cursor + target

        if next_time >= song_length - min_segment:
            break

        snapped = _snap_to_beat(next_time, beats)

        # Enforce minimum spacing from last boundary
        if snapped - cursor < min_segment:
            snapped = cursor + min_segment

        if snapped >= song_length:
            break

        boundaries.append(SegmentBoundary(time=snapped, name="", number=0))
        cursor = snapped

    boundaries.append(SegmentBoundary(time=song_length, name="END", number=1))
    return boundaries


def assign_section_names(
    boundaries: list[SegmentBoundary],
    original_sections: list,
) -> list[SegmentBoundary]:
    """Assign section names to boundaries based on original sections.

    Each boundary inherits the name from the original section it falls within.
    Numbering increments per name occurrence.
    """
    if not original_sections:
        return boundaries

    # Build sorted list of (start_time, name)
    orig = sorted(
        [(s.startTime if hasattr(s, "startTime") else s["startTime"],
          s.name if hasattr(s, "name") else s["name"])
         for s in original_sections],
        key=lambda x: x[0],
    )
    orig_times = [o[0] for o in orig]

    name_counts: dict[str, int] = {}

    for b in boundaries:
        if b.name in ("COUNT", "END"):
            continue

        # Find which original section this boundary falls in
        idx = bisect.bisect_right(orig_times, b.time) - 1
        idx = max(0, min(idx, len(orig) - 1))
        section_name = orig[idx][1]

        name_counts[section_name] = name_counts.get(section_name, 0) + 1
        b.name = section_name
        b.number = name_counts[section_name]

    return boundaries


# ---------------------------------------------------------------------------
# SNG rebuild
# ---------------------------------------------------------------------------

def rebuild_sng(sng: Container, boundaries: list[SegmentBoundary]) -> bytes:
    """Rebuild SNG binary with new phrase segmentation.

    Updates: phrases, phraseIterations, sections, beats, levels[*].notes,
    levels[*].anchors, averageNotesPerIter, notesInIterCount.
    """
    sng = deepcopy(sng)

    # --- 1. Build new phrases ---
    # COUNT at index 0, named interior phrases, END at last index
    phrase_names: list[str] = []
    phrase_names.append("COUNT")
    for b in boundaries:
        if b.name not in ("COUNT", "END"):
            phrase_names.append(b.name)
    phrase_names.append("END")

    # Deduplicate phrase definitions — map name -> phraseId
    unique_phrases: dict[str, int] = {}
    new_phrases = ListContainer()

    for name in phrase_names:
        if name not in unique_phrases:
            unique_phrases[name] = len(new_phrases)
            # Find maxDifficulty from original phrases with same name
            max_diff = 0
            for orig_p in sng.phrases:
                if orig_p.name == name:
                    max_diff = max(max_diff, orig_p.maxDifficulty)
            if max_diff == 0 and name not in ("COUNT", "END"):
                # Use the global max difficulty
                max_diff = max(p.maxDifficulty for p in sng.phrases) if sng.phrases else 0

            new_phrases.append(Container(
                solo=0,
                disparity=0,
                ignore=1 if name in ("COUNT", "END") else 0,
                maxDifficulty=max_diff,
                phraseIterationLinks=0,  # recomputed below
                name=name,
            ))

    # --- 2. Build new phraseIterations ---
    new_pi = ListContainer()
    for i, b in enumerate(boundaries):
        phrase_id = unique_phrases[b.name if b.name in unique_phrases else
                                   ("COUNT" if i == 0 else "END")]
        end_time = boundaries[i + 1].time if i + 1 < len(boundaries) else b.time
        max_diff = new_phrases[phrase_id].maxDifficulty
        new_pi.append(Container(
            phraseId=phrase_id,
            time=b.time,
            endTime=end_time,
            difficulty=ListContainer([max_diff, max_diff, max_diff]),
        ))

    # Update phraseIterationLinks on phrases
    for p in new_phrases:
        p.phraseIterationLinks = 0
    for pi in new_pi:
        new_phrases[pi.phraseId].phraseIterationLinks += 1

    # --- 3. Build new sections (1:1 with PIs, excluding COUNT PI at index 0) ---
    new_sections = ListContainer()
    section_name_counts: dict[str, int] = {}
    for pi_idx in range(1, len(new_pi)):
        pi = new_pi[pi_idx]
        name = new_phrases[pi.phraseId].name
        if name == "END":
            continue
        section_name_counts[name] = section_name_counts.get(name, 0) + 1

        # Find the end PI for this section (next section start, or last PI)
        end_pi_idx = pi_idx + 1 if pi_idx + 1 < len(new_pi) else pi_idx

        new_sections.append(Container(
            name=name,
            number=section_name_counts[name],
            startTime=pi.time,
            endTime=pi.endTime,
            startPhraseIterationId=pi_idx,
            endPhraseIterationId=end_pi_idx,
            stringMask=ListContainer([0] * 36),
        ))

    # Copy stringMask from original sections if possible
    if sng.sections and new_sections:
        default_mask = sng.sections[0].stringMask
        for s in new_sections:
            s.stringMask = ListContainer(list(default_mask))

    # --- 4. Update beats -> phraseIteration index ---
    pi_times = [pi.time for pi in new_pi]
    for beat in sng.beats:
        idx = bisect.bisect_right(pi_times, beat.time) - 1
        beat.phraseIteration = max(0, min(idx, len(new_pi) - 1))

    # --- 5-8. Update levels ---
    num_pi = len(new_pi)
    num_phrases = len(new_phrases)

    for level in sng.levels:
        # --- 5. Notes: update phraseId, phraseIterationId, rechain nextIterNote/prevIterNote ---
        for note in level.notes:
            pi_idx = bisect.bisect_right(pi_times, note.time) - 1
            pi_idx = max(0, min(pi_idx, num_pi - 1))
            note.phraseIterationId = pi_idx
            note.phraseId = new_pi[pi_idx].phraseId

        # Rechain nextIterNote / prevIterNote per PI
        pi_note_groups: dict[int, list[int]] = {}
        for note_idx, note in enumerate(level.notes):
            pi_idx = note.phraseIterationId
            pi_note_groups.setdefault(pi_idx, []).append(note_idx)

        for pi_idx, note_indices in pi_note_groups.items():
            for i, ni in enumerate(note_indices):
                level.notes[ni].prevIterNote = (
                    note_indices[i - 1] if i > 0 else SENTINEL
                )
                level.notes[ni].nextIterNote = (
                    note_indices[i + 1] if i < len(note_indices) - 1 else SENTINEL
                )

        # --- 6. Anchors: update phraseIterationId ---
        for anchor in level.anchors:
            pi_idx = bisect.bisect_right(pi_times, anchor.time) - 1
            anchor.phraseIterationId = max(0, min(pi_idx, num_pi - 1))

        # --- 7. averageNotesPerIter (per phraseId) ---
        phrase_note_counts: dict[int, list[int]] = {}
        for pi_idx, note_indices in pi_note_groups.items():
            phrase_id = new_pi[pi_idx].phraseId
            phrase_note_counts.setdefault(phrase_id, []).append(len(note_indices))

        avg = ListContainer()
        for pid in range(num_phrases):
            counts = phrase_note_counts.get(pid, [0])
            avg.append(sum(counts) / len(counts) if counts else 0.0)
        level.averageNotesPerIter = avg

        # --- 8. notesInIterCount / notesInIterCountNoIgnored (per PI index) ---
        counts = ListContainer()
        counts_no_ignored = ListContainer()
        for pi_idx in range(num_pi):
            n = len(pi_note_groups.get(pi_idx, []))
            counts.append(n)
            counts_no_ignored.append(n)
        level.notesInIterCount = counts
        level.notesInIterCountNoIgnored = counts_no_ignored

    # Apply new structures
    sng.phrases = new_phrases
    sng.phraseIterations = new_pi
    sng.sections = new_sections

    # Clear phraseExtraInfos (optional metadata, safe to empty)
    sng.phraseExtraInfos = ListContainer()

    return Song.build(sng)


# ---------------------------------------------------------------------------
# XML rebuild
# ---------------------------------------------------------------------------

def rebuild_xml(xml_bytes: bytes, boundaries: list[SegmentBoundary]) -> bytes:
    """Rebuild arrangement XML with new phrases, phraseIterations, sections."""
    root = ET.fromstring(xml_bytes)

    # --- Phrases ---
    unique_phrases: dict[str, int] = {}
    phrase_list: list[tuple[str, int]] = []  # (name, maxDifficulty)

    # Collect original maxDifficulty per name
    orig_max_diff: dict[str, int] = {}
    phrases_el = root.find("phrases")
    if phrases_el is not None:
        for p in phrases_el.findall("phrase"):
            name = p.get("name", "")
            md = int(p.get("maxDifficulty", "0"))
            orig_max_diff[name] = max(orig_max_diff.get(name, 0), md)

    global_max = max(orig_max_diff.values()) if orig_max_diff else 0

    names_in_order = ["COUNT"]
    for b in boundaries:
        if b.name not in ("COUNT", "END"):
            names_in_order.append(b.name)
    names_in_order.append("END")

    for name in names_in_order:
        if name not in unique_phrases:
            unique_phrases[name] = len(phrase_list)
            md = orig_max_diff.get(name, global_max if name not in ("COUNT", "END") else 0)
            phrase_list.append((name, md))

    # Replace <phrases>
    if phrases_el is not None:
        root.remove(phrases_el)
    new_phrases_el = ET.SubElement(root, "phrases")
    new_phrases_el.set("count", str(len(phrase_list)))
    for name, md in phrase_list:
        p = ET.SubElement(new_phrases_el, "phrase")
        p.set("name", name)
        p.set("maxDifficulty", str(md))
        p.set("disparity", "0")
        p.set("ignore", "1" if name in ("COUNT", "END") else "0")
        p.set("solo", "0")

    # --- PhraseIterations ---
    pi_el = root.find("phraseIterations")
    if pi_el is not None:
        root.remove(pi_el)
    new_pi_el = ET.SubElement(root, "phraseIterations")
    new_pi_el.set("count", str(len(boundaries)))
    for i, b in enumerate(boundaries):
        phrase_id = unique_phrases.get(b.name, 0)
        pi = ET.SubElement(new_pi_el, "phraseIteration")
        pi.set("time", f"{b.time:.3f}")
        pi.set("phraseId", str(phrase_id))

    # --- Sections ---
    sec_el = root.find("sections")
    if sec_el is not None:
        root.remove(sec_el)
    new_sec_el = ET.SubElement(root, "sections")
    section_entries = []
    for i, b in enumerate(boundaries):
        if b.name in ("COUNT", "END"):
            continue
        end_time = boundaries[i + 1].time if i + 1 < len(boundaries) else b.time
        section_entries.append((b.name, b.number, b.time, end_time))

    new_sec_el.set("count", str(len(section_entries)))
    for name, number, start, end in section_entries:
        s = ET.SubElement(new_sec_el, "section")
        s.set("name", name)
        s.set("number", str(number))
        s.set("startTime", f"{start:.3f}")

    return ET.tostring(root, encoding="unicode").encode("utf-8")


# ---------------------------------------------------------------------------
# Manifest rebuild
# ---------------------------------------------------------------------------

def rebuild_manifest(
    manifest_bytes: bytes,
    boundaries: list[SegmentBoundary],
    new_phrases: list[dict],
) -> bytes:
    """Update manifest JSON with new Sections, Phrases, PhraseIterations."""
    manifest = json.loads(manifest_bytes)

    for entry_val in manifest.get("Entries", {}).values():
        attrs = entry_val.get("Attributes", {})
        if not attrs:
            continue

        # Sections
        new_sections = []
        section_name_counts: dict[str, int] = {}
        for i, b in enumerate(boundaries):
            if b.name in ("COUNT", "END"):
                continue
            end_time = boundaries[i + 1].time if i + 1 < len(boundaries) else b.time
            section_name_counts[b.name] = section_name_counts.get(b.name, 0) + 1
            new_sections.append({
                "Name": b.name,
                "Number": section_name_counts[b.name],
                "StartTime": round(b.time, 3),
                "EndTime": round(end_time, 3),
                "IsSolo": False,
            })
        attrs["Sections"] = new_sections

        # Phrases
        attrs["Phrases"] = new_phrases

        # PhraseIterations
        unique_names: dict[str, int] = {}
        phrase_id_map: dict[str, int] = {}
        for p in new_phrases:
            if p["Name"] not in phrase_id_map:
                phrase_id_map[p["Name"]] = len(phrase_id_map)

        pi_list = []
        for i, b in enumerate(boundaries):
            end_time = boundaries[i + 1].time if i + 1 < len(boundaries) else b.time
            pid = phrase_id_map.get(b.name, 0)
            pi_list.append({
                "PhraseId": pid,
                "StartTime": round(b.time, 3),
                "EndTime": round(end_time, 3),
            })
        attrs["PhraseIterations"] = pi_list

    return json.dumps(manifest, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def reslice_psarc(
    psarc_path: Path,
    output_path: Path,
    min_segment: float = 3.0,
    max_segment: float = 15.0,
    window: float = 2.0,
    dry_run: bool = False,
) -> list[SegmentBoundary]:
    """Re-segment a PSARC file based on note density.

    Returns the computed boundaries (useful for dry-run display).
    """
    # Parse PSARC (crypto=True handles SNG decryption)
    with open(psarc_path, "rb") as f:
        content = PSARC(crypto=True).parse_stream(f)

    # Find bass SNG
    sng_key = _find_bass_sng_key(content)
    if sng_key is None:
        raise ValueError("No bass SNG found in PSARC")

    sng_data = content[sng_key]
    sng = Song.parse(sng_data)
    log.info("Parsed SNG: %d phrases, %d phraseIterations, %d sections",
             len(sng.phrases), len(sng.phraseIterations), len(sng.sections))

    # Get notes from highest difficulty level
    if not sng.levels:
        raise ValueError("No levels found in SNG")
    max_diff = max(lv.difficulty for lv in sng.levels)
    top_level = next(lv for lv in sng.levels if lv.difficulty == max_diff)
    notes = list(top_level.notes)
    log.info("Top level (%d): %d notes", max_diff, len(notes))

    # Compute density curve
    curve = compute_density_curve(notes, window_size=window, step=0.5)
    log.info("Density curve: %d points, max=%.1f n/s",
             len(curve),
             max(dp.notes_per_second for dp in curve) if curve else 0)

    # Determine boundaries
    song_length = sng.metadata.songLength
    boundaries = determine_boundaries(
        curve, list(sng.beats), song_length,
        min_segment=min_segment, max_segment=max_segment,
    )

    # Assign section names from original sections
    boundaries = assign_section_names(boundaries, list(sng.sections))

    log.info("New boundaries: %d segments", len(boundaries) - 1)

    if dry_run:
        return boundaries

    # Rebuild SNG
    new_sng_bytes = rebuild_sng(sng, boundaries)
    # Re-encrypt for PSARC (encrypt expects raw SNG, content dict has decrypted)
    content[sng_key] = new_sng_bytes

    # Rebuild XML if present
    xml_key = _find_bass_xml_key(content)
    if xml_key is not None:
        content[xml_key] = rebuild_xml(content[xml_key], boundaries)
        log.info("Rebuilt XML: %s", xml_key)

    # Rebuild manifest if present
    manifest_key = _find_bass_manifest_key(content)
    if manifest_key is not None:
        # Build phrase dicts for manifest
        phrase_names_seen: dict[str, int] = {}
        manifest_phrases = []
        for b in boundaries:
            if b.name not in phrase_names_seen:
                phrase_names_seen[b.name] = len(manifest_phrases)
                manifest_phrases.append({
                    "Name": b.name,
                    "MaxDifficulty": 0,
                    "Disparity": 0,
                    "Ignore": b.name in ("COUNT", "END"),
                    "Solo": False,
                })
        content[manifest_key] = rebuild_manifest(
            content[manifest_key], boundaries, manifest_phrases,
        )
        log.info("Rebuilt manifest: %s", manifest_key)

    # Write output PSARC
    with open(output_path, "wb") as f:
        PSARC(crypto=True).build_stream(content, f)

    log.info("Wrote: %s", output_path)
    return boundaries
