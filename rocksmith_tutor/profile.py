"""Rocksmith profile decryption, parsing, and PersistentID map management."""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from .config import (
    _STEAM_USERDATA,
    ROCKSMITH_APP_ID,
    ID_MAP_PATH,
    DEFAULT_PSARC_DIRS,
)
from .scanner import find_psarcs, extract_id_map_from_psarc

log = logging.getLogger(__name__)

# AES-256-ECB key for PRFLDB decryption (from rocksmith library)
PRF_KEY = bytes.fromhex(
    "728B369E24ED0134768511021812AFC0A3C25D02065F166B4BCC58CD2644F29E"
)


def find_profile_path() -> Path | None:
    """Auto-detect the most recent *_PRFLDB save file in Steam userdata."""
    if not _STEAM_USERDATA.is_dir():
        log.debug("Steam userdata dir not found: %s", _STEAM_USERDATA)
        return None

    candidates: list[Path] = []
    for user_dir in _STEAM_USERDATA.iterdir():
        if not user_dir.is_dir():
            continue
        remote = user_dir / ROCKSMITH_APP_ID / "remote"
        if remote.is_dir():
            candidates.extend(remote.glob("*_PRFLDB"))

    if not candidates:
        log.debug("No PRFLDB files found under %s", _STEAM_USERDATA)
        return None

    # Pick the most recently modified
    best = max(candidates, key=lambda p: p.stat().st_mtime)
    log.debug("Auto-detected profile: %s", best)
    return best


def decrypt_profile(path: Path) -> dict:
    """Decrypt a Rocksmith PRFLDB save file and return the parsed JSON."""
    data = path.read_bytes()

    if data[:4] != b"EVAS":
        raise ValueError(f"Not a PRFLDB file (magic={data[:4]!r})")

    # Skip 20-byte header
    payload = data[20:]

    # Trim to AES block boundary
    remainder = len(payload) % 16
    if remainder != 0:
        payload = payload[:-remainder]

    # AES-256-ECB decrypt
    cipher = Cipher(algorithms.AES(PRF_KEY), modes.ECB())
    dec = cipher.decryptor()
    result = dec.update(payload) + dec.finalize()

    # Zlib decompress
    decompressed = zlib.decompress(result)

    # Parse JSON (strip trailing nulls)
    text = decompressed.decode("utf-8").rstrip("\x00").rstrip()
    decoder = json.JSONDecoder()
    profile, _ = decoder.raw_decode(text)
    return profile


@dataclass
class SongProgress:
    """Per-song progress from the profile."""
    persistent_id: str
    song_id: str  # mapped from persistent_id via id_map
    badge_easy: int = 0
    badge_medium: int = 0
    badge_hard: int = 0
    badge_master: int = 0
    play_count: int = 0
    high_score_hard: float = 0.0
    timestamp: float = 0.0
    dd_avg: float = 0.0  # DynamicDifficulty average


@dataclass
class PlayerProfile:
    """Parsed player profile with song progress data."""
    songs: dict[str, SongProgress] = field(default_factory=dict)

    @property
    def mastered_song_ids(self) -> list[str]:
        """Songs with gold badge on Hard or Master (badge >= 5)."""
        return [
            sp.song_id for sp in self.songs.values()
            if sp.badge_hard >= 5 or sp.badge_master >= 5
        ]

    @property
    def competent_song_ids(self) -> list[str]:
        """Songs with silver+ badge on Hard or Master (badge >= 4)."""
        return [
            sp.song_id for sp in self.songs.values()
            if sp.badge_hard >= 4 or sp.badge_master >= 4
        ]

    @property
    def played_song_ids(self) -> list[str]:
        """Songs with any play count > 0."""
        return [
            sp.song_id for sp in self.songs.values()
            if sp.play_count > 0
        ]

    def get_by_song_id(self, song_id: str) -> SongProgress | None:
        """Look up progress by catalog song_id."""
        for sp in self.songs.values():
            if sp.song_id == song_id:
                return sp
        return None


