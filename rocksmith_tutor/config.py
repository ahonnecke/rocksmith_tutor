"""Paths, defaults, and environment variable configuration."""

import os
import platform
from pathlib import Path

DATA_DIR = Path(os.environ.get(
    "ROCKSMITH_TUTOR_DATA_DIR",
    Path.home() / ".local" / "share" / "rocksmith_tutor",
))

CATALOG_PATH = DATA_DIR / "catalog.json"
CURRICULUM_PATH = DATA_DIR / "curriculum.yaml"

if platform.system() == "Darwin":
    DEFAULT_PSARC_DIRS = [
        Path.home() / "Library" / "Application Support" / "Steam"
        / "steamapps" / "common" / "Rocksmith2014" / "dlc",
    ]
else:
    DEFAULT_PSARC_DIRS = [
        Path.home() / "nasty" / "music" / "Rocksmith_CDLC" / "verified",
        Path.home() / "nasty" / "music" / "Rocksmith_CDLC" / "unverified",
    ]

DEFAULT_MODEL = "claude-sonnet-4-20250514"
