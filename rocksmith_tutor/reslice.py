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


def _note_complexity(notes: list) -> float:
    """Compute complexity score for a group of notes.

    Measures variety: unique (string, fret) positions × unique transitions,
    normalized by note count.  A repeating root-note pattern scores low;
    a riff using multiple strings and frets scores high.
    """
    if len(notes) < 2:
        return 0.0
    positions = set((n.string, n.fret) for n in notes)
    transitions = set()
    for i in range(len(notes) - 1):
        a, b = notes[i], notes[i + 1]
        transitions.add((b.string - a.string, b.fret - a.fret))
    return len(positions) * len(transitions) / len(notes)


def _score_gaps(gaps: list[NoteGap], beats: list, notes: list | None = None) -> None:
    """Score each gap using local-median ratio + beat-alignment + complexity change.

    Mutates gaps in place, setting .score and .snap_target.

    The complexity bonus detects transitions between simple repeating patterns
    and complex riffs (more strings, more pitch variety).  Gaps where the
    musical character changes get higher scores, making the greedy splitter
    prefer them as boundary points.
    """
    if not gaps:
        return

    # Pre-compute measure-start times for beat bonus
    measure_times = sorted(b.time for b in beats if b.beat == 0)

    # Pre-compute complexity change at each gap position
    complexity_bonus: list[float] = [1.0] * len(gaps)
    if notes and len(notes) >= 4:
        note_times = [n.time for n in notes]
        window = 4.0  # seconds of context on each side

        for i, gap in enumerate(gaps):
            t = gap.midpoint
            # Notes in [t - window, t)
            lo_idx = bisect.bisect_left(note_times, t - window)
            mid_idx = bisect.bisect_right(note_times, t)
            hi_idx = bisect.bisect_right(note_times, t + window)

            before = notes[lo_idx:mid_idx]
            after = notes[mid_idx:hi_idx]

            if len(before) >= 2 and len(after) >= 2:
                c_before = _note_complexity(before)
                c_after = _note_complexity(after)
                # Ratio of change — high when complexity spikes or drops
                if c_before > 0:
                    ratio = max(c_after / c_before, c_before / c_after)
                elif c_after > 0:
                    ratio = c_after + 1.0
                else:
                    ratio = 1.0
                # Boost gaps at complexity transitions (≥2x change → bonus)
                # Cap at 2x to avoid cascade-splitting in the greedy pass
                if ratio >= 2.0:
                    complexity_bonus[i] = 1.0 + min(ratio - 1.0, 1.0)

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

        # Complexity change bonus
        gap.score *= complexity_bonus[i]

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
    _score_gaps(gaps, beats, notes)

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

    # --- Step 4: split at complexity transitions within existing segments ---
    # Even if a segment is under max_segment, split it when the musical
    # character changes significantly (e.g. simple repeating pattern → complex
    # riff).  Only split segments that are long enough to benefit; skip
    # segments already near min_segment.
    complexity_min_span = max(min_segment * 2.5, 8.0)  # don't split short segs
    note_times = [n.time for n in notes]
    for _ in range(50):  # safety bound
        bounds = _all_bounds()
        split_made = False
        for i in range(len(bounds) - 1):
            seg_lo, seg_hi = bounds[i], bounds[i + 1]
            span = seg_hi - seg_lo
            if span < complexity_min_span:
                continue

            # Find notes in this segment
            lo_idx = bisect.bisect_left(note_times, seg_lo)
            hi_idx = bisect.bisect_right(note_times, seg_hi)
            seg_notes = notes[lo_idx:hi_idx]
            if len(seg_notes) < 10:
                continue

            # Find the point of maximum complexity change within the segment.
            # Use 8-note windows on each side for stable measurement.
            best_ratio = 1.0
            best_gap_t = None
            step = max(1, len(seg_notes) // 20)
            for j in range(4, len(seg_notes) - 4, step):
                before = seg_notes[max(0, j - 8):j]
                after = seg_notes[j:min(len(seg_notes), j + 8)]
                if len(before) < 3 or len(after) < 3:
                    continue
                c_b = _note_complexity(before)
                c_a = _note_complexity(after)
                if c_b > 0:
                    ratio = max(c_a / c_b, c_b / c_a)
                elif c_a > 0:
                    ratio = c_a + 1.0
                else:
                    continue
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_gap_t = seg_notes[j].time

            # Only split if complexity change is substantial (≥3x)
            if best_ratio >= 3.0 and best_gap_t is not None:
                # Find the best gap near the complexity transition
                search_lo = max(seg_lo, best_gap_t - 2.0)
                search_hi = min(seg_hi, best_gap_t + 2.0)
                best = _best_gap_in_range(gaps, search_lo, search_hi)
                if best is not None and _respects_min(best.snap_target):
                    selected_times.append(best.snap_target)
                    split_made = True
                    break  # restart scan with updated bounds
        if not split_made:
            break

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

            # COUNT/END are normally ignored (not scored, skipped by
            # Riff Repeater).  But if COUNT has notes, mark it playable
            # so Riff Repeater can reach it.
            if name == "END":
                ignore = 1
            elif name == "COUNT" and len(non_end) > 1:
                count_end = non_end[1].time
                has_notes = any(
                    any(n.time < count_end for n in lv.notes)
                    for lv in sng.levels
                )
                ignore = 0 if has_notes else 1
            else:
                ignore = 0

            new_phrases.append(Container(
                solo=0,
                disparity=0,
                ignore=ignore,
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

    # --- 3. Build new sections (1:1 with PIs) ---
    # Include COUNT PI as "intro" section so notes in the pre-song region
    # are reachable in Riff Repeater.
    new_sections = ListContainer()
    section_name_counts: dict[str, int] = {}
    for pi_idx in range(len(new_pi)):
        pi = new_pi[pi_idx]
        name = new_phrases[pi.phraseId].name
        if name == "END":
            continue
        if name == "COUNT":
            name = "intro"
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

    # Composite: for each PI, pull notes from levels[phrase.maxDifficulty].
    # DD spreads notes across levels — no single level has all of them.
    pi_times = [pi.time for pi in sng.phraseIterations]
    num_pi = len(pi_times)

    # Use level 0 as the base (has anchors, handshapes, etc. for full song)
    base = sng.levels[0]
    composite_notes = ListContainer()

    for pi_idx, pi in enumerate(sng.phraseIterations):
        end = pi_times[pi_idx + 1] if pi_idx + 1 < num_pi else sng.metadata.songLength
        max_d = min(sng.phrases[pi.phraseId].maxDifficulty, len(sng.levels) - 1)
        source = sng.levels[max_d]
        for n in source.notes:
            if pi.time <= n.time < end:
                composite_notes.append(n)

    composite_notes.sort(key=lambda n: n.time)
    base.notes = composite_notes
    base.difficulty = 0

    # Replace levels with just the composite
    sng.levels = ListContainer([base])

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
        if b.name == "END":
            continue
        name = "intro" if b.name == "COUNT" else b.name
        number = 1 if b.name == "COUNT" else b.number
        section_entries.append((name, number, b.time))
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
            name = "intro" if b.name == "COUNT" else b.name
            end_time = non_end[i + 1].time if i + 1 < len(non_end) else b.time
            section_name_counts[name] = section_name_counts.get(name, 0) + 1
            num = section_name_counts[name]
            new_sections.append({
                "Name": name,
                "UIName": _ui_name(name, num),
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
    """Flatten bass arrangement to 100% difficulty.

    Only touches the bass SNG — leaves lead, rhythm, vocals, XML, and
    manifest untouched to minimize risk of breaking anything.

    Returns the post-repair ValidationReport.
    """
    from .validate import validate_psarc, ValidationReport

    with open(psarc_path, "rb") as f:
        content = PSARC(crypto=True).parse_stream(f)

    bass_key = _find_bass_sng_key(content)
    if bass_key is None:
        raise ValueError("No bass SNG found in PSARC")

    bass_sng = Song.parse(content[bass_key])
    if not bass_sng.sections:
        raise ValueError("Bass SNG has no sections")

    log.info("Flattening bass: %s (%d levels -> 1)",
             bass_key.split("/")[-1], len(bass_sng.levels))
    content[bass_key] = flatten_sng(bass_sng)

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

    # Build composite notes for gap analysis: for each PI, take notes from
    # levels[phrase.maxDifficulty].  No single DD level has all the notes.
    if not sng.levels:
        raise ValueError("No levels found in SNG")

    pi_times = [pi.time for pi in sng.phraseIterations]
    num_pi = len(pi_times)
    notes = []
    for pi_idx, pi in enumerate(sng.phraseIterations):
        end = pi_times[pi_idx + 1] if pi_idx + 1 < num_pi else sng.metadata.songLength
        max_d = min(sng.phrases[pi.phraseId].maxDifficulty, len(sng.levels) - 1)
        for n in sng.levels[max_d].notes:
            if pi.time <= n.time < end:
                notes.append(n)
    notes.sort(key=lambda n: n.time)
    log.info("Composite analysis notes: %d", len(notes))

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