def _compute_psarc_hash(dirs: list[Path]) -> str:
    """SHA-256 hash of sorted (path, mtime) for all PSARCs in dirs."""
    entries: list[tuple[str, float]] = []
    for d in dirs:
        if d.is_dir():
            for p in sorted(d.glob("*.psarc")):
                entries.append((str(p), p.stat().st_mtime))
    entries.sort()
    content = json.dumps(entries).encode()
    return hashlib.sha256(content).hexdigest()


def load_or_build_id_map(
    psarc_dirs: list[Path] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Load cached PersistentID -> song_id map, or build from PSARCs.

    The map is invalidated when the SHA-256 hash of PSARC (path, mtime)
    tuples changes.
    """
    dirs = psarc_dirs or DEFAULT_PSARC_DIRS
    current_hash = _compute_psarc_hash(dirs)

    # Try loading cache
    if not force and ID_MAP_PATH.exists():
        cached = json.loads(ID_MAP_PATH.read_text())
        if cached.get("psarc_hash") == current_hash:
            log.debug("ID map cache hit (%d entries)", len(cached["map"]))
            return cached["map"]
        log.debug("ID map cache stale (hash mismatch)")

    # Build from PSARCs
    psarcs = find_psarcs(dirs)
    if not psarcs:
        log.warning("No PSARCs found to build ID map from %s", dirs)
        return {}

    full_map: dict[str, str] = {}
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[filename]}"),
    ) as progress:
        task = progress.add_task("Building ID map", total=len(psarcs), filename="")

        for psarc_path in psarcs:
            progress.update(task, filename=psarc_path.name)
            try:
                partial = extract_id_map_from_psarc(psarc_path)
                full_map.update(partial)
            except Exception as e:
                log.debug("Failed to extract IDs from %s: %s", psarc_path.name, e)
                errors += 1
            progress.advance(task)

    if errors:
        log.warning("%d PSARCs failed during ID map build", errors)

    # Cache
    ID_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {"psarc_hash": current_hash, "map": full_map}
    ID_MAP_PATH.write_text(json.dumps(cache_data, indent=2))
    log.debug("ID map cached: %d entries -> %s", len(full_map), ID_MAP_PATH)

    return full_map


def parse_profile(
    profile_json: dict,
    id_map: dict[str, str],
) -> PlayerProfile:
    """Parse decrypted profile JSON into a PlayerProfile.

    Merges data from Songs (DynamicDifficulty) and SongsSA (Score Attack).
    """
    player = PlayerProfile()
    songs_dd = profile_json.get("Songs", {})
    songs_sa = profile_json.get("SongsSA", {})

    # Collect all PersistentIDs from both dicts
    all_pids = set()
    all_pids.update(k.upper() for k in songs_dd)
    all_pids.update(k.upper() for k in songs_sa)

    for pid in all_pids:
        song_id = id_map.get(pid, "")
        if not song_id:
            continue  # PersistentID not in our bass catalog

        sp = SongProgress(persistent_id=pid, song_id=song_id)

        # DynamicDifficulty data
        dd = songs_dd.get(pid, {})
        dd_data = dd.get("DynamicDifficulty", {})
        sp.dd_avg = float(dd_data.get("Avg", 0.0))
        sp.timestamp = float(dd.get("TimeStamp", 0.0))

        # Score Attack data
        sa = songs_sa.get(pid, {})
        badges = sa.get("Badges", {})
        sp.badge_easy = int(badges.get("Easy", 0))
        sp.badge_medium = int(badges.get("Medium", 0))
        sp.badge_hard = int(badges.get("Hard", 0))
        sp.badge_master = int(badges.get("Master", 0))
        sp.play_count = int(sa.get("PlayCount", 0))

        scores = sa.get("HighScores", {})
        sp.high_score_hard = float(scores.get("Hard", 0))

        player.songs[pid] = sp

    return player
