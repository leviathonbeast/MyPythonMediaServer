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

import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .connection import DIALECT_POSTGRES, get_conn, get_dialect


_LEADING_ARTICLE_RE = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)


def normalize_sort_name(name: str) -> str:
    """Compute an alphabetical sort key matching Apple Music's convention:
    leading English articles are stripped ('The Wall' sorts under 'W'),
    but symbol-leading names keep their symbol and get prefixed with '~'
    so they sort after Z ('$0', '& Juliet', '( D E S ) O L Λ T E' all land
    at the bottom of A-Z lists rather than the top).
    """
    s = _LEADING_ARTICLE_RE.sub('', name.strip(), count=1)
    if not s or not s[:1].isalnum():
        s = '~' + (s or name)
    return s

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
# Starred items
# ---------------------------------------------------------------------------


def star_item(user_id: int, target_type: str, target_id: int) -> None:
    # ON CONFLICT DO NOTHING is the portable form of INSERT OR IGNORE.
    # Both SQLite (>= 3.24) and Postgres support it identically.
    get_conn().execute(
        """
        INSERT INTO starred (user_id, target_type, target_id, starred_at)
        VALUES (:user_id, :target_type, :target_id, :starred_at)
        ON CONFLICT (user_id, target_type, target_id) DO NOTHING
        """,
        {
            "user_id": user_id,
            "target_type": target_type,
            "target_id": target_id,
            "starred_at": int(time.time()),
        },
    )


def unstar_item(user_id: int, target_type: str, target_id: int) -> None:
    get_conn().execute(
        """
        DELETE FROM starred
         WHERE user_id = :user_id
           AND target_type = :target_type
           AND target_id = :target_id
        """,
        {"user_id": user_id, "target_type": target_type, "target_id": target_id},
    )


