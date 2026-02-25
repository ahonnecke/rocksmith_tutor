"""Click CLI commands for rocksmith-tutor."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .catalog import Catalog
from .config import (
    CATALOG_PATH, CURRICULUM_PATH, DEFAULT_PSARC_DIRS, DEFAULT_MODEL,
    DEFAULT_ENRICH_MODEL, TEACHING_NOTES_PATH,
)
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

    # Load teaching notes if available
    from .teaching import TeachingNotesStore
    notes_store = TeachingNotesStore.load()
    has_notes = bool(notes_store.notes)
    if has_notes:
        console.print(f"[dim]Teaching notes: {len(notes_store.notes)} entries[/]")

    # Get recommendations
    ceiling, bounds, recs = get_recommendations(
        catalog=cat,
        profile=player,
        count=count,
        zone_filter=zone_filter,
        technique_filter=technique,
    )

    # Attach teaching notes to recommendations
    if has_notes:
        for rec in recs:
            rec.teaching_note = notes_store.display_text(rec.song.song_id)

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
    if has_notes:
        table.add_column("Why", style="italic", max_width=60)

    for i, rec in enumerate(recs, 1):
        s = rec.song
        style = zone_styles.get(rec.zone, "white")
        techs = ", ".join(s.technique_list()[:3])
        if len(s.technique_list()) > 3:
            techs += f" +{len(s.technique_list()) - 3}"
        row = [
            str(i),
            f"[{style}]{rec.zone.value}[/{style}]",
            s.artist,
            s.song_name,
            f"{s.difficulty_hard:.3f}",
            f"{s.tempo:.0f}",
            str(rec.play_count) if rec.play_count > 0 else "-",
            techs,
        ]
        if has_notes:
            row.append(rec.teaching_note or "")
        table.add_row(*row)

    console.print()
    console.print(table)


@cli.command()
@click.option("--count", "-n", default=20, help="Number of songs to show")
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
def refine(
    count: int,
    technique: str | None,
    profile_path: Path | None,
    dirs: tuple[Path, ...],
) -> None:
    """Songs you can play — focus on tone, clarity, and feel."""
    from .profile import (
        find_profile_path,
        decrypt_profile,
        load_or_build_id_map,
        parse_profile,
    )
    from .recommend import get_refinement_picks
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

    # Load teaching notes if available
    from .teaching import TeachingNotesStore
    notes_store = TeachingNotesStore.load()
    has_notes = bool(notes_store.notes)
    if has_notes:
        console.print(f"[dim]Teaching notes: {len(notes_store.notes)} entries[/]")

    # Get refinement picks
    ceiling, recs = get_refinement_picks(
        catalog=cat,
        profile=player,
        count=count,
        technique_filter=technique,
    )

    # Attach teaching notes
    if has_notes:
        for rec in recs:
            rec.teaching_note = notes_store.display_text(rec.song.song_id)

    # Display
    console.print(f"\n[bold]Comfort ceiling:[/] {ceiling:.3f}")
    console.print(
        f"[dim]Showing songs at or below {ceiling:.3f} that you've played "
        f"but not mastered — work on tone and clarity.[/]"
    )

    if not recs:
        console.print("\n[yellow]No refinement picks match the filters.[/]")
        return

    table = Table(title=f"Refine ({len(recs)} songs)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Artist", style="cyan")
    table.add_column("Song", style="white")
    table.add_column("Diff", justify="right")
    table.add_column("BPM", justify="right")
    table.add_column("Plays", justify="right")
    table.add_column("Techniques", style="dim")
    if has_notes:
        table.add_column("Why", style="italic", max_width=60)

    for i, rec in enumerate(recs, 1):
        s = rec.song
        techs = ", ".join(s.technique_list()[:3])
        if len(s.technique_list()) > 3:
            techs += f" +{len(s.technique_list()) - 3}"
        row = [
            str(i),
            s.artist,
            s.song_name,
            f"{s.difficulty_hard:.3f}",
            f"{s.tempo:.0f}",
            str(rec.play_count),
            techs,
        ]
        if has_notes:
            row.append(rec.teaching_note or "")
        table.add_row(*row)

    console.print()
    console.print(table)


@cli.command()
@click.option("--force", is_flag=True, help="Regenerate all notes (including LLM)")
@click.option(
    "--model", default=None,
    help=f"LLM model override (default: {DEFAULT_ENRICH_MODEL})",
)
@click.option("--batch-size", default=40, help="Songs per LLM call (default: 40)")
@click.option("--skip-llm", is_flag=True, help="Only compute template layer (no API cost)")
def enrich(force: bool, model: str | None, batch_size: int, skip_llm: bool) -> None:
    """Add teaching context to songs (template metadata + LLM descriptions)."""
    from .teaching import enrich_catalog

    cat = Catalog.load()
    if not cat.songs:
        console.print("[yellow]No catalog found. Run 'rocksmith-tutor scan' first.[/]")
        return

    enrich_catalog(
        catalog=cat,
        force=force,
        skip_llm=skip_llm,
        model=model,
        batch_size=batch_size,
    )


@cli.command()
@click.argument("psarc_file", type=click.Path(exists=True, path_type=Path))
def validate(psarc_file: Path) -> None:
    """Validate a PSARC for known Rocksmith failure modes."""
    from .validate import validate_psarc

    console.print(f"[dim]Validating: {psarc_file}[/]\n")
    report = validate_psarc(psarc_file)

    for check in report.checks:
        icon = "[green]\u2713[/]" if check.passed else "[red]\u2717[/]"
        detail = f"  [dim]{check.detail}[/]" if check.detail else ""
        console.print(f"  {icon} {check.name}{detail}")

    console.print()
    if report.passed:
        console.print(f"[green]All {len(report.checks)} checks passed.[/]")
    else:
        console.print(
            f"[red]{report.failed_count} of {len(report.checks)} checks failed.[/]"
        )


@cli.command()
@click.argument("song_name", required=False)
@click.option("--file", "file_path", type=click.Path(exists=True, path_type=Path),
              help="Direct path to a .psarc file")
@click.option("--min-segment", default=3.0, type=float,
              help="Minimum segment duration in seconds (default: 3.0)")
@click.option("--max-segment", default=15.0, type=float,
              help="Maximum segment duration in seconds (default: 15.0)")
@click.option("--window", default=2.0, type=float,
              help="Density analysis window size in seconds (default: 2.0)")
@click.option("--output", "-o", "output_path", type=click.Path(path_type=Path),
              help="Output path (default: {stem}_resliced{suffix})")
@click.option("--dry-run", is_flag=True,
              help="Print before/after comparison, don't write")
@click.option("--split-at", multiple=True, type=float,
              help="Force a boundary at this timestamp (seconds). Repeatable.")
def reslice(
    song_name: str | None,
    file_path: Path | None,
    min_segment: float,
    max_segment: float,
    window: float,
    output_path: Path | None,
    dry_run: bool,
    split_at: tuple[float, ...],
) -> None:
    """Re-segment a song so Riff Repeater gives smaller bites where notes are dense."""
    from .reslice import reslice_psarc

    # Resolve PSARC path
    if file_path is None and song_name is None:
        console.print("[red]Provide a song name or --file path.[/]")
        return

    psarc_path: Path | None = file_path
    if psarc_path is None:
        # Fuzzy match against catalog
        cat = Catalog.load()
        if not cat.songs:
            console.print("[yellow]No catalog found. Run 'rocksmith-tutor scan' first.[/]")
            return

        needle = song_name.lower()  # type: ignore[union-attr]
        matches = [
            s for s in cat.songs.values()
            if needle in s.song_name.lower() or needle in s.artist.lower()
        ]
        if not matches:
            console.print(f"[red]No song matching '{song_name}' in catalog.[/]")
            return
        if len(matches) > 1:
            console.print(f"[yellow]Multiple matches for '{song_name}':[/]")
            for m in matches:
                console.print(f"  {m.artist} — {m.song_name}  ({m.psarc_path})")
            console.print("[dim]Use --file to specify directly.[/]")
            return

        psarc_path = Path(matches[0].psarc_path)
        console.print(f"[dim]Matched: {matches[0].artist} — {matches[0].song_name}[/]")

    if not psarc_path.exists():
        console.print(f"[red]File not found:[/] {psarc_path}")
        return

    # Default output path
    if output_path is None:
        output_path = psarc_path.with_name(
            psarc_path.stem + "_resliced" + psarc_path.suffix
        )

    console.print(f"[dim]Input:  {psarc_path}[/]")
    if not dry_run:
        console.print(f"[dim]Output: {output_path}[/]")
    manual_splits = list(split_at) if split_at else None
    params_str = f"[dim]Params: min={min_segment}s  max={max_segment}s  window={window}s"
    if manual_splits:
        params_str += f"  splits={','.join(f'{t:.1f}' for t in manual_splits)}"
    params_str += "[/]"
    console.print(params_str)

    try:
        # First pass: get original sections for comparison
        from rocksmith.psarc import PSARC as _PSARC
        from rocksmith.sng import Song as _Song
        from .reslice import _find_bass_sng_key

        with open(psarc_path, "rb") as f:
            content = _PSARC(crypto=True).parse_stream(f)
        sng_key = _find_bass_sng_key(content)
        if sng_key:
            sng = _Song.parse(content[sng_key])
            orig_sections = list(sng.sections)
        else:
            orig_sections = []

        boundaries = reslice_psarc(
            psarc_path=psarc_path,
            output_path=output_path,
            min_segment=min_segment,
            max_segment=max_segment,
            window=window,
            dry_run=dry_run,
            manual_splits=manual_splits,
        )
    except Exception as e:
        console.print(f"[red]Reslice failed:[/] {e}")
        raise

    # Display comparison
    console.print()

    # Before table
    if orig_sections:
        table_before = Table(title="Original Sections")
        table_before.add_column("#", justify="right", style="dim")
        table_before.add_column("Name", style="cyan")
        table_before.add_column("Start", justify="right")
        table_before.add_column("End", justify="right")
        table_before.add_column("Duration", justify="right")
        for i, s in enumerate(orig_sections, 1):
            dur = s.endTime - s.startTime
            table_before.add_row(
                str(i), s.name,
                f"{s.startTime:.1f}s", f"{s.endTime:.1f}s", f"{dur:.1f}s",
            )
        console.print(table_before)
        console.print()

    # After table
    table_after = Table(
        title=f"{'Proposed' if dry_run else 'New'} Segments ({len(boundaries)} boundaries)"
    )
    table_after.add_column("#", justify="right", style="dim")
    table_after.add_column("Name", style="cyan")
    table_after.add_column("Start", justify="right")
    table_after.add_column("End", justify="right")
    table_after.add_column("Duration", justify="right")

    for i in range(len(boundaries) - 1):
        b = boundaries[i]
        end = boundaries[i + 1].time
        dur = end - b.time
        table_after.add_row(
            str(i + 1), b.name,
            f"{b.time:.1f}s", f"{end:.1f}s", f"{dur:.1f}s",
        )
    console.print(table_after)

    if dry_run:
        console.print("\n[dim]Dry run — no file written.[/]")
    else:
        console.print(f"\n[green]Written:[/] {output_path}")
