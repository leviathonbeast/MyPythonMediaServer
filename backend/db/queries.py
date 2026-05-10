"""
All non-trivial SQL lives here.

WHY centralise queries:
    1. Easy to audit indexes — every query is visible in one file.
    2. Easy to optimise — we know exactly where the hot paths are.
    3. Easy to migrate to a different DB later — just rewrite this module.

Conventions:
    * Functions return plain dicts (or lists thereof). The API layer turns
      dicts into Subsonic XML/JSON responses.
    * Functions never commit on their own when called inside a transaction()
      context — that's the caller's job.
    * Read functions are pure (don't mutate state).

Performance notes:
    * Every WHERE/ORDER BY column is indexed (see schema.sql).
    * Browse endpoints LIMIT aggressively. We never return all 100k tracks at
      once, only what the user asked for.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .connection import get_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Convert a sqlite3.Row to a plain dict (or None passthrough)."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [{k: row[k] for k in row.keys()} for row in rows]


# ---------------------------------------------------------------------------
# Music folders
# ---------------------------------------------------------------------------

def list_music_folders() -> List[Dict[str, Any]]:
    """All configured music folders. Used by getMusicFolders."""
    rows = get_conn().execute("SELECT id, name, path FROM music_folders ORDER BY name").fetchall()
    return _rows_to_dicts(rows)


def list_music_folders_with_counts() -> List[Dict[str, Any]]:
    """List folders enriched with their track count.

    Used by the admin UI so the user knows how many tracks they're about
    to lose if they remove a folder. We use a LEFT JOIN so folders with
    zero tracks still appear (track_count = 0). The aggregate is cheap
    against the (music_folder_id) index.
    """
    rows = get_conn().execute("""
        SELECT f.id, f.name, f.path, COUNT(t.id) AS track_count
        FROM music_folders f
        LEFT JOIN tracks t ON t.music_folder_id = f.id
        GROUP BY f.id
        ORDER BY f.name
    """).fetchall()
    return _rows_to_dicts(rows)


def get_music_folder(folder_id: int) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        "SELECT id, name, path FROM music_folders WHERE id = ?", (folder_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_music_folder_by_path(path: str) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        "SELECT id, name, path FROM music_folders WHERE path = ?", (path,)
    ).fetchone()
    return _row_to_dict(row)


def add_music_folder(name: str, path: str) -> int:
    """Insert a new music folder and return its id.

    Caller is expected to validate `path` (existence, readability) before
    calling — this layer just enforces the UNIQUE constraint on path,
    raising sqlite3.IntegrityError on duplicates.
    """
    cur = get_conn().execute(
        "INSERT INTO music_folders (name, path) VALUES (?, ?)",
        (name, path),
    )
    return cur.lastrowid


def delete_music_folder(folder_id: int) -> bool:
    """Remove a music folder. Tracks under it are cascade-deleted by FK.

    Returns True if a row was deleted, False if no folder existed at that id.
    Aggregate cleanup of newly-empty albums/artists is the GC's job, not
    ours — call run_gc() afterwards.
    """
    cur = get_conn().execute("DELETE FROM music_folders WHERE id = ?", (folder_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------

def upsert_artist(name: str, sort_name: Optional[str] = None) -> int:
    """
    Insert artist if missing, return id.

    Used by the scanner. Lower-case dedup key means "AC/DC" and "ac/dc" merge.
    """
    name_lower = name.strip().lower()
    sort_name = sort_name or name
    conn = get_conn()
    # The UNIQUE on name_lower lets us use ON CONFLICT to dedup atomically.
    conn.execute(
        """
        INSERT INTO artists (name, name_lower, sort_name)
        VALUES (?, ?, ?)
        ON CONFLICT(name_lower) DO NOTHING
        """,
        (name, name_lower, sort_name),
    )
    row = conn.execute("SELECT id FROM artists WHERE name_lower = ?", (name_lower,)).fetchone()
    return int(row["id"])


def list_artists_indexed() -> Dict[str, List[Dict[str, Any]]]:
    """
    Return artists grouped by their first letter (A, B, C, ...).

    Subsonic's getIndexes wants this exact shape. Numbers and symbols go
    into a "#" bucket.
    """
    rows = get_conn().execute(
        """
        SELECT a.id, a.name, COUNT(al.id) AS album_count,
               COALESCE(a.sort_name, a.name) AS sort_name,
               (SELECT al2.cover_art_id
                  FROM albums al2
                 WHERE al2.artist_id = a.id
                   AND al2.cover_art_id IS NOT NULL
                 ORDER BY al2.year DESC, al2.created_at DESC
                 LIMIT 1) AS cover_art_id
          FROM artists a
          JOIN albums al ON al.artist_id = a.id
         GROUP BY a.id, a.name, a.sort_name
         ORDER BY sort_name COLLATE NOCASE
        """
    ).fetchall()

    indexed: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        # The "index letter" is the first alpha char of the sort name. Stripping
        # leading "The " is a common Subsonic-ism but we leave that to sort_name.
        key = (row["sort_name"][:1] or "#").upper()
        if not key.isalpha():
            key = "#"
        indexed.setdefault(key, []).append({
            "id":          row["id"],
            "name":        row["name"],
            "albumCount":  row["album_count"],
            "coverArtId":  row["cover_art_id"],
        })
    return indexed


def get_artist(artist_id: int) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        "SELECT id, name, album_count FROM artists WHERE id = ?", (artist_id,)
    ).fetchone()
    return _row_to_dict(row)


def list_artist_albums(artist_id: int) -> List[Dict[str, Any]]:
    """All albums by an artist, ordered by year then name."""
    rows = get_conn().execute(
        """
        SELECT id, name, year, genre, release_type, track_count, duration,
               cover_art_id, created_at
          FROM albums
         WHERE artist_id = ?
         ORDER BY year, name COLLATE NOCASE
        """,
        (artist_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------

def upsert_album(
    artist_id: int,
    name: str,
    year: Optional[int] = None,
    genre: Optional[str] = None,
    release_type: Optional[str] = None,
) -> int:
    """Insert album if missing, return id.

    `release_type` follows MusicBrainz primary types ("album", "ep",
    "single", "compilation", "live", ...). NULL means "not tagged" and
    is treated as "album" for grouping purposes by the artist page.
    We use COALESCE on update so a later track that DOES have the tag
    populates an album initially seeded from a tag-less file.
    """
    name_lower = name.strip().lower()
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO albums (artist_id, name, name_lower, year, genre, release_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artist_id, name_lower) DO UPDATE SET
            year         = COALESCE(excluded.year,         albums.year),
            genre        = COALESCE(excluded.genre,        albums.genre),
            release_type = COALESCE(excluded.release_type, albums.release_type)
        """,
        (artist_id, name, name_lower, year, genre, release_type, now),
    )
    row = conn.execute(
        "SELECT id FROM albums WHERE artist_id = ? AND name_lower = ?",
        (artist_id, name_lower),
    ).fetchone()
    return int(row["id"])


