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

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://10.0.1.201:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

DEFAULT_MODEL = "claude-sonnet-4-20250514"
