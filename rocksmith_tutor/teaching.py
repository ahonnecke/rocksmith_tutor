"""Teaching notes: pedagogical context for song recommendations.

Two layers:
- Template: deterministic metadata (skill group, tuning, tempo, density)
- LLM: bass-specific pedagogical descriptions from Anthropic Haiku
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

from .catalog import Catalog, SongEntry
from .config import TEACHING_NOTES_PATH, DEFAULT_ENRICH_MODEL
from .techniques import SKILL_GROUPS

log = logging.getLogger(__name__)
console = Console()

# --- Tuning detection ---

# Standard tuning for bass (4-string): all zeros
STANDARD_TUNING = {str(i): 0 for i in range(4)}

KNOWN_TUNINGS: list[tuple[str, dict[str, int]]] = [
    ("Drop D", {"0": -2, "1": 0, "2": 0, "3": 0}),
    ("Drop C", {"0": -4, "1": -2, "2": -2, "3": -2}),
    ("Drop C#", {"0": -3, "1": -1, "2": -1, "3": -1}),
    ("D Standard", {"0": -1, "1": -1, "2": -1, "3": -1}),
    ("Eb Standard", {"0": 1, "1": 1, "2": 1, "3": 1}),
    ("C Standard", {"0": -4, "1": -4, "2": -4, "3": -4}),
    ("DADG", {"0": -2, "1": 0, "2": 0, "3": 0}),
]


def detect_tuning_name(tuning: dict[str, int]) -> str:
    """Map a tuning offset dict to a human-readable name."""
    # Normalize to first 4 strings (bass)
    t = {str(i): tuning.get(str(i), 0) for i in range(min(6, len(tuning)))}
    bass_t = {str(i): t.get(str(i), 0) for i in range(4)}

    if all(v == 0 for v in bass_t.values()):
        return "Standard"

    for name, offsets in KNOWN_TUNINGS:
        if bass_t == offsets:
            return name

    # Unknown non-standard
    vals = [bass_t[str(i)] for i in range(4)]
    return f"Custom ({','.join(f'{v:+d}' for v in vals)})"


# --- Tempo / density classification ---

def tempo_band(bpm: float) -> str:
    if bpm < 90:
        return f"Slow ({bpm:.0f} BPM)"
    elif bpm < 140:
        return f"Medium ({bpm:.0f} BPM)"
    elif bpm < 180:
        return f"Fast ({bpm:.0f} BPM)"
    else:
        return f"Very Fast ({bpm:.0f} BPM)"


def note_density_label(notes: int, length: float) -> str:
    if length <= 0:
        return "Unknown"
    nps = notes / length
    if nps < 1.5:
        return "Sparse"
    elif nps < 3.0:
        return "Moderate"
    elif nps < 5.0:
        return "Dense"
    else:
        return "Very Dense"


def difficulty_curve_label(easy: float, hard: float) -> str:
    spread = hard - easy
    if spread < 0.05:
        return "Flat"
    elif spread < 0.15:
        return "Gradual"
    elif spread < 0.30:
        return "Moderate"
    else:
        return "Steep"


# --- Skill group mapping ---

def skill_focus(techniques: dict[str, bool]) -> str:
    """Map active techniques to their skill group names."""
    active = [t for t, v in techniques.items() if v]
    if not active:
        return "General"

    groups_seen: dict[str, list[str]] = {}
    for tech in active:
        for group in SKILL_GROUPS.values():
            if tech in group["techniques"]:
                groups_seen.setdefault(group["name"], []).append(tech)
                break

    if not groups_seen:
        return "General"

    # Return the group(s) with a parenthetical of specific techniques
    parts = []
    for group_name, techs in groups_seen.items():
        parts.append(f"{group_name} ({', '.join(techs)})")
    return " | ".join(parts)


# --- Template line ---

def compute_template_line(song: SongEntry) -> str:
    """Build deterministic template line from song metadata."""
    parts = [
        skill_focus(song.techniques),
        detect_tuning_name(song.tuning),
        tempo_band(song.tempo),
        note_density_label(song.notes_hard, song.song_length),
    ]

    curve = difficulty_curve_label(song.difficulty_easy, song.difficulty_hard)
    if curve != "Flat":
        parts.append(f"{curve} progression")

    has_solo = any(s.is_solo for s in song.sections)
    if has_solo:
        parts.append("Features solo")

    return " | ".join(parts)


# --- Data model and persistence ---

@dataclass
class TeachingNote:
    template_line: str
    llm_description: str = ""
    model: str = ""
    enriched_at: str = ""


@dataclass
class TeachingNotesStore:
    version: int = 1
    enriched_at: str = ""
    notes: dict[str, TeachingNote] = field(default_factory=dict)

    def save(self, path: Path | None = None) -> None:
        path = path or TEACHING_NOTES_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "enriched_at": self.enriched_at,
            "notes": {k: asdict(v) for k, v in self.notes.items()},
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path | None = None) -> TeachingNotesStore:
        path = path or TEACHING_NOTES_PATH
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        notes = {}
        for k, v in data.get("notes", {}).items():
            notes[k] = TeachingNote(**v)
        return cls(
            version=data.get("version", 1),
            enriched_at=data.get("enriched_at", ""),
            notes=notes,
        )

    def get(self, song_id: str) -> TeachingNote | None:
        return self.notes.get(song_id)

    def display_text(self, song_id: str) -> str:
        """Combined display string for a song's teaching note."""
        note = self.notes.get(song_id)
        if not note:
            return ""
        parts = []
        if note.llm_description:
            parts.append(note.llm_description)
        if note.template_line:
            parts.append(note.template_line)
        return " | ".join(parts) if parts else ""


