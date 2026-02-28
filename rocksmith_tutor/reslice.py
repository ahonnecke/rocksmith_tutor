"""Re-segment Rocksmith PSARC files based on note-gap analysis.

Boundaries land in the largest gaps between notes — musically natural break
points rather than arbitrary density-window positions.
"""

from __future__ import annotations

import bisect
import json
import logging
import statistics
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from construct import Container, ListContainer

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


@dataclass
class NoteGap:
    """A gap between two consecutive notes, scored as a boundary candidate."""
    index: int          # index of note *before* the gap
    start: float        # gap_start = notes[i].time + notes[i].sustain
    end: float          # gap_end   = notes[i+1].time
    duration: float     # end - start (negative when sustain bleeds over)
    midpoint: float     # (start + end) / 2
    score: float = 0.0
    snap_target: float = 0.0


# ---------------------------------------------------------------------------
# Note-gap analysis
# ---------------------------------------------------------------------------

def _compute_note_gaps(notes: list) -> list[NoteGap]:
    """Extract sustain-aware gaps between consecutive notes."""
    gaps: list[NoteGap] = []
    for i in range(len(notes) - 1):
        sustain = getattr(notes[i], "sustain", 0.0) or 0.0
        gap_start = notes[i].time + sustain
        gap_end = notes[i + 1].time
        duration = gap_end - gap_start
        gaps.append(NoteGap(
            index=i,
            start=gap_start,
            end=gap_end,
            duration=duration,
            midpoint=(gap_start + gap_end) / 2,
        ))
    return gaps


def _score_gaps(gaps: list[NoteGap], beats: list) -> None:
    """Score each gap using local-median ratio + beat-alignment bonus.

    Mutates gaps in place, setting .score and .snap_target.
    """
    if not gaps:
        return

    # Pre-compute measure-start times for beat bonus
    measure_times = sorted(b.time for b in beats if b.beat == 0)

    half_window = 10  # ~20 surrounding gaps

    for i, gap in enumerate(gaps):
        if gap.duration <= 0:
            gap.score = 0.0
            continue

        # Local median of surrounding gap durations (only positive ones)
        lo = max(0, i - half_window)
        hi = min(len(gaps), i + half_window + 1)
        neighbors = [g.duration for g in gaps[lo:hi] if g.duration > 0]
        local_med = statistics.median(neighbors) if neighbors else gap.duration

        # Avoid division by zero
        if local_med <= 0:
            local_med = gap.duration

        gap.score = gap.duration * (gap.duration / local_med)

        # Beat alignment bonus: measure start within the gap → 1.5x
        idx = bisect.bisect_left(measure_times, gap.start)
        for j in range(max(0, idx - 1), min(len(measure_times), idx + 2)):
            if gap.start <= measure_times[j] <= gap.end:
                gap.score *= 1.5
                break

        # Snap midpoint to nearest beat
        gap.snap_target = _snap_to_beat(gap.midpoint, beats)


def _best_gap_in_range(
    gaps: list[NoteGap],
    lo: float,
    hi: float,
) -> NoteGap | None:
    """Return the highest-scored gap whose midpoint falls in [lo, hi]."""
    best: NoteGap | None = None
    for g in gaps:
        if g.duration <= 0:
            continue
        if lo <= g.midpoint <= hi and (best is None or g.score > best.score):
            best = g
    return best


# ---------------------------------------------------------------------------
# PSARC key helpers
# ---------------------------------------------------------------------------

