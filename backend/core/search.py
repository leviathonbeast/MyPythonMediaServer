"""
Search service.

Currently delegates to db.queries.search3 (LIKE-based). For larger libraries
we'd want SQLite FTS5: a virtual table tracks_fts mirroring (title, artist,
album) populated by triggers. The query path then becomes:

    SELECT t.* FROM tracks t
      JOIN tracks_fts f ON f.rowid = t.id
     WHERE tracks_fts MATCH :query

That's roughly 100x faster on a million-row library. We've left a TODO at
the bottom — the schema is the only thing that needs touching.
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
    raw = queries.search3(query, artist_count, album_count, song_count,
                          artist_offset, album_offset, song_offset)

    artists = [
        {"id": make_artist_id(a["id"]), "name": a["name"], "albumCount": a["album_count"]}
        for a in raw["artists"]
    ]
    albums = [
        {
            "id":        make_album_id(al["id"]),
            "name":      al["name"],
            "title":     al["name"],
            "artist":    al["artist_name"],
            "artistId":  make_artist_id(al["artist_id"]),
            "year":      al.get("year"),
            "coverArt":  al.get("cover_art_id"),
            "songCount": al.get("track_count"),
            "duration":  al.get("duration"),
        }
        for al in raw["albums"]
    ]
    songs = [track_to_subsonic(s) for s in raw["songs"]]

    return {"artist": artists, "album": albums, "song": songs}


# TODO: add FTS5 virtual table + triggers in a future migration to make this
# scale to multi-million-track libraries. The function signature above doesn't
# need to change — just rewrite db.queries.search3.
