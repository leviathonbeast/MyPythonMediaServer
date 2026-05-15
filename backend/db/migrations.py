"""
Schema migrations.

WHY roll our own instead of using Alembic:
    Alembic is great for SQLAlchemy. We don't use SQLAlchemy. A version-int +
    list-of-callables is sufficient for a project of this size and avoids a
    heavy dependency. If the schema gets complicated we can swap this out.

How it works:
    schema_version table stores a single integer. On startup we read it,
    compare against MIGRATIONS, and apply any whose version > current.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, List, Tuple

from backend.config import Settings, get_settings
from .connection import get_conn

# Each migration is (version, callable). Callable receives a connection and
# does whatever it needs. Versions must be monotonically increasing.
Migration = Tuple[int, Callable[[sqlite3.Connection], None]]


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """Create initial schema from schema.sql."""
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql)


def _migration_002_seed_admin(conn: sqlite3.Connection) -> None:
    """
    Create the bootstrap admin user.

    We do this in a migration (not in app code) so it runs exactly once:
    re-running the server with a different MUSE_ADMIN_PASSWORD won't reset
    the password later, which matches user expectations.
    """
    # Local import — bcrypt is a heavy dep we'd rather not pull in at module
    # import time on workers that never run migrations.
    from backend.core.auth import hash_password

    settings = get_settings()
    cur = conn.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] > 0:
        return  # already seeded — never touch existing users

    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
        (
            settings.admin_username,
            hash_password(settings.admin_password),
            int(time.time()),
        ),
    )


def _migration_003_seed_music_folders(conn: sqlite3.Connection) -> None:
    """
    Insert configured music folders.

    Re-run safely: only inserts paths that aren't already there. If the user
    removes a path from config we leave the row (and its tracks) in place so
    a typo doesn't nuke their library.
    """
    settings = get_settings()
    for path in settings.music_folders:
        name = Path(path).name or path
        conn.execute(
            "INSERT OR IGNORE INTO music_folders (name, path) VALUES (?, ?)",
            (name, path),
        )


def _migration_005_user_roles(conn: sqlite3.Connection) -> None:
    """
    Add Subsonic/OpenSubsonic user-role and preference columns.

    Defaults follow the Subsonic 1.16.1 spec:
      streamRole / settingsRole / playlistRole — true for all users
      All other roles — false (admin grants them explicitly)
    We use try/except per column so the migration is re-runnable if a
    previous partial run left some columns in place.
    """
    columns = [
        ("email", "TEXT"),
        ("scrobbling_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("max_bit_rate", "INTEGER NOT NULL DEFAULT 0"),
        ("settings_role", "INTEGER NOT NULL DEFAULT 1"),
        ("stream_role", "INTEGER NOT NULL DEFAULT 1"),
        ("download_role", "INTEGER NOT NULL DEFAULT 0"),
        ("upload_role", "INTEGER NOT NULL DEFAULT 0"),
        ("playlist_role", "INTEGER NOT NULL DEFAULT 1"),
        ("cover_art_role", "INTEGER NOT NULL DEFAULT 0"),
        ("comment_role", "INTEGER NOT NULL DEFAULT 0"),
        ("podcast_role", "INTEGER NOT NULL DEFAULT 0"),
        ("jukebox_role", "INTEGER NOT NULL DEFAULT 0"),
        ("share_role", "INTEGER NOT NULL DEFAULT 0"),
        ("video_conversion_role", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in columns:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # column already present — idempotent


def _migration_004_album_release_type(conn: sqlite3.Connection) -> None:
    """Add `release_type` column to albums.

    Picard / MusicBrainz tag releases with a primary type (album, single,
    ep, compilation, live, ...). When the scanner finds the tag we now
    persist it on the album row so the artist page can group albums into
    Albums / EPs / Singles / etc. Untagged albums get NULL and are
    treated as "album" for grouping purposes.
    """
    # SQLite's ALTER TABLE ADD COLUMN is cheap and doesn't rewrite the
    # table. Existing rows get NULL. The scanner will start populating
    # the column on the next scan; users can also force a re-tag via
    # rescan + GC if they care.
    conn.execute("ALTER TABLE albums ADD COLUMN release_type TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_albums_release_type ON albums(release_type)"
    )


def _migration_006_password_changed_at(conn: sqlite3.Connection) -> None:
    """
    Add `password_changed_at` to users so the admin UI can show when each
    user last rotated their password. Backfill existing rows with their
    `created_at` value — we don't know the real change date for accounts
    that predate this column, but that's the best approximation.
    """
    try:
        conn.execute("ALTER TABLE users ADD COLUMN password_changed_at INTEGER")
    except Exception:
        pass  # idempotent — column may already exist on a partial run
    conn.execute(
        "UPDATE users SET password_changed_at = created_at "
        "WHERE password_changed_at IS NULL"
    )


def _migration_007_fts5(conn: sqlite3.Connection) -> None:
    """
    add full text search table storing title, genre from tracks table and artist_name from artists and
    album_name from albums linking back to original via tracks.id via fts5 rowid


    """
    conn.execute(""" CREATE VIRTUAL TABLE IF NOT EXISTS virt_fts5 USING fts5(
        
        title,
        genre,
        artist_name,
        album_name,

        content='',
        content_rowid='id'
    )""")

    conn.execute("""
        INSERT INTO virt_fts5(rowid, title, genre, artist_name, album_name)
        SELECT tracks.id, tracks.title, tracks.genre, artists.name, albums.name
        FROM tracks
        LEFT JOIN artists ON tracks.artist_id = artists.id
        LEFT JOIN albums ON tracks.album_id = albums.id
        """)

    conn.execute("""
    CREATE TRIGGER fts5_track_insert AFTER INSERT ON tracks 
    BEGIN
    INSERT INTO virt_fts5(rowid, title, genre, artist_name, album_name)
    VALUES (
    NEW.id,
    NEW.title,
    NEW.genre,
    (SELECT name FROM artists WHERE id = NEW.artist_id),
    (SELECT name FROM albums WHERE id = NEW.album_id)
        );
    END;
    """)

    conn.execute("""
    CREATE TRIGGER fts5_track_delete AFTER DELETE ON tracks 
    BEGIN
        DELETE FROM virt_fts5 WHERE rowid = OLD.id;
    END;
    """)

    conn.execute(""" 
    CREATE TRIGGER fts5_track_update AFTER UPDATE ON tracks 
    BEGIN
        DELETE FROM virt_fts5 WHERE rowid = OLD.id;

        INSERT INTO virt_fts5(rowid, title, genre, artist_name, album_name)
        VALUES (
        NEW.id,
        NEW.title,
        NEW.genre,
        (SELECT name FROM artists WHERE id = NEW.artist_id),
        (SELECT name FROM albums WHERE id = NEW.album_id)
        );
    END;

    """)


def _migration_008_artist_image(conn: sqlite3.Connection) -> None:
    """Add `image_id` to artists for cached artist photos.

    Same hash format as `albums.cover_art_id` — the file lives in the same
    artwork cache dir, so `getCoverArt` can serve both transparently and
    clients get the existing one-year immutable cache headers for free.
    Populated by the recovery sweep (scanner.recover_missing_artwork) which
    fetches each artist's photo from Deezer once and stores the bytes
    locally; from then on every request hits our cache, not Deezer's CDN.
    """
    try:
        conn.execute("ALTER TABLE artists ADD COLUMN image_id TEXT")
    except Exception:
        pass  # idempotent — column may already exist on a partial run


def _migration_011_user_disabled(conn: sqlite3.Connection) -> None:
    """
    Add `disabled` flag to users so accounts can be locked without deletion.

    Disabled users still exist in getUsers / getUser responses (the spec has
    no notion of disabled), but auth always fails for them with the standard
    ERR_AUTH (40) — indistinguishable from a wrong password to clients.
    """
    try:
        conn.execute(
            "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass  # idempotent — column may already exist on a partial run


def _migration_010_play_queue(conn: sqlite3.Connection) -> None:
    """
    Per-user play queue for Subsonic savePlayQueue / getPlayQueue.

    One row per user in play_queues (overwritten on every save), plus a
    child table holding the ordered list of track ids. Splitting the list
    into its own table keeps ordering and joins natural — stuffing ids into
    a CSV column would force string parsing for every read.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS play_queues (
            user_id      INTEGER PRIMARY KEY,
            current_id   INTEGER,
            position_ms  INTEGER NOT NULL DEFAULT 0,
            changed_at   INTEGER NOT NULL,
            changed_by   TEXT NOT NULL,
            FOREIGN KEY (user_id)    REFERENCES users(id)  ON DELETE CASCADE,
            FOREIGN KEY (current_id) REFERENCES tracks(id) ON DELETE SET NULL
        )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS play_queue_entries (
            user_id  INTEGER NOT NULL,
            position INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, position),
            FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """)


def _migration_009_encrypted_password(conn: sqlite3.Connection) -> None:
    """Add `encrypted_password` to users for Subsonic token+salt verification.

    Subsonic's token+salt auth (t=md5(password+salt)&s=salt) requires the
    server to know the plaintext password to recompute the expected token.
    Previously the server cached it in memory only (lost on restart, seeded
    from the insecure ?p= URL param). This column stores a Fernet-encrypted
    copy so token+salt works across restarts without needing ?p= on the wire.

    NULL for existing users until their next web login or password change.
    """
    try:
        conn.execute("ALTER TABLE users ADD COLUMN encrypted_password TEXT")
    except Exception:
        pass  # idempotent


def _migration_012_album_sort_name(conn: sqlite3.Connection) -> None:
    """Add albums.sort_name for clean alphabetical ordering.

    Without this column, alphabeticalByName puts albums starting with
    punctuation at the top of the list. sort_name strips leading non-alphanumerics
    and English articles so '$ome $exy $ongs' sorts under 'o' and 'The Wall'
    sorts under 'W'.
    """
    from .queries import normalize_sort_name

    try:
        conn.execute("ALTER TABLE albums ADD COLUMN sort_name TEXT")
    except Exception:
        pass  # idempotent

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_albums_sort ON albums(sort_name COLLATE NOCASE)"
    )

    rows = conn.execute("SELECT id, name FROM albums WHERE sort_name IS NULL").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE albums SET sort_name = ? WHERE id = ?",
            (normalize_sort_name(row["name"]), row["id"]),
        )


def _migration_014_resort_apple_music_style(conn: sqlite3.Connection) -> None:
    """Re-backfill albums.sort_name with the Apple Music sort convention.

    Earlier migrations stripped leading punctuation so '$ome' sorted under 'o'.
    Apple Music instead keeps the leading symbol and pushes the whole name to
    the end of the A-Z list. The current normalize_sort_name reflects that.
    """
    from .queries import normalize_sort_name

    rows = conn.execute("SELECT id, name FROM albums").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE albums SET sort_name = ? WHERE id = ?",
            (normalize_sort_name(row["name"]), row["id"]),
        )


def _migration_015_performance_indexes(conn: sqlite3.Connection) -> None:
    """
    Indexes for queries that EXPLAIN QUERY PLAN showed as full-table SCANs.

    What and why:
      * tracks(genre): list_song_by_genre, list_random_songs (genre filter),
        list_genre_count all currently scan tracks. Partial index because
        most rows have a non-NULL genre and the index is half the size.
      * tracks(year): list_random_songs year filter.
      * tracks(album_id) covering on (cover_art_id, year, created_at):
        the artists-index "pick the best cover for this artist" lookup
        becomes index-only with the rewrite in queries.py.
      * play_counts(track_id): frequent/recent album sort joins
        play_counts on track_id; without this SQLite builds an automatic
        temp index every call (visible in EXPLAIN as 'AUTOMATIC COVERING INDEX').
      * starred(user_id, starred_at DESC): get_starred_items orders by
        starred_at DESC for a single user; the composite avoids a temp sort.
      * idx_tracks_path is redundant — the UNIQUE constraint already
        provides an index on path. Drop to save space and write overhead.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_genre "
        "ON tracks(genre COLLATE NOCASE) WHERE genre IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_year ON tracks(year)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_play_counts_track ON play_counts(track_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_starred_user_at "
        "ON starred(user_id, starred_at DESC)"
    )
    # Composite that lets the artist-index cover-art lookup hit a single
    # index without touching the table rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_albums_artist_year "
        "ON albums(artist_id, year DESC, created_at DESC)"
    )
    # Drop the duplicate path index — UNIQUE(path) already covers lookups.
    conn.execute("DROP INDEX IF EXISTS idx_tracks_path")
    # ANALYZE updates the planner stats so the new indexes are actually used.
    conn.execute("ANALYZE")