def _find_bass_sng_key(content: dict[str, bytes]) -> str | None:
    """Find the bass SNG key, checking both Mac and PC paths.

    Matches on '_bass.sng' suffix to avoid false positives when 'bass'
    appears in the song name (e.g. 'allaboutthatbass_vocals.sng').
    """
    for k in content:
        if ("songs/bin/generic/" in k or "songs/bin/macos/" in k) \
                and k.lower().endswith("_bass.sng"):
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
    notes: list,
    beats: list,
    song_length: float,
    min_segment: float = 3.0,
    max_segment: float = 15.0,
    manual_splits: list[float] | None = None,
) -> list[SegmentBoundary]:
    """Compute segment boundaries by scoring inter-note gaps.

    Boundaries land in the largest gaps between notes — musically natural
    break points.  A greedy algorithm selects the highest-scored gaps
    subject to min/max segment duration constraints.

    manual_splits: additional timestamps forced as boundaries, merged
    with gap-selected boundaries (duplicates within 1s are deduplicated).
    """
    if not notes:
        return [
            SegmentBoundary(time=0.0, name="COUNT", number=1),
            SegmentBoundary(time=song_length, name="END", number=1),
        ]

    # --- Step 1-2: compute and score gaps ---
    gaps = _compute_note_gaps(notes)
    _score_gaps(gaps, beats)

    # --- Step 3: iteratively split the longest span using highest-scored gap ---
    selected_times: list[float] = []

    def _all_bounds() -> list[float]:
        return sorted([0.0] + selected_times + [song_length])

    def _respects_min(t: float) -> bool:
        for et in _all_bounds():
            if abs(t - et) < min_segment:
                return False
        return True

    # Keep splitting while any span exceeds max_segment
    for _ in range(200):  # safety bound
        bounds = _all_bounds()
        # Find the longest span
        worst_span = 0.0
        worst_lo = 0.0
        worst_hi = 0.0
        for i in range(len(bounds) - 1):
            span = bounds[i + 1] - bounds[i]
            if span > worst_span:
                worst_span = span
                worst_lo = bounds[i]
                worst_hi = bounds[i + 1]

        if worst_span <= max_segment:
            break

        best = _best_gap_in_range(gaps, worst_lo, worst_hi)
        if best is not None and _respects_min(best.snap_target):
            selected_times.append(best.snap_target)
        else:
            # No scored gap — insert midpoint snapped to beat
            mid = (worst_lo + worst_hi) / 2
            t = _snap_to_beat(mid, beats)
            if _respects_min(t):
                selected_times.append(t)
            else:
                # Force it in even if min_segment is tight
                selected_times.append(t)
                break  # avoid infinite loop

    # --- Merge manual splits ---
    if manual_splits:
        for t in manual_splits:
            if t <= 0 or t >= song_length:
                continue
            too_close = any(abs(t - et) < 1.0 for et in selected_times)
            if not too_close:
                selected_times.append(t)

    # --- Build boundary list ---
    selected_times = sorted(set(selected_times))
    boundaries = [SegmentBoundary(time=0.0, name="COUNT", number=1)]
    for t in selected_times:
        boundaries.append(SegmentBoundary(time=t, name="", number=0))
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
    # COUNT at index 0, named interior phrases.  No separate END phrase —
    # the original Rocksmith format uses the last section's PI as the song
    # end marker, not a dedicated END PI.
    phrase_names: list[str] = []
    phrase_names.append("COUNT")
    for b in boundaries:
        if b.name not in ("COUNT", "END"):
            phrase_names.append(b.name)

    # Build a lookup: for any time range, find the original PI whose time
    # range overlaps the most, and inherit its phrase's maxDifficulty.
    # This preserves DD ladders even when original phrase names (p0, p1, ...)
    # don't match section names (intro, verse, ...).
    orig_pi_times = [(pi.time, pi.endTime, sng.phrases[pi.phraseId].maxDifficulty)
                     for pi in sng.phraseIterations]

    def _max_diff_for_range(t_start: float, t_end: float) -> int:
        """Find max of maxDifficulty across all original PIs that overlap this range.

        Must be the max (not dominant) because notes from a high-maxDiff
        original PI exist at levels above a low-maxDiff one.  If the new PI
        straddles both, the phrase ceiling must accommodate the higher.
        """
        result = 0
        for ot_start, ot_end, md in orig_pi_times:
            overlap = max(0.0, min(t_end, ot_end) - max(t_start, ot_start))
            if overlap > 0:
                result = max(result, md)
        return result

    # Deduplicate phrase definitions — map name -> phraseId
    unique_phrases: dict[str, int] = {}
    new_phrases = ListContainer()

    # First pass: collect per-name max difficulty from boundary time ranges
    non_end = [b for b in boundaries if b.name != "END"]
    sl = sng.metadata.songLength
    name_max_diff: dict[str, int] = {}
    for i, b in enumerate(non_end):
        if b.name in ("COUNT", "END"):
            continue
        end_time = non_end[i + 1].time if i + 1 < len(non_end) else sl
        md = _max_diff_for_range(b.time, end_time)
        name_max_diff[b.name] = max(name_max_diff.get(b.name, 0), md)

    for name in phrase_names:
        if name not in unique_phrases:
            unique_phrases[name] = len(new_phrases)
            max_diff = name_max_diff.get(name, 0)

            new_phrases.append(Container(
                solo=0,
                disparity=0,
                ignore=1 if name in ("COUNT", "END") else 0,
                maxDifficulty=max_diff,
                phraseIterationLinks=0,  # recomputed below
                name=name,
            ))

    # --- 2. Build new phraseIterations (skip END boundary) ---
    new_pi = ListContainer()
    for i, b in enumerate(non_end):
        phrase_id = unique_phrases.get(b.name, 0)
        end_time = non_end[i + 1].time if i + 1 < len(non_end) else sl
        # Use per-PI maxDifficulty from the overlapping original PI,
        # capped at the phrase's maxDifficulty.
        pi_max_diff = _max_diff_for_range(b.time, end_time)
        pi_max_diff = min(pi_max_diff, new_phrases[phrase_id].maxDifficulty)
        new_pi.append(Container(
            phraseId=phrase_id,
            time=b.time,
            endTime=end_time,
            difficulty=ListContainer([pi_max_diff, pi_max_diff, pi_max_diff]),
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

        new_sections.append(Container(
            name=name,
            number=section_name_counts[name],
            startTime=pi.time,
            endTime=pi.endTime,
            startPhraseIterationId=pi_idx,
            endPhraseIterationId=pi_idx,  # inclusive, same as original format
            stringMask=ListContainer([0] * 36),
        ))

    # Copy stringMask from original sections if possible
    if sng.sections and new_sections:
        default_mask = sng.sections[0].stringMask
        for s in new_sections:
            s.stringMask = ListContainer(list(default_mask))

    # --- 4. Update beats -> phraseIteration index ---
    # Rocksmith convention: beats on PI boundaries belong to the previous PI
    pi_times = [pi.time for pi in new_pi]
    for beat in sng.beats:
        idx = bisect.bisect_left(pi_times, beat.time) - 1
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

    # Clear phraseExtraInfos (optional DD metadata, safe to empty)
    sng.phraseExtraInfos = ListContainer()

    # Clear newLinkedDiffs (phrase indices are remapped, stale refs break DD)
    sng.newLinkedDiffs = ListContainer()

    return Song.build(sng)


# ---------------------------------------------------------------------------
# SNG flatten (strip DD, single max-difficulty level)
# ---------------------------------------------------------------------------

def flatten_sng(sng: Container) -> bytes:
    """Flatten an SNG to a single max-difficulty level.

    Picks the level with the most notes, drops all others, sets every
    phrase maxDifficulty=0, and recomputes all derived fields.  The result
    plays at 100% difficulty with no DD ramp-up.
    """
    sng = deepcopy(sng)

    if not sng.levels:
        return Song.build(sng)

    # Pick the level with the most notes (highest difficulty, full note set)
    best = max(sng.levels, key=lambda l: len(l.notes))
    best.difficulty = 0

    # Replace levels with just the one
    sng.levels = ListContainer([best])

    # Set all phrases to maxDifficulty=0
    for p in sng.phrases:
        p.maxDifficulty = 0

    # Set all PI difficulty arrays to [0, 0, 0]
    for pi in sng.phraseIterations:
        pi.difficulty = ListContainer([0, 0, 0])

    # Clear DD metadata (stale phrase indices after flattening)
    sng.newLinkedDiffs = ListContainer()
    sng.phraseExtraInfos = ListContainer()

    # Recompute phrase.phraseIterationLinks
    for p in sng.phrases:
        p.phraseIterationLinks = 0
    for pi in sng.phraseIterations:
        sng.phrases[pi.phraseId].phraseIterationLinks += 1

    # Recompute beat PI indices.
    # Rocksmith convention: beats on PI boundaries belong to the previous PI.
    pi_times = [pi.time for pi in sng.phraseIterations]
    num_pi = len(sng.phraseIterations)
    num_phrases = len(sng.phrases)

    for beat in sng.beats:
        idx = bisect.bisect_left(pi_times, beat.time) - 1
        beat.phraseIteration = max(0, min(idx, num_pi - 1))

    # Recompute per-level derived fields (single level)
    level = sng.levels[0]

    for note in level.notes:
        pi_idx = bisect.bisect_right(pi_times, note.time) - 1
        pi_idx = max(0, min(pi_idx, num_pi - 1))
        note.phraseIterationId = pi_idx
        note.phraseId = sng.phraseIterations[pi_idx].phraseId

    pi_note_groups: dict[int, list[int]] = {}
    for note_idx, note in enumerate(level.notes):
        pi_note_groups.setdefault(note.phraseIterationId, []).append(note_idx)

    for group_indices in pi_note_groups.values():
        for i, ni in enumerate(group_indices):
            level.notes[ni].prevIterNote = (
                group_indices[i - 1] if i > 0 else SENTINEL
            )
            level.notes[ni].nextIterNote = (
                group_indices[i + 1] if i < len(group_indices) - 1 else SENTINEL
            )

    for anchor in level.anchors:
        pi_idx = bisect.bisect_right(pi_times, anchor.time) - 1
        anchor.phraseIterationId = max(0, min(pi_idx, num_pi - 1))

    # averageNotesPerIter (per phraseId)
    phrase_note_counts: dict[int, list[int]] = {}
    for pi_idx, note_indices in pi_note_groups.items():
        phrase_id = sng.phraseIterations[pi_idx].phraseId
        phrase_note_counts.setdefault(phrase_id, []).append(len(note_indices))

    avg = ListContainer()
    for pid in range(num_phrases):
        counts = phrase_note_counts.get(pid, [0])
        avg.append(sum(counts) / len(counts) if counts else 0.0)
    level.averageNotesPerIter = avg

    # notesInIterCount / notesInIterCountNoIgnored (per PI index)
    iter_counts = ListContainer()
    iter_counts_no_ignored = ListContainer()
    for pi_idx in range(num_pi):
        n = len(pi_note_groups.get(pi_idx, []))
        iter_counts.append(n)
        iter_counts_no_ignored.append(n)
    level.notesInIterCount = iter_counts
    level.notesInIterCountNoIgnored = iter_counts_no_ignored

    return Song.build(sng)


# ---------------------------------------------------------------------------
# XML rebuild
# ---------------------------------------------------------------------------

def rebuild_xml(xml_bytes: bytes, boundaries: list[SegmentBoundary]) -> bytes:
    """Rebuild arrangement XML with new phrases, phraseIterations, sections.

    Uses regex substitution to preserve original XML formatting — ElementTree
    mangles attribute order and encoding declarations, which Rocksmith rejects.
    """
    import re
    text = xml_bytes.decode("utf-8")

    # --- Build unique phrases ---
    unique_phrases: dict[str, int] = {}
    phrase_names_in_order: list[str] = []

    # Extract original maxDifficulty per phrase name
    orig_max_diff: dict[str, int] = {}
    for m in re.finditer(r'<phrase\s[^>]*name="([^"]*)"[^>]*maxDifficulty="(\d+)"', text):
        name, md = m.group(1), int(m.group(2))
        orig_max_diff[name] = max(orig_max_diff.get(name, 0), md)
    # Also try reversed attribute order
    for m in re.finditer(r'<phrase\s[^>]*maxDifficulty="(\d+)"[^>]*name="([^"]*)"', text):
        md, name = int(m.group(1)), m.group(2)
        orig_max_diff[name] = max(orig_max_diff.get(name, 0), md)

    all_names = ["COUNT"]
    for b in boundaries:
        if b.name not in ("COUNT", "END"):
            all_names.append(b.name)

    for name in all_names:
        if name not in unique_phrases:
            unique_phrases[name] = len(phrase_names_in_order)
            phrase_names_in_order.append(name)

    # --- Generate <phrases> block ---
    phrase_lines = [f'  <phrases count="{len(phrase_names_in_order)}">']
    for name in phrase_names_in_order:
        md = orig_max_diff.get(name, 0)
        phrase_lines.append(
            f'    <phrase disparity="0" ignore="0" maxDifficulty="{md}" '
            f'name="{name}" solo="0"/>'
        )
    phrase_lines.append("  </phrases>")
    new_phrases_block = "\n".join(phrase_lines)

    # --- Generate <phraseIterations> block (skip END) ---
    non_end = [b for b in boundaries if b.name != "END"]
    pi_lines = [f'  <phraseIterations count="{len(non_end)}">']
    for b in non_end:
        pid = unique_phrases[b.name]
        pi_lines.append(f'    <phraseIteration time="{b.time:.3f}" phraseId="{pid}"/>')
    pi_lines.append("  </phraseIterations>")
    new_pi_block = "\n".join(pi_lines)

    # --- Generate <sections> block ---
    section_entries = []
    for b in boundaries:
        if b.name in ("COUNT", "END"):
            continue
        section_entries.append((b.name, b.number, b.time))
    sec_lines = [f'  <sections count="{len(section_entries)}">']
    for name, number, start in section_entries:
        sec_lines.append(f'    <section name="{name}" number="{number}" startTime="{start:.3f}"/>')
    sec_lines.append("  </sections>")
    new_sec_block = "\n".join(sec_lines)

    # --- Detect line ending style ---
    newline = "\r\n" if "\r\n" in text else "\n"
    new_phrases_block = new_phrases_block.replace("\n", newline)
    new_pi_block = new_pi_block.replace("\n", newline)
    new_sec_block = new_sec_block.replace("\n", newline)

    # --- Substitute blocks via regex ---
    text = re.sub(
        r'  <phrases count="\d+">[\r\n].*?  </phrases>',
        new_phrases_block, text, flags=re.DOTALL,
    )
    text = re.sub(
        r'  <phraseIterations count="\d+">[\r\n].*?  </phraseIterations>',
        new_pi_block, text, flags=re.DOTALL,
    )
    text = re.sub(
        r'  <sections count="\d+">[\r\n].*?  </sections>',
        new_sec_block, text, flags=re.DOTALL,
    )

    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Manifest rebuild
# ---------------------------------------------------------------------------

def _ui_name(section_name: str, number: int) -> str:
    """Generate Rocksmith UIName string for a section."""
    # Rocksmith uses localization tokens like "$[34286] Intro [1]"
    # but CDLC typically just uses the display name directly
    return f"$[0] {section_name.capitalize()} [{number}]"


def rebuild_manifest(
    manifest_bytes: bytes,
    boundaries: list[SegmentBoundary],
) -> bytes:
    """Update manifest JSON with new Sections, Phrases, PhraseIterations.

    Matches exact field structure Rocksmith expects.
    """
    manifest = json.loads(manifest_bytes)

    # Build unique phrase list with iteration counts (skip END)
    non_end = [b for b in boundaries if b.name != "END"]
    unique_phrases: dict[str, int] = {}  # name -> index
    phrase_list: list[dict] = []
    phrase_iter_counts: dict[str, int] = {}

    for b in non_end:
        phrase_iter_counts[b.name] = phrase_iter_counts.get(b.name, 0) + 1
        if b.name not in unique_phrases:
            unique_phrases[b.name] = len(phrase_list)
            phrase_list.append({"name": b.name, "index": len(phrase_list)})

    for entry_val in manifest.get("Entries", {}).values():
        attrs = entry_val.get("Attributes", {})
        if not attrs:
            continue

        # Get original max difficulties per phrase name
        orig_max_diff: dict[str, int] = {}
        for p in attrs.get("Phrases", []):
            name = p.get("Name", "")
            md = p.get("MaxDifficulty", 0)
            orig_max_diff[name] = max(orig_max_diff.get(name, 0), md)
        global_max = max(orig_max_diff.values()) if orig_max_diff else 0

        # Phrases: {MaxDifficulty, Name, IterationCount}
        new_phrases = []
        for pl in phrase_list:
            name = pl["name"]
            md = orig_max_diff.get(name, global_max if name not in ("COUNT", "END") else 0)
            new_phrases.append({
                "MaxDifficulty": md,
                "Name": name,
                "IterationCount": phrase_iter_counts[name],
            })
        attrs["Phrases"] = new_phrases

        # PhraseIterations: {PhraseIndex, MaxDifficulty, Name, StartTime, EndTime}
        pi_list = []
        for i, b in enumerate(non_end):
            end_time = non_end[i + 1].time if i + 1 < len(non_end) else b.time
            pidx = unique_phrases[b.name]
            md = new_phrases[pidx]["MaxDifficulty"]
            pi_list.append({
                "PhraseIndex": pidx,
                "MaxDifficulty": md,
                "Name": b.name,
                "StartTime": round(b.time, 3),
                "EndTime": round(end_time, 3),
            })
        attrs["PhraseIterations"] = pi_list

        # Sections: {Name, UIName, Number, StartTime, EndTime,
        #            StartPhraseIterationIndex, EndPhraseIterationIndex, IsSolo}
        new_sections = []
        section_name_counts: dict[str, int] = {}
        for i, b in enumerate(non_end):
            if b.name == "COUNT":
                continue
            end_time = non_end[i + 1].time if i + 1 < len(non_end) else b.time
            section_name_counts[b.name] = section_name_counts.get(b.name, 0) + 1
            num = section_name_counts[b.name]
            new_sections.append({
                "Name": b.name,
                "UIName": _ui_name(b.name, num),
                "Number": num,
                "StartTime": round(b.time, 3),
                "EndTime": round(end_time, 3),
                "StartPhraseIterationIndex": i,
                "EndPhraseIterationIndex": i,
                "IsSolo": False,
            })
        attrs["Sections"] = new_sections

    return json.dumps(manifest, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# PSARC repair orchestrator
# ---------------------------------------------------------------------------

def _extract_boundaries(sng: Container) -> list[SegmentBoundary]:
    """Extract SegmentBoundary list from an existing SNG's sections.

    Returns the same format as determine_boundaries(): COUNT at 0,
    one boundary per section, END at songLength.
    """
    boundaries = [SegmentBoundary(time=0.0, name="COUNT", number=1)]
    for sec in sng.sections:
        boundaries.append(SegmentBoundary(
            time=sec.startTime,
            name=sec.name,
            number=sec.number,
        ))
    boundaries.append(SegmentBoundary(
        time=sng.metadata.songLength,
        name="END",
        number=1,
    ))
    return boundaries


def repair_psarc(
    psarc_path: Path,
    output_path: Path,
    dry_run: bool = False,
) -> "ValidationReport":
    """Fix broken CDLC by flattening to single max-difficulty level.

    Strips DD, keeps only the level with the most notes, sets all
    maxDifficulty=0, and recomputes derived fields.  For cross-arrangement
    section mismatches, uses bass SNG sections as canonical.

    XML and manifest are left untouched (no phrase reordering).

    Returns the post-repair ValidationReport.
    """
    from .validate import validate_psarc, ValidationReport

    with open(psarc_path, "rb") as f:
        content = PSARC(crypto=True).parse_stream(f)

    # Find bass SNG — canonical source for section structure
    bass_key = _find_bass_sng_key(content)
    if bass_key is None:
        raise ValueError("No bass SNG found in PSARC")

    bass_sng = Song.parse(content[bass_key])
    if not bass_sng.sections:
        raise ValueError("Bass SNG has no sections")

    canonical_boundaries = _extract_boundaries(bass_sng)
    bass_section_count = len(bass_sng.sections)
    log.info("Canonical: %d sections from bass", bass_section_count)

    # Process each arrangement SNG
    sng_keys = [
        k for k in content
        if ("songs/bin/generic/" in k or "songs/bin/macos/" in k)
        and k.endswith(".sng")
    ]

    # First pass: detect if any arrangement has a section count mismatch
    had_section_mismatch = False
    arrangement_sngs: list[tuple[str, Container]] = []
    for key in sng_keys:
        short = key.split("/")[-1]
        try:
            arr_sng = Song.parse(content[key])
        except Exception as e:
            log.warning("Failed to parse SNG %s: %s", short, e)
            continue

        if not arr_sng.sections:
            continue  # Vocals — no sections, skip

        arrangement_sngs.append((key, arr_sng))
        if len(arr_sng.sections) != bass_section_count:
            had_section_mismatch = True

    # Second pass: rebuild or flatten each arrangement
    for key, arr_sng in arrangement_sngs:
        short = key.split("/")[-1]
        if had_section_mismatch:
            # Rebuild ALL arrangements with canonical boundaries so PI counts
            # are consistent across arrangements and with rebuilt XML/manifest
            log.info("Rebuilding %s: %d sections -> %d (canonical)",
                     short, len(arr_sng.sections), bass_section_count)
            rebuilt_bytes = rebuild_sng(arr_sng, canonical_boundaries)
            rebuilt_sng_obj = Song.parse(rebuilt_bytes)
            content[key] = flatten_sng(rebuilt_sng_obj)
        else:
            log.info("Flattening %s: %d levels -> 1", short, len(arr_sng.levels))
            content[key] = flatten_sng(arr_sng)

    # Only rebuild XML/manifest if section structure actually changed
    if had_section_mismatch:
        for key in list(content.keys()):
            if key.startswith("songs/arr/") and key.endswith(".xml"):
                try:
                    xml_text = content[key].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if "<sections" in xml_text:
                    content[key] = rebuild_xml(content[key], canonical_boundaries)
                    log.info("Rebuilt XML: %s", key)

        for key in list(content.keys()):
            if key.startswith("manifests/") and key.endswith(".json"):
                try:
                    manifest = json.loads(content[key])
                    has_sections = any(
                        entry.get("Attributes", {}).get("Sections")
                        for entry in manifest.get("Entries", {}).values()
                    )
                    if has_sections:
                        content[key] = rebuild_manifest(content[key], canonical_boundaries)
                        log.info("Rebuilt manifest: %s", key)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

    if dry_run:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".psarc", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            PSARC(crypto=True).build_stream(content, tmp)
        try:
            report = validate_psarc(tmp_path)
            report.path = psarc_path
        finally:
            tmp_path.unlink()
        return report

    # Write output PSARC
    with open(output_path, "wb") as f:
        PSARC(crypto=True).build_stream(content, f)
    log.info("Wrote: %s", output_path)

    return validate_psarc(output_path)


# ---------------------------------------------------------------------------
# Top-level reslice orchestrator
# ---------------------------------------------------------------------------

def reslice_psarc(
    psarc_path: Path,
    output_path: Path,
    min_segment: float = 3.0,
    max_segment: float = 15.0,
    dry_run: bool = False,
    manual_splits: list[float] | None = None,
) -> list[SegmentBoundary]:
    """Re-segment a PSARC file based on note-gap analysis.

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

    # Get notes from the best level for gap analysis.
    # For DD songs, the global max level may only contain notes for
    # the highest-maxDifficulty phrases.  We want the highest level
    # that still covers ALL non-ignored phrase iterations.
    if not sng.levels:
        raise ValueError("No levels found in SNG")

    ignored_pi = {
        i for i, pi in enumerate(sng.phraseIterations)
        if sng.phrases[pi.phraseId].ignore
    }
    target_pis = set(range(len(sng.phraseIterations))) - ignored_pi

    best_level = None
    for lv in sorted(sng.levels, key=lambda l: l.difficulty, reverse=True):
        covered = {n.phraseIterationId for n in lv.notes}
        if target_pis <= covered:
            best_level = lv
            break

    if best_level is None:
        # Fallback: level with the most notes
        best_level = max(sng.levels, key=lambda l: len(l.notes))

    notes = list(best_level.notes)
    log.info("Analysis level (%d): %d notes", best_level.difficulty, len(notes))

    # Determine boundaries from note gaps
    song_length = sng.metadata.songLength
    boundaries = determine_boundaries(
        notes, list(sng.beats), song_length,
        min_segment=min_segment, max_segment=max_segment,
        manual_splits=manual_splits,
    )

    # Assign section names from original sections
    boundaries = assign_section_names(boundaries, list(sng.sections))

    log.info("New boundaries: %d segments", len(boundaries) - 1)

    if dry_run:
        return boundaries

    # Rebuild ALL arrangement SNGs (sections are song-level in Rocksmith)
    for key in list(content.keys()):
        if ("songs/bin/generic/" in key or "songs/bin/macos/" in key) \
                and key.endswith(".sng"):
            try:
                arr_sng = Song.parse(content[key])
                if arr_sng.sections:  # skip vocals (no sections)
                    content[key] = rebuild_sng(arr_sng, boundaries)
                    log.info("Rebuilt SNG: %s", key)
            except Exception as e:
                log.warning("Failed to rebuild SNG %s: %s", key, e)

    # Rebuild ALL manifests (sections are song-level in Rocksmith)
    for key in list(content.keys()):
        if key.startswith("manifests/") and key.endswith(".json"):
            try:
                manifest = json.loads(content[key])
                has_sections = any(
                    entry.get("Attributes", {}).get("Sections")
                    for entry in manifest.get("Entries", {}).values()
                )
                if has_sections:
                    content[key] = rebuild_manifest(content[key], boundaries)
                    log.info("Rebuilt manifest: %s", key)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    # Rebuild ALL arrangement XMLs (sections are song-level in Rocksmith)
    for key in list(content.keys()):
        if key.startswith("songs/arr/") and key.endswith(".xml"):
            try:
                xml_text = content[key].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "<sections" in xml_text:
                content[key] = rebuild_xml(content[key], boundaries)
                log.info("Rebuilt XML: %s", key)

    # Write output PSARC
    with open(output_path, "wb") as f:
        PSARC(crypto=True).build_stream(content, f)

    log.info("Wrote: %s", output_path)
    return boundaries
