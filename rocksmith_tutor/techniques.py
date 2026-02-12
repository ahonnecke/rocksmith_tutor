"""Bass technique taxonomy and detection constants.

Defines technique categories parsed from Rocksmith manifest
ArrangementProperties and SNG note-level data.
"""

# Techniques detected from manifest ArrangementProperties (boolean flags)
MANIFEST_TECHNIQUES = [
    "slides",
    "unpitchedSlides",
    "hopo",
    "slapPop",
    "fretHandMutes",
    "palmMutes",
    "harmonics",
    "pinchHarmonics",
    "tapping",
    "vibrato",
    "tremolo",
    "bends",
    "sustain",
    "syncopation",
    "twoFingerPicking",
    "bassPick",
    "fingerPicking",
    "fifthsAndOctaves",
    "doubleStops",
    "openChords",
    "pickDirection",
]

# Skill progression grouping for curriculum module ordering
SKILL_GROUPS = {
    "fundamentals": {
        "name": "Bass Fundamentals",
        "order": 1,
        "level": "beginner",
        "techniques": ["sustain", "twoFingerPicking", "bassPick", "fingerPicking"],
    },
    "rhythm": {
        "name": "Rhythm & Muting",
        "order": 2,
        "level": "beginner-intermediate",
        "techniques": ["syncopation", "fretHandMutes", "palmMutes"],
    },
    "articulation": {
        "name": "Articulation",
        "order": 3,
        "level": "intermediate",
        "techniques": ["hopo", "slides", "unpitchedSlides", "bends", "vibrato"],
    },
    "advanced": {
        "name": "Advanced Techniques",
        "order": 4,
        "level": "advanced",
        "techniques": [
            "slapPop",
            "tapping",
            "harmonics",
            "pinchHarmonics",
            "tremolo",
            "pickDirection",
        ],
    },
    "patterns": {
        "name": "Patterns & Chords",
        "order": 5,
        "level": "intermediate-advanced",
        "techniques": ["fifthsAndOctaves", "doubleStops", "openChords"],
    },
}

# Human-readable names for display
TECHNIQUE_DISPLAY_NAMES = {
    "slides": "Slides",
    "unpitchedSlides": "Unpitched Slides",
    "hopo": "Hammer-On / Pull-Off",
    "slapPop": "Slap & Pop",
    "fretHandMutes": "Fret-Hand Mutes",
    "palmMutes": "Palm Mutes",
    "harmonics": "Harmonics",
    "pinchHarmonics": "Pinch Harmonics",
    "tapping": "Tapping",
    "vibrato": "Vibrato",
    "tremolo": "Tremolo",
    "bends": "Bends",
    "sustain": "Sustain",
    "syncopation": "Syncopation",
    "twoFingerPicking": "Two-Finger Picking",
    "bassPick": "Bass Pick",
    "fingerPicking": "Finger Picking",
    "fifthsAndOctaves": "Fifths & Octaves",
    "doubleStops": "Double Stops",
    "openChords": "Open Chords",
    "pickDirection": "Pick Direction",
}


def technique_group_for(technique: str) -> str | None:
    """Return the skill group ID for a given technique, or None."""
    for group_id, group in SKILL_GROUPS.items():
        if technique in group["techniques"]:
            return group_id
    return None
