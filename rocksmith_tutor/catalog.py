"""Song catalog data model with JSON persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from .config import CATALOG_PATH


@dataclass
class SectionInfo:
    name: str
    number: int
    start_time: float
    end_time: float
    is_solo: bool


@dataclass
class SongEntry:
    song_id: str
    artist: str
    song_name: str
    album: str
    year: int
    psarc_path: str
    psarc_mtime: float
    tempo: float
    song_length: float
    tuning: dict[str, int]
    standard_tuning: bool
    difficulty_easy: float
    difficulty_med: float
    difficulty_hard: float
    notes_easy: int
    notes_med: int
    notes_hard: int
    max_phrase_difficulty: int
    techniques: dict[str, bool]
    sections: list[SectionInfo]
    dlc_key: str = ""

    def technique_list(self) -> list[str]:
        """Return list of technique names that are True for this song."""
        return [t for t, v in self.techniques.items() if v]

    def section_summary(self) -> str:
        """Compact section summary: 'intro(3),chorus(4),...'"""
        counts: dict[str, int] = {}
        for s in self.sections:
            counts[s.name] = counts.get(s.name, 0) + 1
        return ",".join(f"{name}({count})" for name, count in counts.items())

    def one_line_summary(self) -> str:
        """Single-line summary for LLM context."""
        techs = ",".join(self.technique_list())
        return (
            f"{self.artist} - {self.song_name} | "
            f"{self.tempo:.0f}bpm | "
            f"diff:{self.difficulty_hard:.2f} | "
            f"{techs} | "
            f"sections: {self.section_summary()}"
        )


@dataclass
class Catalog:
    songs: dict[str, SongEntry] = field(default_factory=dict)
    scanned_at: str = ""
    version: int = 1

    def save(self, path: Path | None = None) -> None:
        path = path or CATALOG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "scanned_at": self.scanned_at,
            "songs": {k: asdict(v) for k, v in self.songs.items()},
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path | None = None) -> Catalog:
        path = path or CATALOG_PATH
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        songs = {}
        for k, v in data.get("songs", {}).items():
            v["sections"] = [SectionInfo(**s) for s in v.get("sections", [])]
            songs[k] = SongEntry(**v)
        return cls(
            songs=songs,
            scanned_at=data.get("scanned_at", ""),
            version=data.get("version", 1),
        )

    def update_timestamp(self) -> None:
        self.scanned_at = datetime.now(timezone.utc).isoformat()

    @property
    def bass_song_count(self) -> int:
        return len(self.songs)

    def songs_with_technique(self, technique: str) -> list[SongEntry]:
        return [s for s in self.songs.values() if s.techniques.get(technique)]

    def songs_by_artist(self, artist_substr: str) -> list[SongEntry]:
        needle = artist_substr.lower()
        return [s for s in self.songs.values() if needle in s.artist.lower()]
