"""Click CLI commands for rocksmith-tutor."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .catalog import Catalog
from .config import CATALOG_PATH, CURRICULUM_PATH, DATA_DIR, DEFAULT_PSARC_DIRS, DEFAULT_MODEL
from .techniques import MANIFEST_TECHNIQUES, TECHNIQUE_DISPLAY_NAMES

console = Console()


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Rocksmith Bass Tutor — learn bass with your CDLC library."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )


@cli.command()
@click.option("--force", is_flag=True, help="Re-scan all files, ignoring cache")
@click.option(
    "--dir", "dirs", multiple=True, type=click.Path(exists=True, path_type=Path),
    help="PSARC directories to scan (can specify multiple)",
)
def scan(force: bool, dirs: tuple[Path, ...]) -> None:
    """Scan PSARC files and build the song catalog."""
    from .scanner import scan_psarcs

    scan_dirs = list(dirs) if dirs else DEFAULT_PSARC_DIRS
    catalog = scan_psarcs(dirs=scan_dirs, force=force)
    catalog.save()
    console.print(
        f"[green]Catalog saved:[/] {catalog.bass_song_count} bass songs → {CATALOG_PATH}"
    )


@cli.command()
@click.option("--technique", "-t", help="Filter by technique name")
@click.option("--artist", "-a", help="Filter by artist (substring)")
@click.option(
    "--sort", "sort_by", type=click.Choice(["difficulty", "tempo", "name"]),
    default="name", help="Sort order",
)
def catalog(technique: str | None, artist: str | None, sort_by: str) -> None:
    """Browse the song catalog."""
    cat = Catalog.load()
    if not cat.songs:
        console.print("[yellow]No catalog found. Run 'rocksmith-tutor scan' first.[/]")
        return

    songs = list(cat.songs.values())

    if technique:
        if technique not in MANIFEST_TECHNIQUES:
            console.print(f"[red]Unknown technique:[/] {technique}")
            console.print(f"Available: {', '.join(MANIFEST_TECHNIQUES)}")
            return
        songs = [s for s in songs if s.techniques.get(technique)]

    if artist:
        needle = artist.lower()
        songs = [s for s in songs if needle in s.artist.lower()]

    if sort_by == "difficulty":
        songs.sort(key=lambda s: s.difficulty_hard)
    elif sort_by == "tempo":
        songs.sort(key=lambda s: s.tempo)
    else:
        songs.sort(key=lambda s: (s.artist.lower(), s.song_name.lower()))

    table = Table(title=f"Bass Catalog ({len(songs)} songs)")
    table.add_column("Artist", style="cyan")
    table.add_column("Song", style="white")
    table.add_column("BPM", justify="right")
    table.add_column("Diff", justify="right")
    table.add_column("Notes", justify="right")
    table.add_column("Techniques", style="dim")
    table.add_column("Sections", style="dim")

    for s in songs:
        techs = ", ".join(s.technique_list()[:4])
        if len(s.technique_list()) > 4:
            techs += f" +{len(s.technique_list()) - 4}"
        table.add_row(
            s.artist,
            s.song_name,
            f"{s.tempo:.0f}",
            f"{s.difficulty_hard:.2f}",
            str(s.notes_hard),
            techs,
            s.section_summary()[:40],
        )

    console.print(table)


@cli.command()
@click.option("--model", default=None, help=f"Anthropic model (default: {DEFAULT_MODEL})")
@click.option("--artists", help="Comma-separated artist filter for smaller context")
def generate(model: str | None, artists: str | None) -> None:
    """Generate a bass learning curriculum via Anthropic API."""
    from .llm import generate_curriculum

    cat = Catalog.load()
    if not cat.songs:
        console.print("[yellow]No catalog found. Run 'rocksmith-tutor scan' first.[/]")
        return

    songs = list(cat.songs.values())
    if artists:
        needles = [a.strip().lower() for a in artists.split(",")]
        songs = [s for s in songs if any(n in s.artist.lower() for n in needles)]
        console.print(f"[dim]Filtered to {len(songs)} songs from: {artists}[/]")

    curriculum = generate_curriculum(songs, cat, model=model)
    curriculum.save()
    console.print(
        f"[green]Curriculum saved:[/] {len(curriculum.modules)} modules → {CURRICULUM_PATH}"
    )


@cli.command()
@click.option("--module", "-m", help="Filter by module ID")
def lessons(module: str | None) -> None:
    """Display the generated curriculum."""
    from .curriculum import Curriculum

    curr = Curriculum.load()
    if not curr.modules:
        console.print("[yellow]No curriculum found. Run 'rocksmith-tutor generate' first.[/]")
        return

    modules = curr.modules
    if module:
        modules = [m for m in modules if m.id == module]
        if not modules:
            console.print(f"[red]Module not found:[/] {module}")
            return

    for mod in modules:
        console.print(f"\n[bold cyan]{mod.name}[/] ({mod.id}) — {mod.skill_level}")
        for lesson in mod.lessons:
            console.print(f"  [white]{lesson.id}[/]: {lesson.name}")
            for obj in lesson.objectives:
                console.print(f"    • {obj}")
            for ex in lesson.exercises:
                console.print(
                    f"    [dim]→ {ex.song_display}, {ex.section_name} "
                    f"[{', '.join(ex.techniques_practiced)}][/]"
                )
            if lesson.notes:
                console.print(f"    [italic]{lesson.notes}[/]")


@cli.command()
@click.argument("module_id")
@click.argument("lesson_id")
def lesson(module_id: str, lesson_id: str) -> None:
    """Display a single lesson with full exercise detail."""
    from .curriculum import Curriculum

    curr = Curriculum.load()
    for mod in curr.modules:
        if mod.id == module_id:
            for les in mod.lessons:
                if les.id == lesson_id:
                    console.print(f"\n[bold cyan]{mod.name}[/] → [bold]{les.name}[/]\n")
                    console.print("[underline]Objectives:[/]")
                    for obj in les.objectives:
                        console.print(f"  • {obj}")
                    console.print("\n[underline]Exercises:[/]")
                    for i, ex in enumerate(les.exercises, 1):
                        console.print(
                            f"\n  {i}. [cyan]{ex.song_display}[/] — "
                            f"section: {ex.section_name} #{ex.section_number}"
                        )
                        console.print(f"     Techniques: {', '.join(ex.techniques_practiced)}")
                        console.print(f"     [dim]{ex.rationale}[/]")
                    if les.notes:
                        console.print(f"\n[italic]Notes: {les.notes}[/]")
                    return
    console.print(f"[red]Lesson not found:[/] {module_id}/{lesson_id}")


@cli.command()
@click.argument("question", required=False)
@click.option("--model", default=None, help=f"Anthropic model (default: {DEFAULT_MODEL})")
def ask(question: str | None, model: str | None) -> None:
    """Ask the LLM about what to practice. REPL if no question given."""
    from .llm import interactive_ask

    cat = Catalog.load()
    if not cat.songs:
        console.print("[yellow]No catalog found. Run 'rocksmith-tutor scan' first.[/]")
        return

    interactive_ask(cat, question=question, model=model)


@cli.command()
@click.argument("lesson_id")
@click.option("--jellyfin-url", envvar="JELLYFIN_URL", help="Jellyfin server URL")
@click.option("--jellyfin-key", envvar="JELLYFIN_API_KEY", help="Jellyfin API key")
def playlist(lesson_id: str, jellyfin_url: str | None, jellyfin_key: str | None) -> None:
    """Create a Jellyfin playlist from a lesson's exercises."""
    from .curriculum import Curriculum
    from .jellyfin import create_lesson_playlist

    curr = Curriculum.load()
    # Find the lesson across all modules
    for mod in curr.modules:
        for les in mod.lessons:
            if les.id == lesson_id:
                create_lesson_playlist(les, jellyfin_url=jellyfin_url, api_key=jellyfin_key)
                return

    console.print(f"[red]Lesson not found:[/] {lesson_id}")
