"""Jellyfin playlist creation from lesson exercises."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from rich.console import Console

from .config import JELLYFIN_URL, JELLYFIN_API_KEY
from .curriculum import Lesson

log = logging.getLogger(__name__)
console = Console()


def _api_get(url: str, api_key: str) -> dict | list | None:
    """Make a GET request to Jellyfin API."""
    separator = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{separator}api_key={api_key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log.debug("Jellyfin API error: %s", e)
        return None


def _get_user_id(base_url: str, api_key: str) -> str | None:
    """Get the first Jellyfin user ID."""
    users = _api_get(f"{base_url}/Users", api_key)
    if isinstance(users, list) and users:
        return users[0]["Id"]
    return None


def _search_song(base_url: str, api_key: str, user_id: str,
                 artist: str, title: str) -> str | None:
    """Search Jellyfin for a song by artist and title. Returns item ID or None."""
    query = urllib.parse.quote(f"{artist} {title}")
    url = (f"{base_url}/Users/{user_id}/Items"
           f"?searchTerm={query}&IncludeItemTypes=Audio&Recursive=true&Limit=5")
    result = _api_get(url, api_key)
    if isinstance(result, dict):
        items = result.get("Items", [])
        for item in items:
            item_artist = (item.get("AlbumArtist") or "").lower()
            item_name = (item.get("Name") or "").lower()
            if artist.lower() in item_artist and title.lower() in item_name:
                return item["Id"]
        if items:
            return items[0]["Id"]
    return None


def _create_playlist(base_url: str, api_key: str, user_id: str,
                     name: str, item_ids: list[str]) -> str | None:
    """Create a Jellyfin playlist and return its ID."""
    ids = ",".join(item_ids)
    url = (f"{base_url}/Playlists"
           f"?Name={urllib.parse.quote(name)}&Ids={ids}&userId={user_id}")
    req = urllib.request.Request(f"{url}&api_key={api_key}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("Id")
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log.error("Failed to create playlist: %s", e)
        return None


def create_lesson_playlist(
    lesson: Lesson,
    jellyfin_url: str | None = None,
    api_key: str | None = None,
) -> None:
    """Create a Jellyfin playlist from a lesson's exercises."""
    base_url = jellyfin_url or JELLYFIN_URL
    api_key = api_key or JELLYFIN_API_KEY

    if not api_key:
        console.print("[red]JELLYFIN_API_KEY not set[/]")
        return

    user_id = _get_user_id(base_url, api_key)
    if not user_id:
        console.print("[red]Could not get Jellyfin user ID[/]")
        return

    item_ids = []
    for ex in lesson.exercises:
        parts = ex.song_display.split(" - ", 1)
        if len(parts) != 2:
            console.print(f"[yellow]Skipping: {ex.song_display} (can't parse artist/title)[/]")
            continue
        artist, title = parts
        item_id = _search_song(base_url, api_key, user_id, artist.strip(), title.strip())
        if item_id:
            item_ids.append(item_id)
            console.print(f"  [green]Found:[/] {ex.song_display}")
        else:
            console.print(f"  [yellow]Not found:[/] {ex.song_display}")

    if not item_ids:
        console.print("[red]No songs found in Jellyfin[/]")
        return

    playlist_name = f"Bass Lesson: {lesson.name}"
    playlist_id = _create_playlist(base_url, api_key, user_id, playlist_name, item_ids)
    if playlist_id:
        console.print(f"[green]Playlist created:[/] {playlist_name} ({len(item_ids)} songs)")
    else:
        console.print("[red]Failed to create playlist[/]")
