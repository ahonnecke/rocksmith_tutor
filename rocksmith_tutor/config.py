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
ID_MAP_PATH = DATA_DIR / "id_map.json"

ROCKSMITH_APP_ID = "221680"

if platform.system() == "Darwin":
    _STEAM_USERDATA = (
        Path.home() / "Library" / "Application Support" / "Steam" / "userdata"
    )
    DEFAULT_PSARC_DIRS = [
        Path.home() / "Library" / "Application Support" / "Steam"
        / "steamapps" / "common" / "Rocksmith2014" / "dlc",
    ]
else:
    _STEAM_USERDATA = Path.home() / ".local" / "share" / "Steam" / "userdata"
    DEFAULT_PSARC_DIRS = [
        Path.home() / "nasty" / "music" / "Rocksmith_CDLC" / "verified",
        Path.home() / "nasty" / "music" / "Rocksmith_CDLC" / "unverified",
    ]

DEFAULT_MODEL = "claude-sonnet-4-20250514"
