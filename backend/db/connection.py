"""
SQLite connection management.

WHY a custom layer instead of SQLAlchemy:
    For this workload (mostly read-heavy single-statement queries on a single
    SQLite file) SQLAlchemy adds overhead and complexity without much benefit.
    We get explicit SQL, predictable performance, and zero ORM surprises.
    Migrating to SQLAlchemy later is straightforward — queries.py is the only
    module that talks SQL directly.

Threading model:
    SQLite connections are per-thread (the default safe mode). FastAPI runs
    handlers in a thread pool, so we use a thread-local connection registry
    and let each thread keep its own connection alive for the request.

WAL mode notes:
    With journal_mode=WAL, readers don't block writers and vice versa. That's
    important during a scan, when the scanner is writing while the API serves
    reads. Without WAL, a long scan would freeze the API.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from backend.config import Settings


# ---------------------------------------------------------------------------
# Thread-local registry. Each thread gets exactly one connection it reuses.
# ---------------------------------------------------------------------------
_local = threading.local()
_db_path: Optional[str] = None  # set once by init_db()


def init_db(settings: Settings) -> None:
    """
    One-time initialisation.

    Stores the database path so later calls to `get_conn()` know where to go,
    and creates the parent directory. Schema creation lives in migrations.py.
    """
    global _db_path
    db_path = Path(settings.database_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = str(db_path)


def _new_connection() -> sqlite3.Connection:
    """
    Open a fresh connection with the pragmas we always want.

    detect_types lets us read TIMESTAMP columns as datetime objects if we ever
    add them. row_factory=Row gives us dict-like access to columns.
    """
    if _db_path is None:
        raise RuntimeError("init_db() must be called before any DB access")
    conn = sqlite3.connect(_db_path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # foreign_keys must be set on every connection — it's not a database-level
    # setting in SQLite.
    conn.execute("PRAGMA foreign_keys = ON;")
    # Bigger cache helps a lot for index lookups on a 100k-track library.
    # 50000 pages * 4096 bytes = ~200MB. Adjust if you're memory-constrained.
    conn.execute("PRAGMA cache_size = -50000;")
    # busy_timeout means writers wait instead of immediately erroring on lock
    # contention. WAL mode minimises contention but doesn't eliminate it.
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def get_conn() -> sqlite3.Connection:
    """
    Return the current thread's connection, creating it on first use.

    Connections are reused for the lifetime of the thread. FastAPI's thread
    pool will recycle threads, so connections eventually close themselves.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _new_connection()
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    Context manager for explicit transactions.

    Usage:
        with transaction() as conn:
            conn.execute(...)
            conn.execute(...)

    On clean exit we COMMIT; on exception we ROLLBACK and re-raise. SQLite
    auto-begins a transaction on the first write, so we just have to manage
    the commit/rollback boundary.
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_thread_connection() -> None:
    """
    Close the connection bound to the current thread, if any.

    Useful in tests and at shutdown. Not normally called per-request — the
    overhead of opening/closing is significant on SQLite.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
