"""Click CLI commands for rocksmith-tutor."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .catalog import Catalog
from .config import CATALOG_PATH, CURRICULUM_PATH, DEFAULT_PSARC_DIRS, DEFAULT_MODEL
from .recommend import Zone
from .techniques import MANIFEST_TECHNIQUES

ZONE_NAMES = [z.value for z in Zone]

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
@click.option("--count", "-n", default=20, help="Number of recommendations")
@click.option(
    "--zone", "-z", "zone_name",
    type=click.Choice(ZONE_NAMES, case_sensitive=False),
    help="Filter to a specific zone",
)
@click.option("--technique", "-t", help="Filter by technique name")
@click.option(
    "--profile", "profile_path",
    type=click.Path(exists=True, path_type=Path),
    help="Path to *_PRFLDB save file (auto-detected if omitted)",
)
@click.option(
    "--dir", "dirs", multiple=True, type=click.Path(exists=True, path_type=Path),
    help="PSARC directories for ID map (uses defaults if omitted)",
)
def recommend(
    count: int,
    zone_name: str | None,
    technique: str | None,
    profile_path: Path | None,
    dirs: tuple[Path, ...],
) -> None:
    """Recommend songs slightly harder than what you can play."""
    from .profile import (
        find_profile_path,
        decrypt_profile,
        load_or_build_id_map,
        parse_profile,
    )
    from .recommend import get_recommendations, Zone
    from .scanner import scan_psarcs

    # Validate technique
    if technique and technique not in MANIFEST_TECHNIQUES:
        console.print(f"[red]Unknown technique:[/] {technique}")
        console.print(f"Available: {', '.join(MANIFEST_TECHNIQUES)}")
        return

    # Load or auto-scan catalog
    cat = Catalog.load()
    if not cat.songs:
        console.print("[yellow]No catalog found, running scan...[/]")
        scan_dirs = list(dirs) if dirs else DEFAULT_PSARC_DIRS
        cat = scan_psarcs(dirs=scan_dirs)
        cat.save()
        console.print(f"[green]Catalog built:[/] {cat.bass_song_count} bass songs")

    # Find profile save file
    if profile_path is None:
        profile_path = find_profile_path()
        if profile_path is None:
            console.print(
                "[red]No PRFLDB save file found.[/] "
                "Use --profile to specify the path."
            )
            return
    console.print(f"[dim]Profile: {profile_path}[/]")

    # Build/load ID map
    psarc_dirs = list(dirs) if dirs else DEFAULT_PSARC_DIRS
    id_map = load_or_build_id_map(psarc_dirs=psarc_dirs)
    if not id_map:
        console.print("[red]No ID map could be built. Are PSARC dirs correct?[/]")
        return
    console.print(f"[dim]ID map: {len(id_map)} entries[/]")

    # Decrypt and parse profile
    try:
        profile_json = decrypt_profile(profile_path)
    except Exception as e:
        console.print(f"[red]Failed to decrypt profile:[/] {e}")
        return

    player = parse_profile(profile_json, id_map)
    console.print(
        f"[dim]Profile: {len(player.songs)} songs tracked, "
        f"{len(player.competent_song_ids)} competent, "
        f"{len(player.played_song_ids)} played[/]"
    )

    # Resolve zone filter
    zone_filter: Zone | None = None
    if zone_name:
        zone_filter = Zone(zone_name)

    # Get recommendations
    ceiling, bounds, recs = get_recommendations(
        catalog=cat,
        profile=player,
        count=count,
        zone_filter=zone_filter,
        technique_filter=technique,
    )

    # Display comfort zone info
    console.print(f"\n[bold]Comfort ceiling:[/] {ceiling:.3f}")
    for zone, zb in bounds.items():
        console.print(f"  {zone.value:>10s}: {zb.lo:.3f} – {zb.hi:.3f}")

    if not recs:
        console.print("\n[yellow]No recommendations match the filters.[/]")
        return

    # Build table
    zone_styles = {
        Zone.WARMUP: "green",
        Zone.GROWTH: "cyan",
        Zone.CHALLENGE: "yellow",
        Zone.REACH: "red",
    }

    table = Table(title=f"Recommendations ({len(recs)} songs)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Zone", style="bold")
    table.add_column("Artist", style="cyan")
    table.add_column("Song", style="white")
    table.add_column("Diff", justify="right")
    table.add_column("BPM", justify="right")
    table.add_column("Plays", justify="right")
    table.add_column("Techniques", style="dim")

    for i, rec in enumerate(recs, 1):
        s = rec.song
        style = zone_styles.get(rec.zone, "white")
        techs = ", ".join(s.technique_list()[:3])
        if len(s.technique_list()) > 3:
            techs += f" +{len(s.technique_list()) - 3}"
        table.add_row(
            str(i),
            f"[{style}]{rec.zone.value}[/{style}]",
            s.artist,
            s.song_name,
            f"{s.difficulty_hard:.3f}",
            f"{s.tempo:.0f}",
            str(rec.play_count) if rec.play_count > 0 else "-",
            techs,
        )

    console.print()
    console.print(table)