def get_starred_items(user_id: int) -> List[Dict[str, Any]]:
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
         WHERE s.user_id = :user_id
      ORDER BY s.starred_at DESC
        """,
            {"user_id": user_id},
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


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
        .execute(
            "SELECT id, name, path FROM music_folders WHERE id = :id",
            {"id": folder_id},
        )
        .fetchone()
    )
    return _row_to_dict(row)


def get_music_folder_by_path(path: str) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            "SELECT id, name, path FROM music_folders WHERE path = :path",
            {"path": path},
        )
        .fetchone()
    )
    return _row_to_dict(row)


def add_music_folder(name: str, path: str) -> int:
    """Insert a new music folder and return its id.

    Caller is expected to validate `path` (existence, readability) before
    calling — this layer just enforces the UNIQUE constraint on path,
    raising IntegrityError on duplicates.

    Uses RETURNING instead of lastrowid so it works on Postgres too;
    psycopg cursors don't expose a lastrowid attribute.
    """
    row = get_conn().execute(
        "INSERT INTO music_folders (name, path) VALUES (:name, :path) RETURNING id",
        {"name": name, "path": path},
    ).fetchone()
    return int(row["id"])


def delete_music_folder(folder_id: int) -> bool:
    """Remove a music folder. Tracks under it are cascade-deleted by FK.

    Returns True if a row was deleted, False if no folder existed at that id.
    Aggregate cleanup of newly-empty albums/artists is the GC's job, not
    ours — call run_gc() afterwards.
    """
    cur = get_conn().execute(
        "DELETE FROM music_folders WHERE id = :id",
        {"id": folder_id},
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Play count and scrobble
# ---------------------------------------------------------------------------


def play_count(user_id: int, track_id: int) -> None:
    get_conn().execute(
        """
        INSERT INTO play_counts (user_id, track_id, play_count, last_played)
        VALUES (:user_id, :track_id, :play_count, :last_played)
        ON CONFLICT(user_id, track_id) DO UPDATE SET
            play_count  = play_counts.play_count + 1,
            last_played = excluded.last_played
        """,
        {
            "user_id": user_id,
            "track_id": track_id,
            "play_count": 1,
            "last_played": int(time.time()),
        },
    )


def get_playcount_by_user(user_id: int, track_id: int) -> int:
    row = (
        get_conn()
        .execute(
            """
            SELECT play_count FROM play_counts
             WHERE user_id = :user_id AND track_id = :track_id
            """,
            {"user_id": user_id, "track_id": track_id},
        )
        .fetchone()
    )
    return row["play_count"] if row else 0


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


def list_playlists(user_id: int) -> List[Dict[str, Any]]:
    """Return playlists owned by user plus all public ones."""
    # GROUP BY needs `u.username` explicitly because Postgres only
    # treats columns as functionally dependent on the GROUP BY when
    # they're from the same table whose primary key is grouped.
    # `p.id` covers every column from `playlists`, but `u.username`
    # is from a JOINed table and Postgres refuses without it in the
    # group list. SQLite was happy to pick an arbitrary u.username
    # per group; the answer is the same since there's only ever one
    # owner per playlist, but Postgres needs the rule stated.
    rows = (
        get_conn()
        .execute(
            """
        SELECT p.id, p.name, p.comment, p.is_public,
               p.created_at, p.updated_at, p.owner_id,
               u.username AS owner,
               COUNT(pt.track_id) AS trackcount,
               COALESCE(SUM(t.duration), 0) AS duration
          FROM playlists p
     LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
     LEFT JOIN tracks t           ON t.id = pt.track_id
     LEFT JOIN users u            ON u.id = p.owner_id
         WHERE p.owner_id = :user_id OR p.is_public = 1
      GROUP BY p.id, u.username
        """,
            {"user_id": user_id},
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def get_playlist(playlist_id: int) -> Optional[Dict[str, Any]]:
    """Return playlist header + ordered tracks."""
    conn = get_conn()
    header = conn.execute(
        """
        SELECT p.*, u.username AS owner
          FROM playlists p
     LEFT JOIN users u ON u.id = p.owner_id
         WHERE p.id = :playlist_id
        """,
        {"playlist_id": playlist_id},
    ).fetchone()

    if header is None:
        return None

    # Mirror the joins used by get_track / list_album_tracks so each row has
    # the artist_name, album_name, and album.cover_art_id fields that
    # track_to_subsonic() expects. Without these joins the playlist's track
    # entries come back with no artist, no album, and no cover art.
    track_rows = conn.execute(
        """
        SELECT pt.position, t.*,
               ar.name AS artist_name,
               al.name AS album_name,
               al.cover_art_id AS cover_art_id
          FROM playlist_tracks pt
     LEFT JOIN tracks  t  ON t.id  = pt.track_id
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE pt.playlist_id = :playlist_id
      ORDER BY pt.position
        """,
        {"playlist_id": playlist_id},
    ).fetchall()

    result = _row_to_dict(header)
    result["tracks"] = _rows_to_dicts(track_rows)
    result["trackcount"] = len(result["tracks"])
    result["duration"] = sum(track["duration"] or 0 for track in result["tracks"])
    return result


def create_playlist(name: str, owner_id: int, track_ids: List[int]) -> int:
    """Insert playlist + tracks, return new id."""
    now = int(time.time())
    conn = get_conn()
    # RETURNING instead of lastrowid — works on both sqlite3 and psycopg.
    row = conn.execute(
        """
        INSERT INTO playlists (owner_id, name, created_at, updated_at)
        VALUES (:owner_id, :name, :created_at, :updated_at)
        RETURNING id
        """,
        {"owner_id": owner_id, "name": name, "created_at": now, "updated_at": now},
    ).fetchone()
    playlist_id = int(row["id"])

    if track_ids:
        conn.executemany(
            """
            INSERT INTO playlist_tracks (playlist_id, position, track_id)
            VALUES (:playlist_id, :position, :track_id)
            """,
            [
                {"playlist_id": playlist_id, "position": pos, "track_id": tid}
                for pos, tid in enumerate(track_ids)
            ],
        )

    return playlist_id


def update_playlist(
    playlist_id: int,
    name: Optional[str],
    comment: Optional[str],
    public: bool,
) -> bool:
    """Update any subset of (name, comment, public). Returns True if a row matched."""
    # Build SET clause dynamically — only include fields the caller supplied.
    # Each SET clause references a named placeholder; the params dict carries
    # exactly those keys plus :playlist_id for the WHERE.
    fields: List[str] = []
    params: Dict[str, Any] = {"playlist_id": playlist_id}

    if name is not None:
        fields.append("name = :name")
        params["name"] = name
    if comment is not None:
        fields.append("comment = :comment")
        params["comment"] = comment
    if public is not None:
        fields.append("is_public = :is_public")
        params["is_public"] = int(public)  # SQLite stores booleans as 0/1

    if not fields:
        return True  # nothing to update — avoid invalid SQL

    cur = get_conn().execute(
        f"UPDATE playlists SET {', '.join(fields)} WHERE id = :playlist_id",
        params,
    )
    return cur.rowcount > 0


def delete_playlist(playlist_id: int, owner_id: int) -> bool:
    """Delete only if caller owns it."""
    cur = get_conn().execute(
        "DELETE FROM playlists WHERE id = :playlist_id AND owner_id = :owner_id",
        {"playlist_id": playlist_id, "owner_id": owner_id},
    )
    return cur.rowcount > 0


def add_tracks_to_playlist(playlist_id: int, track_ids: List[int]) -> None:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT COALESCE(MAX(position), -1) AS max_position
          FROM playlist_tracks
         WHERE playlist_id = :playlist_id
        """,
        {"playlist_id": playlist_id},
    ).fetchone()
    next_pos = row["max_position"] + 1

    conn.executemany(
        """
        INSERT INTO playlist_tracks (playlist_id, position, track_id)
        VALUES (:playlist_id, :position, :track_id)
        """,
        [
            {"playlist_id": playlist_id, "position": next_pos + i, "track_id": tid}
            for i, tid in enumerate(track_ids)
        ],
    )


