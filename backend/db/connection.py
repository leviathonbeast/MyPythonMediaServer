"""
Database connection management — dialect-aware.

Supports two backends, selected by `settings.resolved_database_url()`:

    sqlite:///./data/library.db
    postgresql://user:pass@host:port/dbname

Threading model:
    Each thread caches its own connection in a `threading.local()` registry
    and reuses it for the thread's lifetime. FastAPI's thread pool eventually
    recycles threads, so connections close themselves. This model works for
    both SQLite (per-thread is required) and Postgres (we don't bother with
    a connection pool — the pool size needed for a single-user music server
    is tiny and a thread-local cache achieves the same effect).

Param-style:
    Every query in `queries.py` uses `:name` named binding. SQLite supports
    that natively. psycopg uses `%(name)s` — so for Postgres connections we
    wrap `execute()`/`executemany()` in `_PgConnection` and translate the
    SQL on the way through with a regex (`:name` → `%(name)s`). This means
    `queries.py` never has to know which dialect is active.

WAL mode notes (SQLite):
    With journal_mode=WAL, readers don't block writers and vice versa.
    Important during a scan, when the scanner writes while the API serves
    reads. Without WAL, a long scan would freeze the API. Postgres has its
    own equivalent behaviour out of the box; no tuning needed.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union
from urllib.parse import urlparse

from backend.config import Settings

# ---------------------------------------------------------------------------
# Dialect identification.
#
# Resolved once at startup by init_db() and exposed via get_dialect() so the
# migrations layer and any dialect-aware query can branch without re-parsing
# the URL each time.
# ---------------------------------------------------------------------------
DIALECT_SQLITE = "sqlite"
DIALECT_POSTGRES = "postgres"
_dialect: Optional[str] = None

# SQLite-specific
_db_path: Optional[str] = None

# Postgres-specific. We store the libpq DSN (after rewriting the scheme
# from `postgresql://` to `postgresql://`, which is a no-op — but we keep
# a single source of truth for what to pass to psycopg.connect()).
_pg_dsn: Optional[str] = None

# Thread-local connection registry. Each thread gets exactly one
# connection it reuses for its whole lifetime.
_local = threading.local()


def init_db(settings: Settings) -> None:
    """
    One-time initialisation. Resolves the dialect from `database_url` (or
    the legacy `database_path` fallback) and stashes the per-dialect state.

    Idempotent — safe to call multiple times in tests.
    """
    global _dialect, _db_path, _pg_dsn

    url = settings.resolved_database_url()
    parsed = urlparse(url)

    if parsed.scheme == "sqlite":
        # sqlite:///./relative or sqlite:////absolute. urlparse leaves
        # the path with a leading slash; strip exactly one for relative
        # paths so Path() doesn't treat them as absolute.
        path = parsed.path
        if path.startswith("/") and not parsed.netloc:
            # urlparse gives sqlite:///./foo.db → path="/./foo.db"
            # and sqlite:////abs/foo.db        → path="//abs/foo.db"
            # The double-slash form is the absolute one; single-slash
            # is the relative form we want to strip.
            if not path.startswith("//"):
                path = path[1:]
            else:
                path = path[1:]  # also strip one for absolute → "/abs/foo.db"
        db_path = Path(path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db_path = str(db_path)
        _pg_dsn = None
        _dialect = DIALECT_SQLITE
    elif parsed.scheme in ("postgresql", "postgres"):
        _db_path = None
        # psycopg accepts the URL form directly.
        _pg_dsn = url
        _dialect = DIALECT_POSTGRES
    else:
        raise ValueError(
            f"Unsupported database URL scheme: {parsed.scheme!r}. "
            "Use sqlite:///path/to/file.db or postgresql://user:pass@host/db."
        )


def get_dialect() -> str:
    """Return the active dialect string. Raises if init_db() hasn't run."""
    if _dialect is None:
        raise RuntimeError("init_db() must be called before any DB access")
    return _dialect


# ---------------------------------------------------------------------------
# SQLite-side.
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_PAGES = -30000  # ~120 MB — enough for hot API queries
_SCANNER_CACHE_PAGES = -40000  # ~160 MB — ample for libraries up to ~150k tracks


def _tune_sqlite(conn: sqlite3.Connection, cache_pages: int) -> None:
    """
    Apply every SQLite-specific PRAGMA we care about to a fresh connection.

    All the dialect-specific knobs are concentrated here. On Postgres these
    have no equivalent — Postgres tunes via postgresql.conf / `ALTER SYSTEM`
    at the server side, not per-connection.
    """
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute(f"PRAGMA cache_size = {int(cache_pages)};")
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA mmap_size = 268435456;")
    conn.execute("PRAGMA temp_store = MEMORY;")


