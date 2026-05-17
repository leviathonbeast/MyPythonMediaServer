"""
Database exception types — one place callers import from.

Today these are aliases for the sqlite3 driver's exceptions. If we ever
swap the storage backend (Postgres, MariaDB, ...) only this file changes:
the right-hand side gets repointed at the new driver's exceptions and
every `from backend.db.errors import IntegrityError` continues to work.

Centralising avoids the situation where a future port has to grep every
`except sqlite3.IntegrityError` in the codebase and rewrite it case by
case. The cost is one tiny file; the win is a clean cut-over surface.
"""

from sqlite3 import (
    IntegrityError,
    OperationalError,
    DatabaseError,
)

__all__ = ["IntegrityError", "OperationalError", "DatabaseError"]