def _migration_013_resort_symbols_last(conn: sqlite3.Connection) -> None:
    """Re-backfill albums.sort_name so symbol-leading names sort after Z.

    Migration 012 left names like '&' at the top of alphabeticalByName lists.
    The normalize_sort_name helper now prefixes such names with '~' so they
    fall to the end. This re-runs the backfill across every album.
    """
    from .queries import normalize_sort_name

    rows = conn.execute("SELECT id, name FROM albums").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE albums SET sort_name = ? WHERE id = ?",
            (normalize_sort_name(row["name"]), row["id"]),
        )


# Order matters. Append new migrations; never reorder existing ones.
MIGRATIONS: List[Migration] = [
    (1, _migration_001_initial),
    (2, _migration_002_seed_admin),
    (3, _migration_003_seed_music_folders),
    (4, _migration_004_album_release_type),
    (5, _migration_005_user_roles),
    (6, _migration_006_password_changed_at),
    (7, _migration_007_fts5),
    (8, _migration_008_artist_image),
    (9, _migration_009_encrypted_password),
    (10, _migration_010_play_queue),
    (11, _migration_011_user_disabled),
    (12, _migration_012_album_sort_name),
    (13, _migration_013_resort_symbols_last),
    (14, _migration_014_resort_apple_music_style),
    (15, _migration_015_performance_indexes),
]


def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    # The schema_version table itself is created by migration 1, so we have to
    # check if it exists first.
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        return 0
    cur = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    return int(cur.fetchone()[0])


def run_migrations() -> None:
    """
    Apply all pending migrations in order.

    Safe to call on every startup. Idempotent.
    """
    conn = get_conn()
    current = _current_version(conn)

    for version, fn in MIGRATIONS:
        if version <= current:
            continue
        # Each migration runs in its own transaction so a failure rolls back
        # cleanly and the schema_version row only gets bumped on success.
        try:
            fn(conn)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