def _new_sqlite_connection(cache_pages: int) -> sqlite3.Connection:
    assert _db_path is not None
    conn = sqlite3.connect(_db_path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
    conn.row_factory = sqlite3.Row
    _tune_sqlite(conn, cache_pages)
    return conn


# ---------------------------------------------------------------------------
# Postgres-side.
#
# We lazy-import psycopg so importing this module on a SQLite-only deployment
# doesn't require psycopg to be installed.
# ---------------------------------------------------------------------------

# `:name` → `%(name)s`. Matches names that start with an alpha or underscore
# and continue with word chars. Doesn't try to dodge `::cast` casts because
# our codebase doesn't use them; if we ever add Postgres-specific casts, the
# regex will need a negative-lookbehind for the preceding `:`.
_NAMED_PARAM_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _translate_named_params(sql: str) -> str:
    """Translate SQLite-style `:name` placeholders to psycopg `%(name)s`."""
    return _NAMED_PARAM_RE.sub(r"%(\1)s", sql)


class _PgConnection:
    """
    Thin wrapper around a psycopg.Connection that mimics enough of the
    sqlite3.Connection surface for queries.py to use it interchangeably:

      * execute(sql, params=None)     → returns a Cursor
      * executemany(sql, params_seq)  → returns a Cursor
      * commit() / rollback() / close()

    Both methods translate `:name` placeholders to `%(name)s` before
    handing the SQL to psycopg. Rows come back as plain dicts (psycopg's
    `dict_row` factory), which supports the `row["col"]` access pattern
    queries.py uses throughout.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Optional[Any] = None) -> Any:
        sql = _translate_named_params(sql)
        if params is None:
            return self._raw.execute(sql)
        return self._raw.execute(sql, params)

    def executemany(self, sql: str, params_seq: Iterable[Any]) -> Any:
        sql = _translate_named_params(sql)
        cur = self._raw.cursor()
        cur.executemany(sql, params_seq)
        return cur

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def _new_pg_connection() -> _PgConnection:
    """
    Open a fresh psycopg connection wrapped in `_PgConnection`.

    Configured with `row_factory=dict_row` so `row["col"]` works the same
    way sqlite3.Row's interface does. `autocommit=False` is the default;
    we rely on explicit transaction() boundaries everywhere.
    """
    import psycopg
    from psycopg.rows import dict_row

    assert _pg_dsn is not None
    raw = psycopg.connect(_pg_dsn, row_factory=dict_row, autocommit=False)
    return _PgConnection(raw)


# ---------------------------------------------------------------------------
# Public API — get_conn / init_thread_connection / transaction
# ---------------------------------------------------------------------------

# The annotated return type is the SQLite union — `_PgConnection` mimics
# enough of sqlite3.Connection's surface that callers don't need to care.
ConnectionType = Union[sqlite3.Connection, _PgConnection]


def _new_connection(cache_pages: int = _DEFAULT_CACHE_PAGES) -> ConnectionType:
    """Open a fresh connection for the active dialect."""
    if _dialect is None:
        raise RuntimeError("init_db() must be called before any DB access")
    if _dialect == DIALECT_SQLITE:
        return _new_sqlite_connection(cache_pages)
    # Postgres ignores the SQLite cache_pages hint — server-side tuning is
    # done via postgresql.conf / shared_buffers / work_mem instead.
    return _new_pg_connection()


def get_conn() -> ConnectionType:
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
    Open (or replace) this thread's connection.

    On SQLite, `cache_pages` lets long-lived worker threads (the scanner)
    request a larger per-connection cache. On Postgres, the parameter is
    accepted-but-ignored — server-side tuning is global.
    """
    existing = getattr(_local, "conn", None)
    if existing is not None:
        existing.close()
    _local.conn = _new_connection(cache_pages=cache_pages)


@contextmanager
def transaction() -> Iterator[ConnectionType]:
    """
    Context manager for explicit transactions.

    Usage:
        with transaction() as conn:
            conn.execute(...)
            conn.execute(...)

    On clean exit we COMMIT, on exception we ROLLBACK and re-raise. Works
    identically on both dialects: SQLite auto-begins on the first write,
    psycopg auto-begins on the first statement when `autocommit=False`.

    IMPORTANT for tests: writes are NOT visible to other database
    connections until commit(). The `with transaction():` pattern is the
    correct way to write data in test setup so that the API (which runs
    on a different thread with its own connection) can see the seeded
    rows.
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_thread_connection() -> None:
    """Close the connection bound to the current thread, if any."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
