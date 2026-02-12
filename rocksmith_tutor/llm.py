"""Anthropic API integration for curriculum generation and interactive Q&A."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import yaml
from anthropic import Anthropic
from rich.console import Console
from rich.markdown import Markdown

from .catalog import Catalog, SongEntry
from .curriculum import Curriculum, Module, Lesson, Exercise
from .config import DEFAULT_MODEL
from .techniques import SKILL_GROUPS

log = logging.getLogger(__name__)
console = Console()

SYSTEM_PROMPT = """\
You are a bass guitar teacher creating a progressive curriculum using \
Rocksmith 2014 CDLC songs. Your student has a library of songs with \
bass arrangements that you can reference by exact name and section.

Guidelines:
- Order modules from fundamentals to advanced techniques.
- Each lesson should have 3-6 exercises using specific songs and sections.
- Reference songs as "Artist - Song, section_name section #N".
- Include a rationale for each exercise explaining what to focus on.
- Prefer songs with lower difficulty for beginner modules.
- Use tempo and difficulty data to order exercises within lessons.
- Group songs that share technique requirements into the same lesson.
"""

GENERATE_PROMPT_TEMPLATE = """\
Here is my Rocksmith bass song catalog ({count} songs). Generate a \
structured bass learning curriculum in YAML format.

Use these skill group categories (in order):
{skill_groups}

Song catalog (one song per line):
{catalog_lines}

Output valid YAML matching this structure exactly:
```yaml
version: 1
generated_at: "{timestamp}"
modules:
  - id: "module_id"
    name: "Module Name"
    order: 1
    skill_level: "beginner"
    prerequisites: []
    lessons:
      - id: "lesson_id"
        name: "Lesson Name"
        objectives:
          - "Objective 1"
        exercises:
          - song_id: "songid_bass"
            song_display: "Artist - Song"
            section_name: "intro"
            section_number: 1
            rationale: "Why this exercise"
            techniques_practiced:
              - "technique1"
        notes: "Optional teacher notes"
```

Important:
- Use the exact song_id values from the catalog.
- Every song_id must correspond to a real song in the catalog.
- Create at least one module per skill group that has songs.
- Each lesson should have 3-6 exercises.
- Order exercises from easiest to hardest within each lesson.
"""


def _build_catalog_lines(songs: list[SongEntry]) -> str:
    """Build compact one-line-per-song catalog for LLM context."""
    lines = []
    for s in sorted(songs, key=lambda s: (s.artist.lower(), s.song_name.lower())):
        lines.append(f"{s.song_id} | {s.one_line_summary()}")
    return "\n".join(lines)


def _build_skill_groups_desc() -> str:
    """Build skill groups description for the prompt."""
    lines = []
    for gid, g in sorted(SKILL_GROUPS.items(), key=lambda x: x[1]["order"]):
        techs = ", ".join(g["techniques"])
        lines.append(f"{g['order']}. {g['name']} ({gid}): {techs}")
    return "\n".join(lines)


def _parse_curriculum_yaml(raw: str, songs_by_id: dict[str, SongEntry]) -> Curriculum:
    """Parse LLM-generated YAML into Curriculum, validating song references."""
    # Extract YAML from markdown code fence if present
    if "```yaml" in raw:
        raw = raw.split("```yaml", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]

    data = yaml.safe_load(raw)
    if not data or not isinstance(data, dict):
        raise ValueError("LLM returned invalid YAML")

    modules = []
    warnings = 0
    for m in data.get("modules", []):
        lessons = []
        for les in m.get("lessons", []):
            exercises = []
            for ex in les.get("exercises", []):
                sid = ex.get("song_id", "")
                if sid not in songs_by_id:
                    log.warning("Song ID not in catalog: %s", sid)
                    warnings += 1
                exercises.append(Exercise(
                    song_id=sid,
                    song_display=ex.get("song_display", sid),
                    section_name=ex.get("section_name", ""),
                    section_number=int(ex.get("section_number", 1)),
                    rationale=ex.get("rationale", ""),
                    techniques_practiced=ex.get("techniques_practiced", []),
                ))
            lessons.append(Lesson(
                id=les.get("id", ""),
                name=les.get("name", ""),
                objectives=les.get("objectives", []),
                exercises=exercises,
                notes=les.get("notes", ""),
            ))
        modules.append(Module(
            id=m.get("id", ""),
            name=m.get("name", ""),
            order=m.get("order", 0),
            skill_level=m.get("skill_level", ""),
            prerequisites=m.get("prerequisites", []),
            lessons=lessons,
        ))

    if warnings:
        console.print(f"[yellow]Warning: {warnings} song ID(s) not found in catalog[/]")

    return Curriculum(
        version=data.get("version", 1),
        generated_at=data.get("generated_at", datetime.now(timezone.utc).isoformat()),
        modules=modules,
    )


def generate_curriculum(
    songs: list[SongEntry],
    catalog: Catalog,
    model: str | None = None,
) -> Curriculum:
    """Call Anthropic API to generate a structured curriculum."""
    model = model or DEFAULT_MODEL
    client = Anthropic()

    catalog_lines = _build_catalog_lines(songs)
    skill_groups = _build_skill_groups_desc()
    timestamp = datetime.now(timezone.utc).isoformat()

    user_prompt = GENERATE_PROMPT_TEMPLATE.format(
        count=len(songs),
        skill_groups=skill_groups,
        catalog_lines=catalog_lines,
        timestamp=timestamp,
    )

    console.print(f"[dim]Sending {len(songs)} songs to {model}...[/]")

    response = client.messages.create(
        model=model,
        max_tokens=16384,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = response.content[0].text
    songs_by_id = {s.song_id: s for s in songs}

    console.print(f"[dim]Parsing curriculum ({response.usage.input_tokens} in, "
                  f"{response.usage.output_tokens} out)...[/]")

    return _parse_curriculum_yaml(raw_text, songs_by_id)


def interactive_ask(
    catalog: Catalog,
    question: str | None = None,
    model: str | None = None,
) -> None:
    """Interactive Q&A about what to practice. REPL if no question given."""
    model = model or DEFAULT_MODEL
    client = Anthropic()

    songs = list(catalog.songs.values())
    catalog_context = _build_catalog_lines(songs)

    # Try to load existing curriculum
    from .curriculum import Curriculum as CurriculumCls
    curr = CurriculumCls.load()
    curriculum_context = ""
    if curr.modules:
        curriculum_context = (
            "\n\nCurrent curriculum modules: "
            + ", ".join(f"{m.name} ({len(m.lessons)} lessons)" for m in curr.modules)
        )

    system = (
        SYSTEM_PROMPT
        + f"\n\nSong catalog ({len(songs)} songs):\n{catalog_context}"
        + curriculum_context
        + "\n\nAnswer questions about what to practice, referencing specific songs and sections."
    )

    messages: list[dict] = []

    def ask_once(q: str) -> None:
        messages.append({"role": "user", "content": q})
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        console.print(Markdown(reply))

    if question:
        ask_once(question)
        return

    # REPL mode
    console.print("[bold]Bass Tutor[/] — ask about what to practice (Ctrl-D to exit)\n")
    while True:
        try:
            q = console.input("[bold cyan]> [/]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/]")
            break
        if not q.strip():
            continue
        ask_once(q.strip())
        console.print()
