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


_DEFAULT_CACHE_PAGES = -30000  # ~120 MB — enough for hot API queries
_SCANNER_CACHE_PAGES = -40000  # ~160 MB — ample for libraries up to ~150k tracks


def _new_connection(cache_pages: int = _DEFAULT_CACHE_PAGES) -> sqlite3.Connection:
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
    # journal_mode is a *database-level* setting (persists across opens) but
    # setting it on every connection is safe + idempotent. Without WAL the
    # scanner's commit and any API write serialise on a single exclusive
    # lock — a long scan reliably aborts the first time a login or progress
    # poll arrives. WAL lets readers and writers proceed concurrently.
    conn.execute("PRAGMA journal_mode = WAL;")
    # synchronous=NORMAL is the standard pairing with WAL: fsync only at
    # checkpoint boundaries, not on every COMMIT. Safe for crash recovery
    # (the WAL replays on next open) and ~5× faster for write-heavy
    # workloads like a full library scan.
    conn.execute("PRAGMA synchronous = NORMAL;")
    # Negative value = page count; SQLite page size is 4096 bytes.
    # API threads use the default (120 MB); the scanner thread requests more
    # via init_thread_connection() because it reads the entire library.
    conn.execute(f"PRAGMA cache_size = {int(cache_pages)};")
    # busy_timeout means writers wait instead of immediately erroring on lock
    # contention. With WAL enabled this rarely fires; kept as a safety net
    # for the very brief windows where two writers contend (e.g. scanner
    # commit + a user updating their playlist at the same instant).
    conn.execute("PRAGMA busy_timeout = 10000;")
    # Memory-mapped IO for reads. SQLite serves index/table pages straight
    # from the kernel page cache without a userspace copy. 256 MB is plenty
    # for libraries up to ~500k tracks; the OS reclaims it under pressure.
    conn.execute("PRAGMA mmap_size = 268435456;")
    # Keep TEMP B-TREEs (group-by/order-by spill, automatic indexes) in RAM
    # rather than on disk. Affects every aggregating browse query.
    conn.execute("PRAGMA temp_store = MEMORY;")
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


def init_thread_connection(cache_pages: int) -> None:
    """
    Open (or replace) this thread's connection with an explicit cache size.

    Call this at the very start of any long-lived worker thread before the
    first get_conn() use. The scanner thread calls it with _SCANNER_CACHE_PAGES
    so it gets a larger cache without inflating every API thread's footprint.
    Any existing connection for this thread is closed first.
    """
    existing = getattr(_local, "conn", None)
    if existing is not None:
        existing.close()
    _local.conn = _new_connection(cache_pages=cache_pages)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    Context manager for explicit transactions.

    Usage:
        with transaction() as conn:
            conn.execute(...)
            conn.execute(...)

    A "transaction" groups several database writes so they either ALL succeed
    or ALL fail together (atomicity). For example, when the scanner writes an
    artist, album, and 12 tracks, it does all of them inside one transaction.
    If the server crashes halfway through, the partial data is rolled back
    automatically — you never end up with an album that has no artist.

    On clean exit we COMMIT (make the writes permanent and visible to other
    connections). On exception we ROLLBACK and re-raise the error. SQLite
    auto-begins a transaction on the first write, so we just have to manage
    the commit/rollback boundary.

    IMPORTANT for tests: writes are NOT visible to other database connections
    until you call commit(). The `with transaction():` pattern is the correct
    way to write data in test setup so that the API (which runs on a different
    thread with its own connection) can see the seeded rows.
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
