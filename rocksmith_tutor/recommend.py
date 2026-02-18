"""Goldilocks recommendation algorithm: songs slightly harder than your comfort zone."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from .catalog import Catalog, SongEntry
from .profile import PlayerProfile

log = logging.getLogger(__name__)


class Zone(enum.Enum):
    WARMUP = "warm-up"
    GROWTH = "growth"
    CHALLENGE = "challenge"
    REACH = "reach"


# Lower number = higher priority in recommendations
ZONE_PRIORITY = {
    Zone.GROWTH: 0,
    Zone.CHALLENGE: 1,
    Zone.WARMUP: 2,
    Zone.REACH: 3,
}


@dataclass
class ZoneBounds:
    zone: Zone
    lo: float
    hi: float

    @property
    def midpoint(self) -> float:
        return (self.lo + self.hi) / 2

    def contains(self, difficulty: float) -> bool:
        return self.lo <= difficulty < self.hi


@dataclass
class Recommendation:
    song: SongEntry
    zone: Zone
    play_count: int
    score: tuple  # sort key
    teaching_note: str = ""


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def compute_comfort_ceiling(
    catalog: Catalog,
    profile: PlayerProfile,
) -> float:
    """Compute the comfort zone ceiling from mastered/played songs.

    Uses 85th percentile of mastered song difficulties, falling back to
    70th percentile of played songs, falling back to 0.15 for beginners.
    """
    # Try mastered songs first (silver+ on Hard or Master)
    competent_ids = profile.competent_song_ids
    competent_diffs = sorted(
        catalog.songs[sid].difficulty_hard
        for sid in competent_ids
        if sid in catalog.songs
    )

    if len(competent_diffs) >= 3:
        idx = int(len(competent_diffs) * 0.85)
        ceiling = competent_diffs[min(idx, len(competent_diffs) - 1)]
        log.debug(
            "Comfort ceiling from %d mastered songs: %.3f (85th pct)",
            len(competent_diffs), ceiling,
        )
        return ceiling

    # Fallback: played songs
    played_ids = profile.played_song_ids
    played_diffs = sorted(
        catalog.songs[sid].difficulty_hard
        for sid in played_ids
        if sid in catalog.songs
    )

    if len(played_diffs) >= 3:
        idx = int(len(played_diffs) * 0.70)
        ceiling = played_diffs[min(idx, len(played_diffs) - 1)]
        log.debug(
            "Comfort ceiling from %d played songs: %.3f (70th pct)",
            len(played_diffs), ceiling,
        )
        return ceiling

    # Absolute beginner
    log.debug("Beginner fallback: ceiling=0.15")
    return 0.15


def compute_zone_bounds(ceiling: float) -> dict[Zone, ZoneBounds]:
    """Compute the four zone bands relative to the comfort ceiling."""
    return {
        Zone.WARMUP: ZoneBounds(
            Zone.WARMUP, _clamp(ceiling - 0.10), _clamp(ceiling),
        ),
        Zone.GROWTH: ZoneBounds(
            Zone.GROWTH, _clamp(ceiling), _clamp(ceiling + 0.10),
        ),
        Zone.CHALLENGE: ZoneBounds(
            Zone.CHALLENGE, _clamp(ceiling + 0.10), _clamp(ceiling + 0.20),
        ),
        Zone.REACH: ZoneBounds(
            Zone.REACH, _clamp(ceiling + 0.20), _clamp(ceiling + 0.30),
        ),
    }


def get_recommendations(
    catalog: Catalog,
    profile: PlayerProfile,
    count: int = 20,
    zone_filter: Zone | None = None,
    technique_filter: str | None = None,
) -> tuple[float, dict[Zone, ZoneBounds], list[Recommendation]]:
    """Generate goldilocks recommendations.

    Returns (ceiling, zone_bounds, recommendations).
    """
    ceiling = compute_comfort_ceiling(catalog, profile)
    bounds = compute_zone_bounds(ceiling)

    # Set of already-mastered song_ids to exclude
    mastered = set(profile.competent_song_ids)

    recommendations: list[Recommendation] = []

    for song in catalog.songs.values():
        # Skip mastered songs
        if song.song_id in mastered:
            continue

        # Technique filter
        if technique_filter and not song.techniques.get(technique_filter):
            continue

        diff = song.difficulty_hard

        # Find which zone this song falls into
        song_zone: Zone | None = None
        for zone, zb in bounds.items():
            if zb.contains(diff):
                song_zone = zone
                break

        if song_zone is None:
            continue

        # Zone filter
        if zone_filter is not None and song_zone != zone_filter:
            continue

        # Get play count for this song
        progress = profile.get_by_song_id(song.song_id)
        play_count = progress.play_count if progress else 0

        # Sort key: zone priority, prefer unplayed, closest to zone midpoint
        zone_mid = bounds[song_zone].midpoint
        sort_key = (
            ZONE_PRIORITY[song_zone],
            play_count,
            abs(diff - zone_mid),
        )

        recommendations.append(Recommendation(
            song=song,
            zone=song_zone,
            play_count=play_count,
            score=sort_key,
        ))

    recommendations.sort(key=lambda r: r.score)
    return ceiling, bounds, recommendations[:count]
