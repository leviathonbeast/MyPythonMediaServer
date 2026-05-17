"""
Database exception types — one place callers import from.

Each name here is a TUPLE of `(sqlite3_exception, psycopg_exception)` so
that `except IntegrityError:` catches a uniqueness violation regardless
of which driver is actually serving the connection. Python's `except`
clause accepts a tuple of exception classes natively.

psycopg is imported under a try/except so that SQLite-only installs
don't fail if psycopg isn't present in the environment — in that case
the tuples collapse to a one-element single-driver form.
"""

from __future__ import annotations

import sqlite3

try:
    import psycopg  # noqa: F401 — import only to verify availability
    from psycopg import errors as _pg_errors

    IntegrityError = (sqlite3.IntegrityError, _pg_errors.IntegrityError)
    OperationalError = (sqlite3.OperationalError, _pg_errors.OperationalError)
    DatabaseError = (sqlite3.DatabaseError, _pg_errors.DatabaseError)
except ImportError:
    IntegrityError = sqlite3.IntegrityError  # type: ignore[assignment]
    OperationalError = sqlite3.OperationalError  # type: ignore[assignment]
    DatabaseError = sqlite3.DatabaseError  # type: ignore[assignment]


__all__ = ["IntegrityError", "OperationalError", "DatabaseError"]