def get_album(album_id: int) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        """
        SELECT al.id, al.name, al.year, al.genre, al.release_type, al.track_count,
               al.duration, al.cover_art_id, al.created_at, al.artist_id,
               ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         WHERE al.id = ?
        """,
        (album_id,),
    ).fetchone()
    return _row_to_dict(row)


def list_albums(
    list_type: str = "alphabeticalByName",
    size: int = 10,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Implements getAlbumList's list types. Subsonic supports many; we ship the
    most useful ones and stub the others to alpha-by-name.

    NOTE: random uses ORDER BY RANDOM() which is fine for libraries up to a
    few hundred thousand albums but not great beyond that. For huge libraries,
    pre-compute a random sample table or use rowid sampling.
    """
    order_clause = {
        "newest":             "al.created_at DESC",
        "alphabeticalByName": "al.name COLLATE NOCASE ASC",
        "alphabeticalByArtist": "ar.name COLLATE NOCASE ASC, al.year ASC",
        "byYear":             "al.year DESC, al.name COLLATE NOCASE ASC",
        "byGenre":            "al.genre COLLATE NOCASE ASC, al.name COLLATE NOCASE ASC",
        "random":             "RANDOM()",
    }.get(list_type, "al.name COLLATE NOCASE ASC")

    rows = get_conn().execute(
        f"""
        SELECT al.id, al.name, al.year, al.genre, al.track_count, al.duration,
               al.cover_art_id, al.created_at, al.artist_id, ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         ORDER BY {order_clause}
         LIMIT ? OFFSET ?
        """,
        (size, offset),
    ).fetchall()
    return _rows_to_dicts(rows)


def update_album_aggregates(album_id: int) -> None:
    """
    Recompute (track_count, duration) for an album.

    We denormalize these on the album row so browsing 1000 albums doesn't run
    1000 COUNT(*) queries. Called by the scanner whenever tracks change.
    """
    conn = get_conn()
    conn.execute(
        """
        UPDATE albums
           SET track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = ?),
               duration    = (SELECT COALESCE(SUM(duration), 0) FROM tracks WHERE album_id = ?)
         WHERE id = ?
        """,
        (album_id, album_id, album_id),
    )


def update_artist_aggregates(artist_id: int) -> None:
    """Recompute album_count for an artist. Same rationale as above."""
    conn = get_conn()
    conn.execute(
        "UPDATE artists SET album_count = (SELECT COUNT(*) FROM albums WHERE artist_id = ?) WHERE id = ?",
        (artist_id, artist_id),
    )


def set_album_cover_art(album_id: int, cover_art_id: str) -> None:
    get_conn().execute("UPDATE albums SET cover_art_id = ? WHERE id = ?", (cover_art_id, album_id))


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def upsert_track(track: Dict[str, Any]) -> int:
    """
    Insert or update a track row by path.

    The scanner builds the dict; we just persist it. Returning the id lets
    the scanner accumulate album_id->track-count maps in memory.
    """
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO tracks (
            album_id, artist_id, music_folder_id, path, title,
            track_number, disc_number, duration, bitrate, size,
            suffix, content_type, year, genre,
            mtime, content_hash, last_scanned
        ) VALUES (
            :album_id, :artist_id, :music_folder_id, :path, :title,
            :track_number, :disc_number, :duration, :bitrate, :size,
            :suffix, :content_type, :year, :genre,
            :mtime, :content_hash, :last_scanned
        )
        ON CONFLICT(path) DO UPDATE SET
            album_id     = excluded.album_id,
            artist_id    = excluded.artist_id,
            title        = excluded.title,
            track_number = excluded.track_number,
            disc_number  = excluded.disc_number,
            duration     = excluded.duration,
            bitrate      = excluded.bitrate,
            size         = excluded.size,
            suffix       = excluded.suffix,
            content_type = excluded.content_type,
            year         = excluded.year,
            genre        = excluded.genre,
            mtime        = excluded.mtime,
            content_hash = excluded.content_hash,
            last_scanned = excluded.last_scanned
        """,
        track,
    )
    row = conn.execute("SELECT id FROM tracks WHERE path = ?", (track["path"],)).fetchone()
    return int(row["id"])