def replace_playlist_tracks(playlist_id: int, track_ids: List[int]) -> None:
    """Replace the entire track list of a playlist (used by createPlaylist update mode)."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = :playlist_id",
        {"playlist_id": playlist_id},
    )
    if track_ids:
        conn.executemany(
            """
            INSERT INTO playlist_tracks (playlist_id, position, track_id)
            VALUES (:playlist_id, :position, :track_id)
            """,
            [
                {"playlist_id": playlist_id, "position": pos, "track_id": tid}
                for pos, tid in enumerate(track_ids)
            ],
        )
    now = int(time.time())
    conn.execute(
        "UPDATE playlists SET updated_at = :updated_at WHERE id = :playlist_id",
        {"updated_at": now, "playlist_id": playlist_id},
    )


def remove_tracks_from_playlist(playlist_id: int, positions: List[int]) -> None:
    conn = get_conn()
    conn.executemany(
        """
        DELETE FROM playlist_tracks
         WHERE playlist_id = :playlist_id AND position = :position
        """,
        [{"playlist_id": playlist_id, "position": pos} for pos in positions],
    )

    # Renumber positions so the remaining tracks stay 0..N-1 contiguous.
    remaining = conn.execute(
        """
        SELECT track_id FROM playlist_tracks
         WHERE playlist_id = :playlist_id
      ORDER BY position ASC
        """,
        {"playlist_id": playlist_id},
    ).fetchall()

    conn.executemany(
        """
        UPDATE playlist_tracks SET position = :position
         WHERE playlist_id = :playlist_id AND track_id = :track_id
        """,
        [
            {"position": new_pos, "playlist_id": playlist_id, "track_id": track_id}
            for new_pos, (track_id,) in enumerate(remaining)
        ],
    )


# ---------------------------------------------------------------------------
# Play queue (Subsonic savePlayQueue / getPlayQueue)
# ---------------------------------------------------------------------------


def get_play_queue(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()

    header = conn.execute(
        """
        SELECT p.*, u.username AS owner
          FROM play_queues p
     LEFT JOIN users u ON u.id = p.user_id
         WHERE p.user_id = :user_id
        """,
        {"user_id": user_id},
    ).fetchone()

    tracks = conn.execute(
        """
        SELECT t.*,
               ar.name AS artist_name,
               al.name AS album_name,
               al.cover_art_id AS cover_art_id
          FROM play_queue_entries e
     LEFT JOIN tracks  t  ON e.track_id = t.id
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE e.user_id = :user_id
      ORDER BY e.position
        """,
        {"user_id": user_id},
    ).fetchall()

    if header is None:
        return None
    result = _row_to_dict(header)
    result["tracks"] = _rows_to_dicts(tracks)
    result["trackcount"] = len(result["tracks"])
    result["duration"] = sum(track["duration"] or 0 for track in result["tracks"])
    return result


# {"current": ..., "position": ..., "changed": ..., "changedBy": ..., "username": ..., "tracks": [<rows for track_to_subsonic>]}


def save_play_queue(
    user_id: int,
    track_ids: List[int],
    current_track_id: Optional[int],
    position_ms: int,
    client: str,
) -> None:
    """Replace this user's saved queue with a new one."""
    conn = get_conn()
    # Wipe the old ordered list. The header row in play_queues is upserted
    # below, so we don't need to delete it explicitly.
    conn.execute(
        "DELETE FROM play_queue_entries WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    # INSERT ... ON CONFLICT DO UPDATE is the portable form of
    # INSERT OR REPLACE. Replaces all the columns of the conflicting row
    # with the new values; only the user_id (the PK) stays.
    conn.execute(
        """
        INSERT INTO play_queues
            (user_id, current_id, position_ms, changed_at, changed_by)
        VALUES (:user_id, :current_id, :position_ms, :changed_at, :changed_by)
        ON CONFLICT (user_id) DO UPDATE SET
            current_id  = excluded.current_id,
            position_ms = excluded.position_ms,
            changed_at  = excluded.changed_at,
            changed_by  = excluded.changed_by
        """,
        {
            "user_id": user_id,
            "current_id": current_track_id,
            "position_ms": position_ms,
            "changed_at": int(time.time()),
            "changed_by": client,
        },
    )
    if track_ids:
        conn.executemany(
            """
            INSERT INTO play_queue_entries (user_id, position, track_id)
            VALUES (:user_id, :position, :track_id)
            """,
            [
                {"user_id": user_id, "position": pos, "track_id": tid}
                for pos, tid in enumerate(track_ids)
            ],
        )


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------


def upsert_artist(
    name: str,
    sort_name: Optional[str] = None,
    musicbrainz_id: Optional[str] = None,
) -> int:
    """Insert artist if missing, return id.

    Used by the scanner. Lower-case dedup key means "AC/DC" and "ac/dc" merge.

    Performance: SQLite 3.35+ RETURNING lets us collapse the previous
    INSERT-then-SELECT into one round trip.

    `musicbrainz_id` (if provided) is filled in on first insert and on any
    subsequent scan where the existing row's MBID is NULL. We don't
    overwrite an existing non-null value — once Picard has stamped an
    artist row, a later untagged file shouldn't blank it out.
    """
    name_lower = name.strip().lower()
    sort_name = sort_name or name
    row = get_conn().execute(
        """
        INSERT INTO artists (name, name_lower, sort_name, musicbrainz_id)
        VALUES (:name, :name_lower, :sort_name, :musicbrainz_id)
        ON CONFLICT(name_lower) DO UPDATE SET
            musicbrainz_id = COALESCE(artists.musicbrainz_id, excluded.musicbrainz_id)
        RETURNING id
        """,
        {
            "name": name,
            "name_lower": name_lower,
            "sort_name": sort_name,
            "musicbrainz_id": musicbrainz_id,
        },
    ).fetchone()
    return int(row["id"])


def list_artists_indexed() -> Dict[str, List[Dict[str, Any]]]:
    """Return artists grouped by their first letter (A, B, C, ...).

    Subsonic's getIndexes wants this exact shape. Numbers and symbols go
    into a "#" bucket.

    Performance:
      * `album_count` comes from the denormalized column on artists, so
        there's no GROUP BY / JOIN-and-count over albums. WHERE album_count
        > 0 filters out the empty Various-Artists sentinel.
      * The "newest album cover" pick is done with a ROW_NUMBER() window
        scanned ONCE over the albums table instead of a per-artist
        correlated subquery. EXPLAIN QUERY PLAN previously showed
        'CORRELATED SCALAR SUBQUERY' firing once per artist; the rewrite
        runs the album scan once total.
    """
    rows = get_conn().execute("""
        SELECT a.id,
               a.name,
               a.album_count                       AS album_count,
               COALESCE(a.sort_name, a.name)       AS sort_name,
               a.musicbrainz_id                    AS musicbrainz_id,
               cv.cover_art_id                     AS cover_art_id
          FROM artists a
     LEFT JOIN (
                 SELECT artist_id, cover_art_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY artist_id
                            ORDER BY year DESC NULLS LAST, created_at DESC NULLS LAST
                        ) AS rn
                   FROM albums
                  WHERE cover_art_id IS NOT NULL
               ) cv ON cv.artist_id = a.id AND cv.rn = 1
         WHERE a.album_count > 0
      ORDER BY sort_name COLLATE NOCASE NULLS LAST
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
                "musicBrainzId": row["musicbrainz_id"],
            }
        )
    return indexed


def get_artist(artist_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            """
            SELECT id, name, album_count, musicbrainz_id
              FROM artists WHERE id = :id
            """,
            {"id": artist_id},
        )
        .fetchone()
    )
    return _row_to_dict(row)


def list_genre_count() -> List[Dict[str, Any]]:
    """All genres with their album and track counts."""
    # Aliases are double-quoted so Postgres preserves the camelCase
    # column names. Without quoting Postgres folds unquoted identifiers
    # to lowercase, which would surface as KeyError('songCount') in
    # the API layer. SQLite accepts the quoting unchanged.
    rows = get_conn().execute("""
        SELECT genre,
               COUNT(DISTINCT album_id) AS "albumCount",
               COUNT(*) AS "songCount"
          FROM tracks
         WHERE genre IS NOT NULL
      GROUP BY genre
      ORDER BY genre COLLATE NOCASE
        """).fetchall()
    return _rows_to_dicts(rows)


def list_artist_appearances(artist_id: int) -> List[Dict[str, Any]]:
    """Tracks credited to this artist on an album NOT primarily by them.

    The artist page already shows albums where this artist is the
    album-artist (`list_artist_albums`). Lots of artists also appear as
    track-artists on compilations, soundtracks, or guest verses — the
    track has them as `tracks.artist_id`, but the album's `artist_id`
    points elsewhere (often the Various-Artists sentinel). Without this
    query those contributions are invisible.

    Filter logic: `tracks.artist_id = :artist_id` keeps only the artist's
    tracks; `albums.artist_id != :artist_id` drops everything already
    surfaced via the albums-grouped section. The ordering puts newest
    first (matches the artist-page album sections) and groups tracks by
    album so multi-track appearances on the same comp render adjacent.

    Returns rows in the shape `track_to_subsonic` expects, plus a couple
    of extra fields the frontend can use for context ("appears on …").
    """
    rows = (
        get_conn()
        .execute(
            """
        SELECT t.id, t.title, t.track_number, t.disc_number, t.duration, t.bitrate,
               t.size, t.suffix, t.content_type, t.year, t.genre, t.path,
               t.musicbrainz_id,
               t.artist_id, ar.name AS artist_name,
               t.album_id, al.name AS album_name, al.cover_art_id,
               al.year AS album_year,
               al.artist_id AS album_artist_id,
               aa.name AS album_artist_name
          FROM tracks  t
          JOIN albums  al ON al.id = t.album_id
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN artists aa ON aa.id = al.artist_id
         WHERE t.artist_id = :artist_id
           AND al.artist_id != :artist_id
      ORDER BY COALESCE(al.year, 0) DESC,
               al.name COLLATE NOCASE,
               t.disc_number,
               t.track_number
            """,
            {"artist_id": artist_id},
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def list_artist_albums(artist_id: int) -> List[Dict[str, Any]]:
    """All albums by an artist, ordered by year then name."""
    rows = (
        get_conn()
        .execute(
            """
        SELECT id, artist_id, name, year, genre, release_type, track_count, duration,
               cover_art_id, created_at,
               musicbrainz_id, musicbrainz_releasegroup_id
          FROM albums
         WHERE artist_id = :artist_id
      ORDER BY year NULLS LAST, name COLLATE NOCASE
        """,
            {"artist_id": artist_id},
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
    musicbrainz_id: Optional[str] = None,
    musicbrainz_releasegroup_id: Optional[str] = None,
) -> int:
    """Insert album if missing, return id.

    `release_type` follows MusicBrainz primary types ("album", "ep",
    "single", "compilation", "live", ...). NULL means "not tagged" and
    is treated as "album" for grouping purposes by the artist page.

    The COALESCEs in DO UPDATE preserve existing values when the new row
    has NULLs, so a tag-less file followed by a tagged one fills in
    year/genre/release_type/MBIDs instead of clobbering them.
    """
    name_lower = name.strip().lower()
    sort_name = normalize_sort_name(name)
    now = int(time.time())
    row = get_conn().execute(
        """
        INSERT INTO albums (
            artist_id, name, name_lower, sort_name, year, genre, release_type,
            created_at, musicbrainz_id, musicbrainz_releasegroup_id
        )
        VALUES (
            :artist_id, :name, :name_lower, :sort_name, :year, :genre, :release_type,
            :created_at, :musicbrainz_id, :musicbrainz_releasegroup_id
        )
        ON CONFLICT(artist_id, name_lower) DO UPDATE SET
            year         = COALESCE(excluded.year,         albums.year),
            genre        = COALESCE(excluded.genre,        albums.genre),
            release_type = COALESCE(excluded.release_type, albums.release_type),
            musicbrainz_id = COALESCE(albums.musicbrainz_id, excluded.musicbrainz_id),
            musicbrainz_releasegroup_id =
                COALESCE(albums.musicbrainz_releasegroup_id, excluded.musicbrainz_releasegroup_id)
        RETURNING id
        """,
        {
            "artist_id": artist_id,
            "name": name,
            "name_lower": name_lower,
            "sort_name": sort_name,
            "year": year,
            "genre": genre,
            "release_type": release_type,
            "created_at": now,
            "musicbrainz_id": musicbrainz_id,
            "musicbrainz_releasegroup_id": musicbrainz_releasegroup_id,
        },
    ).fetchone()
    return int(row["id"])


def get_album(album_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            """
        SELECT al.id, al.name, al.year, al.genre, al.release_type, al.track_count,
               al.duration, al.cover_art_id, al.created_at, al.artist_id,
               al.musicbrainz_id, al.musicbrainz_releasegroup_id,
               ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         WHERE al.id = :id
        """,
            {"id": album_id},
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
    """Implements getAlbumList's list types.

    Subsonic supports many; we ship the most useful ones and stub the others
    to alpha-by-name.

    Performance:
      * frequent/recent used to run a correlated scalar subquery per album:
        for every album row in the LIMIT we'd scan its tracks + play_counts.
        With 50 albums in the result that's 50 sub-scans. We now JOIN against
        a single grouped CTE that aggregates play_counts once.
      * random uses ORDER BY RANDOM() which is fine for libraries up to a
        few hundred thousand albums but not great beyond that. For huge
        libraries pre-compute a random sample table or use rowid sampling.
    """
    where_clauses: List[str] = []
    params: Dict[str, Any] = {"size": size, "offset": offset}

    if list_type == "byYear" and from_year is not None and to_year is not None:
        lo, hi = min(from_year, to_year), max(from_year, to_year)
        where_clauses.append("al.year BETWEEN :from_year AND :to_year")
        params["from_year"] = lo
        params["to_year"] = hi

    if list_type == "byGenre" and genre is not None:
        where_clauses.append("al.genre = :genre COLLATE NOCASE")
        params["genre"] = genre

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # frequent/recent need play-count aggregation; everything else is a
    # straight albums-table scan. Keep them on separate code paths so the
    # planner only sees the join when it has to.
    if list_type in ("frequent", "recent"):
        agg = "SUM(pc.play_count)" if list_type == "frequent" else "MAX(pc.last_played)"
        sql = f"""
            WITH album_plays AS (
                SELECT t.album_id AS aid, {agg} AS sort_key
                  FROM tracks t
                  JOIN play_counts pc ON pc.track_id = t.id
                 WHERE t.album_id IS NOT NULL
              GROUP BY t.album_id
            )
            SELECT al.id, al.name, al.year, al.genre, al.track_count, al.duration,
                   al.cover_art_id, al.created_at, al.artist_id,
                   al.musicbrainz_id, al.musicbrainz_releasegroup_id,
                   ar.name AS artist_name
              FROM albums al
              JOIN artists ar ON ar.id = al.artist_id
         LEFT JOIN album_plays ap ON ap.aid = al.id
             {where_sql}
          ORDER BY COALESCE(ap.sort_key, 0) DESC
             LIMIT :size OFFSET :offset
        """
    else:
        order_clause = {
            "newest":               "al.created_at DESC",
            "alphabeticalByName":   "COALESCE(al.sort_name, al.name) COLLATE NOCASE ASC",
            "alphabeticalByArtist": "ar.name COLLATE NOCASE ASC, al.year ASC NULLS LAST",
            "byYear":               "al.year ASC NULLS LAST, COALESCE(al.sort_name, al.name) COLLATE NOCASE ASC",
            "byGenre":              "COALESCE(al.sort_name, al.name) COLLATE NOCASE ASC",
            "random":               "RANDOM()",
        }.get(list_type, "al.name COLLATE NOCASE ASC")

        sql = f"""
            SELECT al.id, al.name, al.year, al.genre, al.track_count, al.duration,
                   al.cover_art_id, al.created_at, al.artist_id,
                   al.musicbrainz_id, al.musicbrainz_releasegroup_id,
                   ar.name AS artist_name
              FROM albums al
              JOIN artists ar ON ar.id = al.artist_id
             {where_sql}
          ORDER BY {order_clause}
             LIMIT :size OFFSET :offset
        """

    rows = get_conn().execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


def update_album_aggregates(album_id: int) -> None:
    """Recompute (track_count, duration) for an album.

    We denormalize these on the album row so browsing 1000 albums doesn't run
    1000 COUNT(*) queries. Called by the scanner whenever tracks change.
    """
    get_conn().execute(
        """
        UPDATE albums
           SET track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = :album_id),
               duration    = (SELECT COALESCE(SUM(duration), 0) FROM tracks WHERE album_id = :album_id)
         WHERE id = :album_id
        """,
        {"album_id": album_id},
    )


def update_artist_aggregates(artist_id: int) -> None:
    """Recompute album_count for an artist. Same rationale as above."""
    get_conn().execute(
        """
        UPDATE artists
           SET album_count = (SELECT COUNT(*) FROM albums WHERE artist_id = :artist_id)
         WHERE id = :artist_id
        """,
        {"artist_id": artist_id},
    )


def set_album_cover_art(album_id: int, cover_art_id: str) -> None:
    get_conn().execute(
        "UPDATE albums SET cover_art_id = :cover_art_id WHERE id = :album_id",
        {"cover_art_id": cover_art_id, "album_id": album_id},
    )


def set_artist_image(artist_id: int, image_id: str) -> None:
    """Persist the hash of a stored artist photo.

    Same hash namespace as `albums.cover_art_id` — files share the artwork
    cache dir so the same `getCoverArt` endpoint serves both kinds of image.
    """
    get_conn().execute(
        "UPDATE artists SET image_id = :image_id WHERE id = :artist_id",
        {"image_id": image_id, "artist_id": artist_id},
    )


def list_artists_missing_image() -> List[Dict[str, Any]]:
    """Return (id, name) for every artist whose photo hasn't been cached.

    Used by the artwork-recovery sweep. We skip the Various-Artists sentinel
    (album_count 0) because Deezer search would always miss on it.
    """
    rows = (
        get_conn()
        .execute(
            "SELECT id, name FROM artists WHERE image_id IS NULL AND album_count > 0"
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


def list_random_songs(
    size: int = 10,
    genre: Optional[str] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    music_folder_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Random song selection for the getRandomSongs endpoint.

    ORDER BY RANDOM() is fine for libraries up to a few hundred thousand
    tracks but degrades on huge libraries. Honor optional genre/year/folder
    filters from the Subsonic spec.
    """
    where: List[str] = []
    params: Dict[str, Any] = {"size": size}

    if genre is not None:
        where.append("t.genre = :genre COLLATE NOCASE")
        params["genre"] = genre
    if from_year is not None:
        where.append("t.year >= :from_year")
        params["from_year"] = from_year
    if to_year is not None:
        where.append("t.year <= :to_year")
        params["to_year"] = to_year
    if music_folder_id is not None:
        where.append("t.music_folder_id = :music_folder_id")
        params["music_folder_id"] = music_folder_id

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = (
        get_conn()
        .execute(
            f"""
        SELECT t.id, t.title, t.track_number, t.disc_number, t.duration, t.bitrate,
               t.size, t.suffix, t.content_type, t.year, t.genre, t.path,
               t.artist_id, ar.name AS artist_name,
               t.album_id,  al.name AS album_name, al.cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         {where_sql}
      ORDER BY RANDOM()
         LIMIT :size
        """,
            params,
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def list_song_by_genre(
    genre: str,
    limit: Optional[int],
    offset: Optional[int],
    music_folder_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Songs filtered by genre, paginated.

    The ORDER BY is required for OFFSET to be stable — without it SQLite is
    free to return rows in any order, which means page 2 can repeat or skip
    rows that appeared on page 1. We sort by id (the rowid alias) because it's
    free: it's the primary key, so the LIMIT+OFFSET walk happens index-only.
    """
    where = ["t.genre = :genre COLLATE NOCASE"]
    params: Dict[str, Any] = {"genre": genre, "limit": limit, "offset": offset}
    if music_folder_id is not None:
        where.append("t.music_folder_id = :music_folder_id")
        params["music_folder_id"] = music_folder_id
    where_sql = "WHERE " + " AND ".join(where)

    rows = (
        get_conn()
        .execute(
            f"""
        SELECT t.id, t.title, t.track_number, t.disc_number, t.duration, t.bitrate,
               t.size, t.suffix, t.content_type, t.year, t.genre, t.path,
               t.artist_id, ar.name AS artist_name,
               t.album_id,  al.name AS album_name, al.cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         {where_sql}
      ORDER BY t.id
         LIMIT :limit OFFSET :offset
        """,
            params,
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def upsert_track(track: Dict[str, Any]) -> int:
    """Insert or update a track row by path.

    The scanner builds the dict; we just persist it. Returning the id lets
    the scanner accumulate album_id->track-count maps in memory.

    Performance: RETURNING collapses INSERT-then-SELECT into one round trip.
    Saves one query per track during scans — on a 100k-track library that's
    100k fewer SELECTs.
    """
    # Ensure musicbrainz_id is always present in the params dict — sqlite3
    # raises ProgrammingError on a missing named placeholder.
    track = {**track}
    track.setdefault("musicbrainz_id", None)
    row = get_conn().execute(
        """
        INSERT INTO tracks (
            album_id, artist_id, music_folder_id, path, title,
            track_number, disc_number, duration, bitrate, size,
            suffix, content_type, year, genre,
            mtime, content_hash, last_scanned, musicbrainz_id
        ) VALUES (
            :album_id, :artist_id, :music_folder_id, :path, :title,
            :track_number, :disc_number, :duration, :bitrate, :size,
            :suffix, :content_type, :year, :genre,
            :mtime, :content_hash, :last_scanned, :musicbrainz_id
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
            last_scanned = excluded.last_scanned,
            -- Preserve an existing track MBID if the incoming tags don't have
            -- one (e.g. Picard tagged the file once, later untagged copy got
            -- written over it). Symmetric with the album/artist behaviour.
            musicbrainz_id = COALESCE(tracks.musicbrainz_id, excluded.musicbrainz_id)
        RETURNING id
        """,
        track,
    ).fetchone()
    return int(row["id"])


def get_track(track_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            """
        SELECT t.*,
               ar.name AS artist_name,
               al.name AS album_name,
               al.cover_art_id AS cover_art_id
          FROM tracks t
     LEFT JOIN artists ar ON ar.id = t.artist_id
     LEFT JOIN albums  al ON al.id = t.album_id
         WHERE t.id = :id
        """,
            {"id": track_id},
        )
        .fetchone()
    )
    return _row_to_dict(row)


def list_album_tracks(album_id: int) -> List[Dict[str, Any]]:
    """Tracks on an album, in disc/track order.

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
         WHERE t.album_id = :album_id
      ORDER BY t.disc_number NULLS LAST, t.track_number NULLS LAST, t.title COLLATE NOCASE
        """,
            {"album_id": album_id},
        )
        .fetchall()
    )
    return _rows_to_dicts(rows)


def get_existing_paths_for_folder(
    folder_id: int,
) -> Dict[str, Tuple[int, int, int, int]]:
    """Return {path: (id, mtime, size, album_id)} for every track in this folder.

    Scanner uses this to decide which files to skip (mtime+size unchanged) and
    which to remove (path no longer on disk). Crucial for incremental scanning
    on large libraries — we never re-parse a file that hasn't changed.
    """
    rows = (
        get_conn()
        .execute(
            """
            SELECT id, path, mtime, size, album_id FROM tracks
             WHERE music_folder_id = :folder_id
            """,
            {"folder_id": folder_id},
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
    # Named placeholders can't be repeated bare for a variable-length IN
    # list, so we generate :id0, :id1, ... per chunk and zip them into a
    # dict. Slightly wordier than the f"?,?,?" trick but ports cleanly.
    for i in range(0, len(track_ids), 500):
        chunk = track_ids[i : i + 500]
        names = [f"id{j}" for j in range(len(chunk))]
        placeholders = ",".join(f":{n}" for n in names)
        params = dict(zip(names, chunk))
        conn.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", params)


def cleanup_empty_albums_and_artists() -> Tuple[int, int]:
    """Remove albums with 0 tracks and artists with 0 albums.

    Run after a scan that deleted tracks. Returns (albums_deleted, artists_deleted).

    Performance: switched from `id NOT IN (SELECT ...)` to NOT EXISTS. The
    NOT IN form forces SQLite to materialise the whole inner result; the
    correlated NOT EXISTS lets it short-circuit per outer row using the
    existing idx_tracks_album / idx_albums_artist indexes.
    """
    conn = get_conn()
    cur = conn.execute("""
        DELETE FROM albums
         WHERE NOT EXISTS (
             SELECT 1 FROM tracks WHERE tracks.album_id = albums.id
         )
        """)
    albums_deleted = cur.rowcount
    cur = conn.execute("""
        DELETE FROM artists
         WHERE NOT EXISTS (SELECT 1 FROM albums WHERE albums.artist_id = artists.id)
           AND NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.artist_id = artists.id)
        """)
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
    """Subsonic-style search. Returns artists, albums, songs that match.

    NOTE: This uses LIKE with leading wildcard which is index-unfriendly.
    For 500k tracks this gets slow. The right answer is a proper FTS5 virtual
    table — see core/search.py for that path. We keep this LIKE version as a
    fallback for installs that haven't built FTS yet.
    """
    pattern = f"%{query}%"
    conn = get_conn()

    artists = conn.execute(
        """
        SELECT id, name, album_count, musicbrainz_id
          FROM artists
         WHERE name LIKE :pattern COLLATE NOCASE
         LIMIT :limit OFFSET :offset
        """,
        {"pattern": pattern, "limit": artist_count, "offset": artist_offset},
    ).fetchall()

    albums = conn.execute(
        """
        SELECT al.id, al.name, al.year, al.cover_art_id, al.track_count, al.duration,
               al.artist_id, al.musicbrainz_id, al.musicbrainz_releasegroup_id,
               ar.name AS artist_name
          FROM albums al
          JOIN artists ar ON ar.id = al.artist_id
         WHERE al.name LIKE :pattern COLLATE NOCASE
            OR ar.name LIKE :pattern COLLATE NOCASE
         LIMIT :limit OFFSET :offset
        """,
        {"pattern": pattern, "limit": album_count, "offset": album_offset},
    ).fetchall()

    if not query:
        songs = conn.execute(
            """
            SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
                   t.track_number, t.year, t.genre, t.path,
                   t.musicbrainz_id,
                   t.artist_id, ar.name AS artist_name,
                   t.album_id, al.name AS album_name, al.cover_art_id
              FROM tracks t
         LEFT JOIN artists ar ON ar.id = t.artist_id
         LEFT JOIN albums  al ON al.id = t.album_id
             LIMIT :limit OFFSET :offset
            """,
            {"limit": song_count, "offset": song_offset},
        ).fetchall()
    elif get_dialect() == DIALECT_POSTGRES:
        # Postgres full-text: ranked search over the tsvector column
        # populated by schema.postgres.sql's BEFORE INSERT/UPDATE trigger.
        # `websearch_to_tsquery('simple', :query)` accepts Google-style
        # inputs (quoted phrases, `-` negation, OR) and the 'simple' config
        # avoids stemming — same behaviour as the FTS5 default.
        songs = conn.execute(
            """
            SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
                   t.track_number, t.year, t.genre, t.path,
                   t.musicbrainz_id,
                   t.artist_id, ar.name AS artist_name,
                   t.album_id, al.name AS album_name, al.cover_art_id
              FROM tracks t
         LEFT JOIN artists ar ON ar.id = t.artist_id
         LEFT JOIN albums  al ON al.id = t.album_id
             WHERE t.search_tsv @@ websearch_to_tsquery('simple', :query)
          ORDER BY ts_rank(t.search_tsv, websearch_to_tsquery('simple', :query)) DESC
             LIMIT :limit OFFSET :offset
            """,
            {"query": query, "limit": song_count, "offset": song_offset},
        ).fetchall()
    else:
        # FTS5 accepts a small query language (NEAR, AND, OR, prefix `*`,
        # quoted phrases). Passing the raw user input means a query like
        # `"unterminated` or `NEAR` raises sqlite3.OperationalError, which
        # propagates as a 500 and is a trivial DoS / log-flooder. Wrap the
        # whole thing as a phrase: any double-quote inside is doubled per
        # FTS5's quoting rule, so the result is one safe phrase literal.
        fts_query = '"' + query.replace('"', '""') + '"'
        try:
            songs = conn.execute(
                """
                SELECT t.id, t.title, t.duration, t.bitrate, t.size, t.suffix, t.content_type,
                       t.track_number, t.year, t.genre, t.path,
                       t.musicbrainz_id,
                       t.artist_id, ar.name AS artist_name,
                       t.album_id, al.name AS album_name, al.cover_art_id
                  FROM tracks t
                  JOIN virt_fts5 f  ON f.rowid = t.id
             LEFT JOIN artists ar   ON ar.id = t.artist_id
             LEFT JOIN albums  al   ON al.id = t.album_id
                 WHERE virt_fts5 MATCH :query
                 LIMIT :limit OFFSET :offset
                """,
                {"query": fts_query, "limit": song_count, "offset": song_offset},
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 still rejects the input (e.g. an empty phrase): degrade
            # gracefully to an empty result rather than 500ing.
            songs = []

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
    "id, username, password_hash, encrypted_password, is_admin, disabled, "
    "created_at, password_changed_at, " + ", ".join(_USER_ROLE_COLS)
)

# Same but without password_hash — safe to return to the API layer / admin UI.
# We never send hashes to the frontend.
_USER_SELECT_NO_HASH = (
    "id, username, is_admin, disabled, created_at, password_changed_at, "
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
    """Insert a new user. Raises IntegrityError if username is taken."""
    now = int(time.time())
    # RETURNING instead of lastrowid — works on both sqlite3 and psycopg.
    row = get_conn().execute(
        """
        INSERT INTO users (
            username, password_hash, is_admin, created_at, password_changed_at,
            email, scrobbling_enabled, max_bit_rate,
            settings_role, stream_role, download_role, upload_role,
            playlist_role, cover_art_role, comment_role, podcast_role,
            jukebox_role, share_role, video_conversion_role
        ) VALUES (
            :username, :password_hash, :is_admin, :created_at, :password_changed_at,
            :email, :scrobbling_enabled, :max_bit_rate,
            :settings_role, :stream_role, :download_role, :upload_role,
            :playlist_role, :cover_art_role, :comment_role, :podcast_role,
            :jukebox_role, :share_role, :video_conversion_role
        )
        RETURNING id
        """,
        {
            "username": username,
            "password_hash": password_hash,
            "is_admin": int(is_admin),
            "created_at": now,
            "password_changed_at": now,
            "email": email,
            "scrobbling_enabled": int(scrobbling_enabled),
            "max_bit_rate": max_bit_rate,
            "settings_role": int(settings_role),
            "stream_role": int(stream_role),
            "download_role": int(download_role),
            "upload_role": int(upload_role),
            "playlist_role": int(playlist_role),
            "cover_art_role": int(cover_art_role),
            "comment_role": int(comment_role),
            "podcast_role": int(podcast_role),
            "jukebox_role": int(jukebox_role),
            "share_role": int(share_role),
            "video_conversion_role": int(video_conversion_role),
        },
    ).fetchone()
    return int(row["id"])


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            f"SELECT {_USER_SELECT} FROM users WHERE username = :username",
            {"username": username},
        )
        .fetchone()
    )
    return _row_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    row = (
        get_conn()
        .execute(
            f"SELECT {_USER_SELECT_NO_HASH} FROM users WHERE id = :id",
            {"id": user_id},
        )
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
    params: Dict[str, Any] = {"username": username}

    def _add(col: str, val: Any, cast=None) -> None:
        """Append 'col = :col' to fields and the value to params, if val is not None."""
        if val is None:
            return
        fields.append(f"{col} = :{col}")
        # cast converts Python bool to int (SQLite stores booleans as 0/1).
        params[col] = cast(val) if cast else val

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

    cur = get_conn().execute(
        f"UPDATE users SET {', '.join(fields)} WHERE username = :username",
        params,
    )
    return cur.rowcount > 0


def update_user_password(user_id: int, password_hash: str) -> bool:
    """Replace the stored password hash by id. Returns True if the user existed.

    Caller is responsible for wrapping in a `with transaction():` block.
    Manual `conn.commit()` would conflict with psycopg's transaction
    context manager (Postgres autocommit=True mode raises an explicit-
    commit error inside its native transaction).
    """
    conn = get_conn()
    cur = conn.execute(
        """
        UPDATE users
           SET password_hash = :password_hash,
               password_changed_at = :password_changed_at
         WHERE id = :id
        """,
        {
            "password_hash": password_hash,
            "password_changed_at": int(time.time()),
            "id": user_id,
        },
    )
    return cur.rowcount > 0


def update_encrypted_password(user_id: int, value: Optional[str]) -> None:
    """Store or clear the Fernet-encrypted plaintext password used for Subsonic token+salt auth.

    Caller wraps in `with transaction():`. See update_user_password.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE users SET encrypted_password = :value WHERE id = :id",
        {"value": value, "id": user_id},
    )


def set_user_disabled(user_id: int, disabled: bool) -> bool:
    """Set or clear the disabled flag by id. Returns True if the user existed.

    Caller wraps in `with transaction():`. See update_user_password.
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE users SET disabled = :disabled WHERE id = :id",
        {"disabled": int(disabled), "id": user_id},
    )
    return cur.rowcount > 0


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Set or clear the admin flag by id. Returns True if the user existed.

    Caller wraps in `with transaction():`. See update_user_password.
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE users SET is_admin = :is_admin WHERE id = :id",
        {"is_admin": int(is_admin), "id": user_id},
    )
    return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    """Remove a user by id. Returns True if a row was deleted."""
    cur = get_conn().execute(
        "DELETE FROM users WHERE id = :id",
        {"id": user_id},
    )
    return cur.rowcount > 0


def delete_user_by_username(username: str) -> bool:
    """Remove a user by username. Returns True if a row was deleted."""
    cur = get_conn().execute(
        "DELETE FROM users WHERE username = :username",
        {"username": username},
    )
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

    Performance: one round-trip via a single SELECT with scalar subqueries.
    Previously took four separate execute() calls — each one a Python ↔
    SQLite hop. SQLite computes all four aggregates in a single statement
    plan and each COUNT(*) is satisfied directly from the table's b-tree
    metadata.
    """
    row = get_conn().execute(
        """
        SELECT
            (SELECT COUNT(*) FROM artists)                  AS artists,
            (SELECT COUNT(*) FROM albums)                   AS albums,
            (SELECT COUNT(*) FROM tracks)                   AS tracks,
            (SELECT COALESCE(SUM(duration), 0) FROM tracks) AS total_duration_seconds
        """
    ).fetchone()
    return {
        "artists": row["artists"],
        "albums": row["albums"],
        "tracks": row["tracks"],
        "total_duration_seconds": int(row["total_duration_seconds"] or 0),
    }
