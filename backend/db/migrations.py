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
from .connection import get_conn

# Each migration is (version, callable). Callable receives a connection and
# does whatever it needs. Versions must be monotonically increasing.
Migration = Tuple[int, Callable[[sqlite3.Connection], None]]


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """Create the entire current schema from the per-dialect schema file.

    The file is `schema.sql` for SQLite. When the Postgres path is wired
    up this function will pick `schema.postgres.sql` based on the
    configured dialect.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    # SQLite's executescript runs multiple `;`-separated statements in one
    # call. psycopg3's execute() can do the same when params are None, so
    # the dialect-aware version of this function will be a one-line branch.
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


# Order matters. Append new migrations; never reorder existing ones.
# Future schema changes start at version 4.
MIGRATIONS: List[Migration] = [
    (1, _migration_001_initial),
    (2, _migration_002_seed_admin),
    (3, _migration_003_seed_music_folders),
]


def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    # schema_version is created by migration 1, so we have to check whether
    # the table exists at all before querying it.
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
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (:version)",
                {"version": version},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