def get_track(track_id: int) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        """
        SELECT t.*, ar.name AS artist_name, al.name AS album_name, al.cover_art_id AS cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE t.id = ?
        """,
        (track_id,),
    ).fetchone()
    return _row_to_dict(row)


def list_album_tracks(album_id: int) -> List[Dict[str, Any]]:
    """
    Tracks on an album, in disc/track order.

    The composite index (album_id, disc_number, track_number) makes this an
    index-only scan — no sort, no temp table, even for 100k+ track libraries.
    """
    rows = get_conn().execute(
        """
        SELECT t.id, t.title, t.track_number, t.disc_number, t.duration, t.bitrate,
               t.size, t.suffix, t.content_type, t.year, t.genre, t.path,
               t.artist_id, ar.name AS artist_name,
               t.album_id,  al.name AS album_name, al.cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE t.album_id = ?
         ORDER BY t.disc_number, t.track_number, t.title COLLATE NOCASE
        """,
        (album_id,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_existing_paths_for_folder(folder_id: int) -> Dict[str, Tuple[int, int, int]]:
    """
    Return {path: (id, mtime, size)} for every track in this folder.

    Scanner uses this to decide which files to skip (mtime+size unchanged) and
    which to remove (path no longer on disk). Crucial for incremental scanning
    on large libraries — we never re-parse a file that hasn't changed.
    """
    rows = get_conn().execute(
        "SELECT id, path, mtime, size FROM tracks WHERE music_folder_id = ?",
        (folder_id,),
    ).fetchall()
    return {row["path"]: (row["id"], row["mtime"], row["size"]) for row in rows}


def delete_tracks(track_ids: List[int]) -> None:
    """Remove tracks (e.g. files deleted on disk). Caller handles aggregates."""
    if not track_ids:
        return
    conn = get_conn()
    # SQLite has a limit on the number of host params; chunk if needed. 500 is
    # safely under SQLITE_MAX_VARIABLE_NUMBER (default 999 / 32766 newer).
    for i in range(0, len(track_ids), 500):
        chunk = track_ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", chunk)


def cleanup_empty_albums_and_artists() -> Tuple[int, int]:
    """
    Remove albums with 0 tracks and artists with 0 albums.

    Run after a scan that deleted tracks. Returns (albums_deleted, artists_deleted).
    """
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)"
    )
    albums_deleted = cur.rowcount
    cur = conn.execute(
        "DELETE FROM artists WHERE id NOT IN (SELECT DISTINCT artist_id FROM albums) AND id NOT IN (SELECT DISTINCT artist_id FROM tracks WHERE artist_id IS NOT NULL)"
    )
    artists_deleted = cur.rowcount
    return albums_deleted, artists_deleted


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search3(
    query: str,
    artist_count: int = 20,
    album_count: int = 20,
    song_count: int = 20,
    artist_offset: int = 0,
    album_offset: int = 0,
    song_offset: int = 0,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Subsonic-style search. Returns artists, albums, songs that match.

    NOTE: This uses LIKE with leading wildcard which is index-unfriendly.
    For 500k tracks this gets slow. The right answer is a proper FTS5 virtual
    table — see core/search.py for that path. We keep this LIKE version as a
    fallback for installs that haven't built FTS yet.
    """
    pattern = f"%{query}%"
    conn = get_conn()

    artists = conn.execute(
        "SELECT id, name, album_count FROM artists WHERE name LIKE ? COLLATE NOCASE LIMIT ? OFFSET ?",
        (pattern, artist_count, artist_offset),
    ).fetchall()

    albums = conn.execute(
        """
        SELECT al.id, al.name, al.year, al.cover_art_id, al.track_count, al.duration,
               al.artist_id, ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         WHERE al.name LIKE ? COLLATE NOCASE
            OR ar.name LIKE ? COLLATE NOCASE
         LIMIT ? OFFSET ?
        """,
        (pattern, pattern, album_count, album_offset),
    ).fetchall()

    songs = conn.execute(
        """
        SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
               t.track_number, t.year, t.genre, t.path,
               t.artist_id, ar.name AS artist_name,
               t.album_id, al.name AS album_name, al.cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums al  ON al.id = t.album_id
         WHERE t.title LIKE ? COLLATE NOCASE
            OR ar.name LIKE ? COLLATE NOCASE
            OR al.name LIKE ? COLLATE NOCASE
         LIMIT ? OFFSET ?
        """,
        (pattern, pattern, pattern, song_count, song_offset),
    ).fetchall()

    return {
        "artists": _rows_to_dicts(artists),
        "albums":  _rows_to_dicts(albums),
        "songs":   _rows_to_dicts(songs),
    }


# ---------------------------------------------------------------------------
# Users (auth)
# ---------------------------------------------------------------------------

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    return _row_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    row = get_conn().execute(
        "SELECT id, username, is_admin FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Stats (used by /api/stats and Subsonic placeholders)
# ---------------------------------------------------------------------------

def library_stats() -> Dict[str, int]:
    """Top-level counts for the admin/settings page.

    We surface total runtime alongside the row counts because it makes a
    big library feel real — "12,340 tracks / 41 days of music" reads
    very differently from "12,340 tracks". The duration sum is cheap on
    indexed columns even at 500k rows.
    """
    conn = get_conn()
    duration_row = conn.execute(
        "SELECT COALESCE(SUM(duration), 0) FROM tracks"
    ).fetchone()
    return {
        "artists": conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
        "albums":  conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
        "tracks":  conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],
        "total_duration_seconds": int(duration_row[0] or 0),
    }