# --- LLM enrichment ---

ENRICH_SYSTEM_PROMPT = """\
You are a bass guitar teacher annotating songs for a learning curriculum.
For each song, write 1-2 sentences about what makes it pedagogically
valuable for a bass student. Focus on: the bass playing style/pattern,
what technique it develops, and any notable musical context. Be specific
about bass — not general music facts. Be concise.\
"""

ENRICH_USER_TEMPLATE = """\
Annotate these bass arrangements for a learning curriculum. Return a JSON \
array where each element has "song_id" (string) and "description" (string, \
1-2 sentences about bass pedagogy).

Songs:
{song_lines}

Return ONLY valid JSON. No markdown fences, no extra text.\
"""


def _build_song_line(idx: int, song: SongEntry) -> str:
    techs = ", ".join(song.technique_list()) or "none"
    tuning = detect_tuning_name(song.tuning)
    return (
        f'{idx}. song_id: "{song.song_id}" | '
        f"{song.artist} - {song.song_name} | "
        f"{song.tempo:.0f} BPM | {tuning} | {techs}"
    )


def enrich_batch_llm(
    songs: list[SongEntry],
    model: str,
) -> dict[str, str]:
    """Call LLM to get pedagogical descriptions for a batch of songs.

    Returns dict of song_id -> description.
    """
    client = Anthropic()

    song_lines = "\n".join(
        _build_song_line(i, s)
        for i, s in enumerate(songs, 1)
    )

    user_prompt = ENRICH_USER_TEMPLATE.format(song_lines=song_lines)

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=ENRICH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw: str = response.content[0].text  # type: ignore[union-attr]
    log.debug("LLM batch response (%d in, %d out): %.200s...",
              response.usage.input_tokens, response.usage.output_tokens, raw)

    # Parse JSON response — handle markdown fences if model wraps it
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        log.error("Failed to parse LLM JSON response: %.500s", text)
        return {}

    result: dict[str, str] = {}
    for item in items:
        sid = item.get("song_id", "")
        desc = item.get("description", "")
        if sid and desc:
            result[sid] = desc

    return result


def enrich_catalog(
    catalog: Catalog,
    force: bool = False,
    skip_llm: bool = False,
    model: str | None = None,
    batch_size: int = 40,
) -> TeachingNotesStore:
    """Enrich all catalog songs with teaching notes.

    - Template layer is always recomputed (free).
    - LLM layer is only called for songs missing llm_description (unless force).
    """
    model = model or DEFAULT_ENRICH_MODEL
    store = TeachingNotesStore.load()
    now = datetime.now(timezone.utc).isoformat()

    songs = list(catalog.songs.values())

    # Phase 1: template layer (always recomputed)
    console.print(f"[dim]Computing template layer for {len(songs)} songs...[/]")
    for song in songs:
        template = compute_template_line(song)
        if song.song_id in store.notes:
            store.notes[song.song_id].template_line = template
        else:
            store.notes[song.song_id] = TeachingNote(
                template_line=template,
                enriched_at=now,
            )

    # Phase 2: LLM layer
    if skip_llm:
        console.print("[dim]Skipping LLM enrichment (--skip-llm)[/]")
    else:
        # Determine which songs need LLM enrichment
        if force:
            needs_llm = songs
        else:
            needs_llm = [
                s for s in songs
                if not store.notes.get(s.song_id, TeachingNote("")).llm_description
            ]

        if not needs_llm:
            console.print("[dim]All songs already have LLM descriptions.[/]")
        else:
            console.print(
                f"[bold]Enriching {len(needs_llm)} songs via {model} "
                f"(batch size {batch_size})...[/]"
            )

            # Process in batches
            batches = [
                needs_llm[i:i + batch_size]
                for i in range(0, len(needs_llm), batch_size)
            ]

            total_enriched = 0
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total} batches"),
                console=console,
            ) as progress:
                task = progress.add_task("LLM enrichment", total=len(batches))
                for batch_idx, batch in enumerate(batches):
                    progress.update(
                        task,
                        description=f"Batch {batch_idx + 1}/{len(batches)} "
                        f"({len(batch)} songs)",
                    )
                    try:
                        descriptions = enrich_batch_llm(batch, model)
                        for song in batch:
                            desc = descriptions.get(song.song_id, "")
                            if desc:
                                store.notes[song.song_id].llm_description = desc
                                store.notes[song.song_id].model = model
                                store.notes[song.song_id].enriched_at = now
                                total_enriched += 1
                    except Exception as e:
                        log.error("Batch %d failed: %s", batch_idx + 1, e)
                        console.print(
                            f"[red]Batch {batch_idx + 1} failed:[/] {e}"
                        )
                    progress.advance(task)

            console.print(f"[green]LLM enriched {total_enriched} songs[/]")

    # Save
    store.enriched_at = now
    store.save()
    console.print(
        f"[green]Teaching notes saved:[/] {len(store.notes)} entries "
        f"→ {TEACHING_NOTES_PATH}"
    )
    return store
