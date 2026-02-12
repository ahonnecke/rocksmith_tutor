"""PSARC scanning pipeline: parse manifests, extract bass arrangement metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from rocksmith.psarc import PSARC

from .catalog import Catalog, SongEntry, SectionInfo
from .config import DEFAULT_PSARC_DIRS
from .techniques import MANIFEST_TECHNIQUES

log = logging.getLogger(__name__)


def find_psarcs(dirs: list[Path]) -> list[Path]:
    """Collect all .psarc files from the given directories."""
    paths = []
    for d in dirs:
        if d.is_dir():
            paths.extend(sorted(d.glob("*.psarc")))
    return paths


def _extract_bass_manifest(content: dict[str, bytes]) -> tuple[str, dict] | None:
    """Find and parse the first bass manifest JSON from PSARC content.

    Returns (manifest_key, attributes) or None if no bass arrangement.
    """
    for key in content:
        if key.startswith("manifests/") and key.endswith("_bass.json"):
            try:
                manifest = json.loads(content[key])
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            entries = manifest.get("Entries", {})
            for entry_val in entries.values():
                attrs = entry_val.get("Attributes", {})
                if attrs:
                    return key, attrs
    return None


def _attrs_to_song_entry(attrs: dict, psarc_path: Path, mtime: float) -> SongEntry:
    """Build a SongEntry from manifest Attributes."""
    ap = attrs.get("ArrangementProperties", {})
    techniques = {t: bool(ap.get(t, 0)) for t in MANIFEST_TECHNIQUES}

    tuning = attrs.get("Tuning", {})
    standard = bool(ap.get("standardTuning", 0))

    raw_sections = attrs.get("Sections", [])
    sections = [
        SectionInfo(
            name=s["Name"],
            number=s["Number"],
            start_time=s["StartTime"],
            end_time=s["EndTime"],
            is_solo=s.get("IsSolo", False),
        )
        for s in raw_sections
    ]

    dlc_key = attrs.get("DLCKey", "")
    song_id = attrs.get("FullName", f"{dlc_key}_Bass").lower()

    return SongEntry(
        song_id=song_id,
        artist=attrs.get("ArtistName", "Unknown"),
        song_name=attrs.get("SongName", "Unknown"),
        album=attrs.get("AlbumName", ""),
        year=int(attrs.get("SongYear", 0)),
        psarc_path=str(psarc_path),
        psarc_mtime=mtime,
        tempo=float(attrs.get("SongAverageTempo", 0)),
        song_length=float(attrs.get("SongLength", 0)),
        tuning=tuning,
        standard_tuning=standard,
        difficulty_easy=float(attrs.get("SongDiffEasy", 0)),
        difficulty_med=float(attrs.get("SongDiffMed", 0)),
        difficulty_hard=float(attrs.get("SongDiffHard", 0)),
        notes_easy=int(attrs.get("NotesEasy", 0)),
        notes_med=int(attrs.get("NotesMedium", 0)),
        notes_hard=int(attrs.get("NotesHard", 0)),
        max_phrase_difficulty=int(attrs.get("MaxPhraseDifficulty", 0)),
        techniques=techniques,
        sections=sections,
        dlc_key=dlc_key,
    )


def _dedup_key(entry: SongEntry) -> str:
    """Dedup key: normalized (artist, song_name)."""
    return f"{entry.artist.lower()}|{entry.song_name.lower()}"


def scan_psarcs(
    dirs: list[Path] | None = None,
    force: bool = False,
    catalog: Catalog | None = None,
) -> Catalog:
    """Scan PSARC files and build/update the catalog.

    Args:
        dirs: Directories to scan. Defaults to DEFAULT_PSARC_DIRS.
        force: Re-scan all files, ignoring cache.
        catalog: Existing catalog to update. Loads from disk if None.
    """
    dirs = dirs or DEFAULT_PSARC_DIRS
    if catalog is None:
        catalog = Catalog.load()

    psarcs = find_psarcs(dirs)
    if not psarcs:
        log.warning("No .psarc files found in %s", dirs)
        return catalog

    # Build mtime cache for skipping unchanged files
    cached_mtimes: dict[str, float] = {}
    if not force:
        for entry in catalog.songs.values():
            cached_mtimes[entry.psarc_path] = entry.psarc_mtime

    # Track seen dedup keys to prefer _p over _m variants
    seen: dict[str, SongEntry] = {}
    # Keep existing entries that aren't being re-scanned
    for entry in catalog.songs.values():
        seen[_dedup_key(entry)] = entry

    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[filename]}"),
    ) as progress:
        task = progress.add_task("Scanning", total=len(psarcs), filename="")

        for psarc_path in psarcs:
            progress.update(task, filename=psarc_path.name)
            mtime = psarc_path.stat().st_mtime

            # Skip if unchanged and not forced
            if not force and cached_mtimes.get(str(psarc_path)) == mtime:
                progress.advance(task)
                continue

            try:
                with open(psarc_path, "rb") as f:
                    content = PSARC(crypto=True).parse_stream(f)
            except Exception as e:
                log.debug("Failed to parse %s: %s", psarc_path.name, e)
                errors += 1
                progress.advance(task)
                continue

            result = _extract_bass_manifest(content)
            if result is None:
                progress.advance(task)
                continue

            _manifest_key, attrs = result
            entry = _attrs_to_song_entry(attrs, psarc_path, mtime)
            dk = _dedup_key(entry)

            # Prefer _p.psarc over _m.psarc (PC over Mac)
            existing = seen.get(dk)
            if existing is None or psarc_path.name.endswith("_p.psarc"):
                seen[dk] = entry

            progress.advance(task)

    # Rebuild catalog from deduped entries
    catalog.songs = {e.song_id: e for e in seen.values()}
    catalog.update_timestamp()

    if errors:
        log.warning("%d PSARC files failed to parse", errors)

    return catalog
