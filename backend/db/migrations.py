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
        (settings.admin_username, hash_password(settings.admin_password), int(time.time())),
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
        ("email",                 "TEXT"),
        ("scrobbling_enabled",    "INTEGER NOT NULL DEFAULT 0"),
        ("max_bit_rate",          "INTEGER NOT NULL DEFAULT 0"),
        ("settings_role",         "INTEGER NOT NULL DEFAULT 1"),
        ("stream_role",           "INTEGER NOT NULL DEFAULT 1"),
        ("download_role",         "INTEGER NOT NULL DEFAULT 0"),
        ("upload_role",           "INTEGER NOT NULL DEFAULT 0"),
        ("playlist_role",         "INTEGER NOT NULL DEFAULT 1"),
        ("cover_art_role",        "INTEGER NOT NULL DEFAULT 0"),
        ("comment_role",          "INTEGER NOT NULL DEFAULT 0"),
        ("podcast_role",          "INTEGER NOT NULL DEFAULT 0"),
        ("jukebox_role",          "INTEGER NOT NULL DEFAULT 0"),
        ("share_role",            "INTEGER NOT NULL DEFAULT 0"),
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


# Order matters. Append new migrations; never reorder existing ones.
MIGRATIONS: List[Migration] = [
    (1, _migration_001_initial),
    (2, _migration_002_seed_admin),
    (3, _migration_003_seed_music_folders),
    (4, _migration_004_album_release_type),
    (5, _migration_005_user_roles),
    (6, _migration_006_password_changed_at),
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
