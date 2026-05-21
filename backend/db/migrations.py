"""
Schema migrations.

Two responsibilities:

  1. On first install: lay down the full schema by running schema.sql
     (or schema.postgres.sql when that path is wired up).
  2. After that: apply any incremental, numbered schema changes added
     after the initial install.

WHY roll our own instead of Alembic:
    Alembic is great for SQLAlchemy. We don't use SQLAlchemy. A version-int
    + list-of-callables is sufficient for a project of this size and avoids
    a heavy dependency.

Historical note:
    Versions 1-16 used to be individual ALTER TABLE / ADD COLUMN / CREATE
    INDEX steps. On this branch they've been folded into a fresh schema.sql
    so a fresh install creates the full current schema in one pass. The
    version-tracking infrastructure stays for any *future* schema changes,
    which should be added as numbered migrations starting at version 4.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, List, Tuple

from backend.config import get_settings
from .connection import DIALECT_POSTGRES, get_conn, get_dialect, transaction

# Each migration is (version, callable). Callable receives a connection and
# does whatever it needs. Versions must be monotonically increasing.
Migration = Tuple[int, Callable[[sqlite3.Connection], None]]


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """Create the entire current schema from the per-dialect schema file.

    `schema.sql` for SQLite, `schema.postgres.sql` for Postgres. Both
    contain the full current schema squashed from the original
    migration sequence.

    The `_PgConnection` wrapper exposes `executescript()` so this call
    works identically on both backends.
    """
    if get_dialect() == DIALECT_POSTGRES:
        schema_name = "schema.postgres.sql"
    else:
        schema_name = "schema.sql"
    schema_path = Path(__file__).parent / schema_name
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
    cur = conn.execute("SELECT COUNT(*) AS n FROM users")
    if cur.fetchone()["n"] > 0:
        return  # already seeded — never touch existing users

    if not settings.admin_password:
        raise RuntimeError(
            "First-run setup requires MUSE_ADMIN_PASSWORD (or admin_password "
            "in config.yaml). Refusing to seed an admin user with a default "
            "password — that's how home servers end up trivially owned. "
            "Set the env var and restart."
        )

    now = int(time.time())
    conn.execute(
        """
        INSERT INTO users (
            username, password_hash, is_admin,
            created_at, password_changed_at
        ) VALUES (
            :username, :password_hash, 1,
            :created_at, :created_at
        )
        """,
        {
            "username": settings.admin_username,
            "password_hash": hash_password(settings.admin_password),
            "created_at": now,
        },
    )


def _migration_003_seed_music_folders(conn: sqlite3.Connection) -> None:
    """
    Insert configured music folders.

    Re-run safely: ON CONFLICT DO NOTHING means already-present paths
    are skipped. If the user removes a path from config we leave the
    row (and its tracks) in place so a typo doesn't nuke their library.
    """
    settings = get_settings()
    for path in settings.music_folders:
        name = Path(path).name or path
        conn.execute(
            """
            INSERT INTO music_folders (name, path)
            VALUES (:name, :path)
            ON CONFLICT (path) DO NOTHING
            """,
            {"name": name, "path": path},
        )


def _migration_004_add_current_pos_to_play_queues(conn: sqlite3.Connection) -> None:
    """ALTER TABLE play_queues ADD COLUMN current_position INTEGER."""
    conn.execute("ALTER TABLE play_queues ADD COLUMN current_position INTEGER")


def _migration_user_external_accounts(conn: sqlite3.Connection) -> None:
    """LAST FM scrobble"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_external_accounts(user_id INTEGER NOT NULL, service TEXT NOT NULL,
        auth_token TEXT NOT NULL, username TEXT, linked_at INTEGER NOT NULL, PRIMARY KEY (user_id, service), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE);

        """)


def _migration_006_add_track_stream_props(conn: sqlite3.Connection) -> None:
    """Add channels / sample_rate / bit_depth to tracks.

    These back the OpenSubsonic song fields channelCount/samplingRate/bitDepth
    and the getTranscodeDecision source-stream analysis. All nullable: existing
    rows stay NULL until a rescan repopulates them, and bit_depth is always
    NULL for lossy formats (it's meaningless there).

    One ALTER per column — SQLite only adds a single column per statement, and
    splitting keeps it portable to Postgres too. ADD COLUMN with no default is
    instant on both engines (no table rewrite).
    """
    conn.execute("ALTER TABLE tracks ADD COLUMN channels INTEGER")
    conn.execute("ALTER TABLE tracks ADD COLUMN sample_rate INTEGER")
    conn.execute("ALTER TABLE tracks ADD COLUMN bit_depth INTEGER")


def _migration_007_add_track_features(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_features(track_id INTEGER NOT NULL,
        features TEXT NOT NULL, feature_version INTEGER NOT NULL, analysed_at INTEGER NOT NULL, PRIMARY KEY(track_id), FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE);
        
        """)


# Order matters. Append new migrations; never reorder existing ones.
# Future schema changes start at version 4.
MIGRATIONS: List[Migration] = [
    (1, _migration_001_initial),
    (2, _migration_002_seed_admin),
    (3, _migration_003_seed_music_folders),
    (4, _migration_004_add_current_pos_to_play_queues),
    (5, _migration_user_external_accounts),
    (6, _migration_006_add_track_stream_props),
    (7, _migration_007_add_track_features),
]


def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    # schema_version is created by migration 1, so we have to check whether
    # the table exists at all before querying it. The two dialects expose
    # their table catalogues differently:
    #   * SQLite: SELECT name FROM sqlite_master WHERE type='table' ...
    #   * Postgres: SELECT FROM pg_catalog.pg_class / information_schema
    # `information_schema.tables` is the standard cross-dialect option and
    # is supported by both — but SQLite implements only a subset, so we
    # keep the dialect branch for clarity.
    if get_dialect() == DIALECT_POSTGRES:
        cur = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'schema_version'"
        )
    else:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        )
    if cur.fetchone() is None:
        return 0
    cur = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
    return int(cur.fetchone()["v"])


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
        # Each migration runs inside a transaction() so a partial-failure
        # rolls back cleanly and the schema_version row only gets bumped
        # on success. The helper handles both dialects — SQLite via manual
        # commit/rollback, Postgres via psycopg's native transaction
        # context (since we run with autocommit=True, raw commit/rollback
        # would be no-ops).
        with transaction():
            fn(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (:version)",
                {"version": version},
            )
