"""
Search service.

Delegates to db.queries.search3 which uses SQLite FTS5 for fast full-text
search across title, artist, album and genre. Falls back to a regular
SELECT for empty queries (Feishin fetches all songs on startup this way).

The FTS5 virtual table (virt_fts5) is created by migration 007 and kept
in sync by INSERT/UPDATE/DELETE triggers on the tracks table.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.db import queries
from .library import make_album_id, make_artist_id, track_to_subsonic


def search3(
    query: str,
    artist_count: int,
    album_count: int,
    song_count: int,
    artist_offset: int = 0,
    album_offset: int = 0,
    song_offset: int = 0,
) -> Dict[str, Any]:
    """Return a Subsonic-shaped search result."""
    raw = queries.search3(
        query,
        artist_count,
        album_count,
        song_count,
        artist_offset,
        album_offset,
        song_offset,
    )

    artists = [
        {
            "id": make_artist_id(a["id"]),
            "name": a["name"],
            "albumCount": a["album_count"],
        }
        for a in raw["artists"]
    ]
    albums = [
        {
            "id": make_album_id(al["id"]),
            "name": al["name"],
            "title": al["name"],
            "artist": al["artist_name"],
            "artistId": make_artist_id(al["artist_id"]),
            "year": al.get("year"),
            "coverArt": al.get("cover_art_id"),
            "songCount": al.get("track_count"),
            "duration": al.get("duration"),
        }
        for al in raw["albums"]
    ]
    songs = [track_to_subsonic(s) for s in raw["songs"]]

    return {"artist": artists, "album": albums, "song": songs}
