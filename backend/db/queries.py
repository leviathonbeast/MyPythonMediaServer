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

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# starred
# ---------------------------------------------------------------------------


def star_item(user_id: int, target_type: str, target_id: int) -> None:
    """star item"""

    rows = get_conn().execute(
        """INSERT OR IGNORE INTO starred (user_id, target_type, target_id, starred_at) 
        VALUES (?, ?, ?, ?)
        """,
        (user_id, target_type, target_id, int(time.time())),
    )


def unstar_item(user_id: int, target_type: str, target_id: int) -> None:
    """unstar item"""

    row = get_conn().execute(
        """
    DELETE FROM starred WHERE user_id = ? AND target_type = ? AND target_id = ?
    """,
        (user_id, target_type, target_id),
    )


def get_starred_items(user_id: int) -> list[dict]:
    """All of a user's stars, hydrated with the target's display fields in one query."""
    rows = (
        get_conn()
        .execute(
            """
            SELECT s.target_type, s.target_id, s.starred_at,

       t.id              AS track_id,
       t.title           AS track_title,
       t.path            AS track_path,
       t.duration        AS track_duration,
       t.track_number    AS track_number,
       t.disc_number     AS track_disc,
       t.year            AS track_year,
       t.genre           AS track_genre,
       t.suffix          AS track_suffix,
       t.content_type    AS track_content_type,
       t.bitrate         AS track_bitrate,
       t.size            AS track_size,
       t.album_id        AS track_album_id,
       t.artist_id       AS track_artist_id,
       t_al.name         AS track_album_name,
       t_al.cover_art_id AS track_cover_art_id,
       t_ar.name         AS track_artist_name,

       al.name           AS album_name,
       al.artist_id      AS album_artist_id,
       al.track_count    AS album_track_count,
       al.duration       AS album_duration,
       al.year           AS album_year,
       al.genre          AS album_genre,
       al.cover_art_id   AS album_cover_art_id,
       al.created_at     AS album_created_at,
       al_ar.name        AS album_artist_name,

       ar.id             AS artist_id,
       ar.name           AS artist_name
  FROM starred s
LEFT JOIN tracks  t     ON s.target_type = 'track'  AND s.target_id = t.id
LEFT JOIN albums  t_al  ON t.album_id  = t_al.id
LEFT JOIN artists t_ar  ON t.artist_id = t_ar.id
LEFT JOIN albums  al    ON s.target_type = 'album'  AND s.target_id = al.id
LEFT JOIN artists al_ar ON al.artist_id = al_ar.id
LEFT JOIN artists ar    ON s.target_type = 'artist' AND s.target_id = ar.id
 WHERE s.user_id = ?
ORDER BY s.starred_at DESC
            """,
            (user_id,),
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


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
    rows = (
        get_conn()
        .execute("SELECT id, name, path FROM music_folders ORDER BY name")
        .fetchall()
    )
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
    row = (
        get_conn()
        .execute("SELECT id, name, path FROM music_folders WHERE id = ?", (folder_id,))
        .fetchone()
    )
    return _row_to_dict(row)


def get_music_folder_by_path(path: str) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute("SELECT id, name, path FROM music_folders WHERE path = ?", (path,))
        .fetchone()
    )
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
#  Playcount and scrobble
# --------------------------------------------------------------------------


def play_count(user_id: int, track_id: int) -> None:
    now = int(time.time())
    get_conn().execute(
        """
    INSERT INTO play_counts (user_id, track_id, play_count, last_played)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, track_id)
    DO UPDATE SET play_count = play_counts.play_count + 1,
    last_played = excluded.last_played;
    """,
        (user_id, track_id, 1, now),
    )


def get_playcount_by_user(user_id: int, track_id: int) -> int:

    row = (
        get_conn()
        .execute(
            """
        SELECT * FROM play_counts WHERE user_id = ? AND track_id = ?
        """,
            (user_id, track_id),
        )
        .fetchone()
    )
    return row["play_count"] if row else 0


# ---------------------------------------------------------------------------
#  Playlists
# --------------------------------------------------------------------------


def list_playlists(user_id: int) -> list[dict]:
    """Return playlists owned by user plus all public ones."""
    rows = (
        get_conn()
        .execute(
            """
        SELECT playlists.id, playlists.name, playlists.comment, playlists.is_public,
       playlists.created_at, playlists.updated_at, playlists.owner_id,
       users.username AS owner,
       COUNT(playlist_tracks.track_id) AS trackcount,
       COALESCE(SUM(tracks.duration), 0) AS duration
        FROM playlists 
        LEFT JOIN playlist_tracks ON playlist_tracks.playlist_id = playlists.id
        LEFT JOIN tracks ON tracks.id = playlist_tracks.track_id
        LEFT JOIN users ON users.id = playlists.owner_id
        WHERE owner_id = ? 
        OR playlists.is_public = 1 
        GROUP BY playlists.id""",
            (user_id,),
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def get_playlist(playlist_id: int) -> dict | None:
    """Return playlist header + ordered tracks."""
    row = (
        get_conn()
        .execute(
            """SELECT playlists.*, users.username AS owner FROM playlists 
    LEFT JOIN users ON users.id = playlists.owner_id 
    WHERE playlists.id = ?""",
            (playlist_id,),
        )
        .fetchone()
    )

    """ bail if none """
    if row is None:
        return None

    """ fetch ordered tracks """

    # Mirror the joins used by get_track / list_album_tracks so each row has
    # the artist_name, album_name, and album.cover_art_id fields that
    # track_to_subsonic() expects. Without these joins the playlist's track
    # entries come back with no artist, no album, and no cover art.
    rows = (
        get_conn()
        .execute(
            """SELECT playlist_tracks.position, t.*,
                      ar.name AS artist_name,
                      al.name AS album_name,
                      al.cover_art_id AS cover_art_id
                 FROM playlist_tracks
            LEFT JOIN tracks  t  ON t.id  = playlist_tracks.track_id
            LEFT JOIN artists ar ON ar.id = t.artist_id
            LEFT JOIN albums  al ON al.id = t.album_id
                WHERE playlist_tracks.playlist_id = ?
                ORDER BY playlist_tracks.position""",
            (playlist_id,),
        )
        .fetchall()
    )

    result = _row_to_dict(row)
    result["tracks"] = _rows_to_dicts(rows)
    total_duration = sum(track["duration"] or 0 for track in result["tracks"])
    result["trackcount"] = len(result["tracks"])
    result["duration"] = total_duration

    return result


def create_playlist(name: str, owner_id: int, track_ids: list[int]) -> int:
    """Insert playlist + tracks, return new id."""

    now = int(time.time())
    con = get_conn()
    cur = con.execute(
        """
        INSERT INTO playlists (owner_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)
        """,
        (owner_id, name, now, now),
    )

    if track_ids:
        # insert the tracks
        tracks = con.executemany(
            """
        INSERT INTO playlist_tracks (playlist_id, position, track_id) VALUES (?, ?, ?)
        """,
            [(cur.lastrowid, pos, tid) for pos, tid in enumerate(track_ids)],
        )

    return cur.lastrowid


# update playlist function takes playlist id and 3 optional fields returns true if row updated
def update_playlist(
    playlist_id: int, name: str | None, comment: str | None, public: bool
) -> bool:
    # field and param constructor, build SET clause dynamically
    fields: List[str] = []
    params: List[Any] = []
    # if field was provided add it to lists
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if comment is not None:
        fields.append("comment = ?")
        params.append(comment)
    if public is not None:
        fields.append("is_public = ?")
        params.append(int(public))  # converts true/false to 1/0

    if not fields:
        return True  # nothing to update — avoid invalid SQL

    conn = get_conn()
    params.append(playlist_id)
    # join fields below turns into full query with all fields
    extra = conn.execute(
        f"""
            UPDATE playlists SET {', '.join(fields)} WHERE id = ?
            """,
        params,
    )
    return extra.rowcount > 0  # returns a count of rows changed


def delete_playlist(playlist_id: int, owner_id: int) -> bool:
    """Delete only if caller owns it."""
    cur = get_conn().execute(
        "DELETE FROM playlists WHERE id = ? AND owner_id = ?",
        (playlist_id, owner_id),
    )
    return cur.rowcount > 0


def add_tracks_to_playlist(playlist_id: int, track_ids: list[int]) -> None:
    conn = get_conn()
    # get last position in playlist
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()

    next_pos = row[0] + 1

    conn.executemany(
        """
    INSERT INTO playlist_tracks (playlist_id, position, track_id) VALUES (?, ?, ?)""",
        [(playlist_id, next_pos + i, tid) for i, tid in enumerate(track_ids)],
    )


def remove_tracks_from_playlist(playlist_id: int, positions: list[int]) -> None:
    conn = get_conn()
    row = conn.executemany(
        """
        DELETE FROM playlist_tracks WHERE playlist_id = ? AND position = ?
        """,
        [(playlist_id, pos) for pos in positions],
    )

    # fetch remaining tracks
    remaining_tracks = conn.execute(
        """
        SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC
        """,
        (playlist_id,),
    ).fetchall()

    # renum positions
    conn.executemany(
        """
        UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?
        """,
        [
            (new_pos, playlist_id, track_id)
            for new_pos, (track_id,) in enumerate(remaining_tracks)
        ],
    )


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
    row = conn.execute(
        "SELECT id FROM artists WHERE name_lower = ?", (name_lower,)
    ).fetchone()
    return int(row["id"])


def list_artists_indexed() -> Dict[str, List[Dict[str, Any]]]:
    """
    Return artists grouped by their first letter (A, B, C, ...).

    Subsonic's getIndexes wants this exact shape. Numbers and symbols go
    into a "#" bucket.
    """
    rows = get_conn().execute("""
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
        """).fetchall()

    indexed: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        # The "index letter" is the first alpha char of the sort name. Stripping
        # leading "The " is a common Subsonic-ism but we leave that to sort_name.
        key = (row["sort_name"][:1] or "#").upper()
        if not key.isalpha():
            key = "#"
        indexed.setdefault(key, []).append(
            {
                "id": row["id"],
                "name": row["name"],
                "albumCount": row["album_count"],
                "coverArtId": row["cover_art_id"],
            }
        )
    return indexed


def get_artist(artist_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute("SELECT id, name, album_count FROM artists WHERE id = ?", (artist_id,))
        .fetchone()
    )
    return _row_to_dict(row)


def list_genre_count() -> list[dict]:
    """All genre by album and tracks"""
    rows = get_conn().execute("""
                SELECT 
                genre,
                COUNT(DISTINCT album_id) AS albumCount,
                COUNT(*) AS songCount
                FROM tracks
                WHERE genre IS NOT NULL
                GROUP BY genre
                ORDER BY genre COLLATE NOCASE
                """).fetchall()
    return _rows_to_dicts(rows)


def list_artist_albums(artist_id: int) -> List[Dict[str, Any]]:
    """All albums by an artist, ordered by year then name."""
    rows = (
        get_conn()
        .execute(
            """
        SELECT id, artist_id, name, year, genre, release_type, track_count, duration,
               cover_art_id, created_at
          FROM albums
         WHERE artist_id = ?
         ORDER BY year, name COLLATE NOCASE
        """,
            (artist_id,),
        )
        .fetchall()
    )
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
    row = (
        get_conn()
        .execute(
            """
        SELECT al.id, al.name, al.year, al.genre, al.release_type, al.track_count,
               al.duration, al.cover_art_id, al.created_at, al.artist_id,
               ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         WHERE al.id = ?
        """,
            (album_id,),
        )
        .fetchone()
    )
    return _row_to_dict(row)


def list_albums(
    list_type: str = "alphabeticalByName",
    size: int = 10,
    offset: int = 0,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    genre: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Implements getAlbumList's list types. Subsonic supports many; we ship the
    most useful ones and stub the others to alpha-by-name.

    NOTE: random uses ORDER BY RANDOM() which is fine for libraries up to a
    few hundred thousand albums but not great beyond that. For huge libraries,
    pre-compute a random sample table or use rowid sampling.
    """
    order_clause = {
        "newest": "al.created_at DESC",
        "alphabeticalByName": "al.name COLLATE NOCASE ASC",
        "alphabeticalByArtist": "ar.name COLLATE NOCASE ASC, al.year ASC",
        "byYear": "al.year ASC, al.name COLLATE NOCASE ASC",
        "byGenre": "al.name COLLATE NOCASE ASC",
        "random": "RANDOM()",
        "frequent": "(SELECT COALESCE(SUM(pc.play_count), 0) FROM tracks t  JOIN play_counts pc ON pc.track_id = t.id  WHERE t.album_id = al.id) DESC",
        "recent": "(SELECT COALESCE(MAX(pc.last_played), 0)  FROM tracks t  JOIN play_counts pc ON pc.track_id = t.id  WHERE t.album_id = al.id) DESC",
    }.get(list_type, "al.name COLLATE NOCASE ASC")

    where_clauses: List[str] = []
    params: List[Any] = []

    if list_type == "byYear" and from_year is not None and to_year is not None:
        lo, hi = min(from_year, to_year), max(from_year, to_year)
        where_clauses.append("al.year BETWEEN ? AND ?")
        params.extend([lo, hi])

    if list_type == "byGenre" and genre is not None:
        where_clauses.append("al.genre = ? COLLATE NOCASE")
        params.append(genre)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = (
        get_conn()
        .execute(
            f"""
        SELECT al.id, al.name, al.year, al.genre, al.track_count, al.duration,
               al.cover_art_id, al.created_at, al.artist_id, ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         {where_sql}
         ORDER BY {order_clause}
         LIMIT ? OFFSET ?
        """,
            (*params, size, offset),
        )
        .fetchall()
    )
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
    get_conn().execute(
        "UPDATE albums SET cover_art_id = ? WHERE id = ?", (cover_art_id, album_id)
    )


def set_artist_image(artist_id: int, image_id: str) -> None:
    """Persist the hash of a stored artist photo. Same hash namespace as
    `albums.cover_art_id` — files share the artwork cache dir so the same
    `getCoverArt` endpoint serves both kinds of image."""
    get_conn().execute(
        "UPDATE artists SET image_id = ? WHERE id = ?", (image_id, artist_id)
    )


def list_artists_missing_image() -> List[Dict[str, Any]]:
    """Return (id, name) for every artist whose photo hasn't been cached.

    Used by the artwork-recovery sweep. We skip the Various-Artists sentinel
    (album_count 0) because Deezer search would always miss on it.
    """
    rows = (
        get_conn()
        .execute(
            "SELECT id, name FROM artists " "WHERE image_id IS NULL AND album_count > 0"
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


def list_song_by_genre(
    genre: str,
    limit: Optional[int],
    offset: Optional[int],
    music_folder_id: Optional[int] = None,
) -> list[dict[str, any]]:

    rows = (
        get_conn()
        .execute(
            """
        SELECT t.id, t.title, t.track_number, t.disc_number, t.duration, t.bitrate,
               t.size, t.suffix, t.content_type, t.year, t.genre, t.path,
               t.artist_id, ar.name AS artist_name,
               t.album_id,  al.name AS album_name, al.cover_art_id
          FROM tracks t
        LEFT JOIN artists ar ON ar.id = t.artist_id
        LEFT JOIN albums  al ON al.id = t.album_id
        WHERE t.genre = ? COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
            (
                genre,
                limit,
                offset,
            ),
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


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
    row = conn.execute(
        "SELECT id FROM tracks WHERE path = ?", (track["path"],)
    ).fetchone()
    return int(row["id"])


def get_track(track_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            """
        SELECT t.*, ar.name AS artist_name, al.name AS album_name, al.cover_art_id AS cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE t.id = ?
        """,
            (track_id,),
        )
        .fetchone()
    )
    return _row_to_dict(row)


def list_album_tracks(album_id: int) -> List[Dict[str, Any]]:
    """
    Tracks on an album, in disc/track order.

    The composite index (album_id, disc_number, track_number) makes this an
    index-only scan — no sort, no temp table, even for 100k+ track libraries.
    """
    rows = (
        get_conn()
        .execute(
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
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def get_existing_paths_for_folder(
    folder_id: int,
) -> Dict[str, Tuple[int, int, int, int]]:
    """
    Return {path: (id, mtime, size)} for every track in this folder.

    Scanner uses this to decide which files to skip (mtime+size unchanged) and
    which to remove (path no longer on disk). Crucial for incremental scanning
    on large libraries — we never re-parse a file that hasn't changed.
    """
    rows = (
        get_conn()
        .execute(
            "SELECT id, path, mtime, size, album_id FROM tracks WHERE music_folder_id = ?",
            (folder_id,),
        )
        .fetchall()
    )
    return {
        row["path"]: (row["id"], row["mtime"], row["size"], row["album_id"])
        for row in rows
    }


def delete_tracks(track_ids: List[int]) -> None:
    """Remove tracks (e.g. files deleted on disk). Caller handles aggregates."""
    if not track_ids:
        return
    conn = get_conn()
    # SQLite has a limit on the number of host params; chunk if needed. 500 is
    # safely under SQLITE_MAX_VARIABLE_NUMBER (default 999 / 32766 newer).
    for i in range(0, len(track_ids), 500):
        chunk = track_ids[i : i + 500]
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

    if not query:
        songs = conn.execute(
            """
            SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
                   t.track_number, t.year, t.genre, t.path,
                   t.artist_id, ar.name AS artist_name,
                   t.album_id, al.name AS album_name, al.cover_art_id
              FROM tracks t
         LEFT JOIN artists ar ON ar.id = t.artist_id
         LEFT JOIN albums al  ON al.id = t.album_id
         LIMIT ? OFFSET ?
            """,
            (song_count, song_offset),
        ).fetchall()
    else:
        songs = conn.execute(
            """
            SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
                   t.track_number, t.year, t.genre, t.path,
                   t.artist_id, ar.name AS artist_name,
                   t.album_id, al.name AS album_name, al.cover_art_id
                   FROM tracks t
            JOIN virt_fts5 f ON f.rowid = t.id
            LEFT JOIN artists ar ON ar.id = t.artist_id
            LEFT JOIN albums al  ON al.id = t.album_id
            WHERE virt_fts5 MATCH ?
            LIMIT ? OFFSET ?
            """,
            (query, song_count, song_offset),
        ).fetchall()

    return {
        "artists": _rows_to_dicts(artists),
        "albums": _rows_to_dicts(albums),
        "songs": _rows_to_dicts(songs),
    }


# ---------------------------------------------------------------------------
# Users (auth)
# ---------------------------------------------------------------------------

# The role columns live in a tuple so we can build SELECT lists programmatically.
# This avoids typos and means adding a new column only requires one edit here.
_USER_ROLE_COLS = (
    "email",
    "scrobbling_enabled",
    "max_bit_rate",
    "settings_role",
    "stream_role",
    "download_role",
    "upload_role",
    "playlist_role",
    "cover_art_role",
    "comment_role",
    "podcast_role",
    "jukebox_role",
    "share_role",
    "video_conversion_role",
)

# Full user row including password_hash and encrypted_password — auth paths only.
_USER_SELECT = (
    "id, username, password_hash, encrypted_password, is_admin, created_at, password_changed_at, "
    + ", ".join(_USER_ROLE_COLS)
)

# Same but without password_hash — safe to return to the API layer / admin UI.
# We never send hashes to the frontend.
_USER_SELECT_NO_HASH = (
    "id, username, is_admin, created_at, password_changed_at, "
    + ", ".join(_USER_ROLE_COLS)
)


def create_user(
    username: str,
    password_hash: str,
    is_admin: bool = False,
    email: Optional[str] = None,
    scrobbling_enabled: bool = False,
    max_bit_rate: int = 0,
    settings_role: bool = True,
    stream_role: bool = True,
    download_role: bool = False,
    upload_role: bool = False,
    playlist_role: bool = True,
    cover_art_role: bool = False,
    comment_role: bool = False,
    podcast_role: bool = False,
    jukebox_role: bool = False,
    share_role: bool = False,
    video_conversion_role: bool = False,
) -> int:
    """Insert a new user. Raises sqlite3.IntegrityError if username is taken."""
    now = int(time.time())
    cur = get_conn().execute(
        """
        INSERT INTO users (
            username, password_hash, is_admin, created_at, password_changed_at,
            email, scrobbling_enabled, max_bit_rate,
            settings_role, stream_role, download_role, upload_role,
            playlist_role, cover_art_role, comment_role, podcast_role,
            jukebox_role, share_role, video_conversion_role
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            password_hash,
            int(is_admin),
            now,
            now,
            email,
            int(scrobbling_enabled),
            max_bit_rate,
            int(settings_role),
            int(stream_role),
            int(download_role),
            int(upload_role),
            int(playlist_role),
            int(cover_art_role),
            int(comment_role),
            int(podcast_role),
            int(jukebox_role),
            int(share_role),
            int(video_conversion_role),
        ),
    )
    return cur.lastrowid


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(f"SELECT {_USER_SELECT} FROM users WHERE username = ?", (username,))
        .fetchone()
    )
    return _row_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(f"SELECT {_USER_SELECT_NO_HASH} FROM users WHERE id = ?", (user_id,))
        .fetchone()
    )
    return _row_to_dict(row)


def list_users() -> List[Dict[str, Any]]:
    """All users without password_hash, ordered by username."""
    rows = (
        get_conn()
        .execute(
            f"SELECT {_USER_SELECT_NO_HASH} FROM users ORDER BY username COLLATE NOCASE"
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def update_user(
    username: str,
    # The * below means every argument after it MUST be passed as a keyword
    # argument (e.g. update_user("alice", download_role=True)), not positionally.
    # This protects against accidentally passing the wrong value to the wrong field.
    *,
    password_hash: Optional[str] = None,
    email: Optional[str] = None,
    is_admin: Optional[bool] = None,
    scrobbling_enabled: Optional[bool] = None,
    max_bit_rate: Optional[int] = None,
    settings_role: Optional[bool] = None,
    stream_role: Optional[bool] = None,
    download_role: Optional[bool] = None,
    upload_role: Optional[bool] = None,
    playlist_role: Optional[bool] = None,
    cover_art_role: Optional[bool] = None,
    comment_role: Optional[bool] = None,
    podcast_role: Optional[bool] = None,
    jukebox_role: Optional[bool] = None,
    share_role: Optional[bool] = None,
    video_conversion_role: Optional[bool] = None,
) -> bool:
    """Update any subset of user fields by username. Returns True if found."""
    # Build the SET clause dynamically — only include fields the caller actually
    # supplied. A None value means "leave this column alone". This lets you call
    # update_user("alice", download_role=True) to flip one role without touching
    # anything else, without needing 16 separate update functions.
    fields: List[str] = []
    params: List[Any] = []

    def _add(col: str, val: Any, cast=None) -> None:
        """Append 'col = ?' to fields and the value to params, if val is not None."""
        if val is None:
            return
        fields.append(f"{col} = ?")
        # cast converts Python bool to int (SQLite stores booleans as 0/1).
        params.append(cast(val) if cast else val)

    _add("password_hash", password_hash)
    # Stamp the rotation timestamp whenever the hash changes.
    if password_hash is not None:
        _add("password_changed_at", int(time.time()))
    _add("email", email)
    _add("is_admin", is_admin, cast=int)
    _add("scrobbling_enabled", scrobbling_enabled, cast=int)
    _add("max_bit_rate", max_bit_rate)
    _add("settings_role", settings_role, cast=int)
    _add("stream_role", stream_role, cast=int)
    _add("download_role", download_role, cast=int)
    _add("upload_role", upload_role, cast=int)
    _add("playlist_role", playlist_role, cast=int)
    _add("cover_art_role", cover_art_role, cast=int)
    _add("comment_role", comment_role, cast=int)
    _add("podcast_role", podcast_role, cast=int)
    _add("jukebox_role", jukebox_role, cast=int)
    _add("share_role", share_role, cast=int)
    _add("video_conversion_role", video_conversion_role, cast=int)

    if not fields:
        return bool(get_user_by_username(username))

    params.append(username)
    cur = get_conn().execute(
        f"UPDATE users SET {', '.join(fields)} WHERE username = ?", params
    )
    return cur.rowcount > 0


def update_user_password(user_id: int, password_hash: str) -> bool:
    """Replace the stored password hash by id. Returns True if the user existed."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
        (password_hash, int(time.time()), user_id),
    )
    conn.commit()
    return cur.rowcount > 0

def update_encrypted_password(user_id: int, value: Optional[str]) -> None:
    """Store or clear the Fernet-encrypted plaintext password used for Subsonic token+salt auth."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET encrypted_password = ? WHERE id = ?",
        (value, user_id),
    )
    conn.commit()


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Set or clear the admin flag by id. Returns True if the user existed."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id)
    )
    conn.commit()
    return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    """Remove a user by id. Returns True if a row was deleted."""
    cur = get_conn().execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cur.rowcount > 0


def delete_user_by_username(username: str) -> bool:
    """Remove a user by username. Returns True if a row was deleted."""
    cur = get_conn().execute("DELETE FROM users WHERE username = ?", (username,))
    return cur.rowcount > 0


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
        "albums": conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
        "tracks": conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],
        "total_duration_seconds": int(duration_row[0] or 0),
    }
