"""
Database & filesystem garbage collection.

Over time a Muse install accumulates four kinds of cruft that aren't
caught by ordinary scan-time deletion:

  1. Empty albums   — albums with zero tracks. Created when every track on
                       an album gets re-tagged or moved. The scanner already
                       calls cleanup, but only when at least one track was
                       deleted *that scan*; an album that became empty
                       through retagging-without-disk-deletion was missed.
  2. Empty artists  — artists with no albums and no tracks. Same root cause.
  3. Orphaned starred rows — the `starred` table is polymorphic: it stores
                       (target_type, target_id) pointing at one of three
                       tables, so SQLite can't enforce a foreign key for
                       it. When the underlying album/artist/track is
                       deleted, the starred row dangles forever.
  4. Orphan artwork files — every cover image lives on disk under
                       ARTWORK_CACHE_DIR/<sha1>.<ext>. When all albums
                       referencing one are deleted, the file stays.
                       Deduplication-by-content-hash limits the damage,
                       but a year of re-scanning a churning library
                       leaves hundreds of MB of orphans.

Plus two engine-level concerns:

  - SQLite never reclaims pages on its own. After a 50k-track delete cycle,
    the .db file is roughly 2× what it should be. `VACUUM` rewrites the
    file compactly; it's expensive (acquires an exclusive lock for the
    duration, so all readers/writers block) and so we keep it OPT-IN, not
    automatic.

  - Long-running write workloads (a big scan) can grow the -wal file.
    `PRAGMA wal_checkpoint(TRUNCATE)` is cheap and safe to run after each
    scan; we do that automatically.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Set

from backend.config import get_settings
from backend.db import get_conn, queries, transaction

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass — surfaced through the API so the UI can render it.
# ---------------------------------------------------------------------------

@dataclass
class GcResult:
    started_at: float
    finished_at: float
    duration_seconds: float

    empty_albums_removed: int = 0
    empty_artists_removed: int = 0
    dangling_starred_removed: int = 0
    orphan_artwork_files_removed: int = 0
    orphan_artwork_bytes_freed: int = 0

    wal_checkpointed: bool = False
    vacuumed: bool = False
    db_size_before_bytes: int = 0
    db_size_after_bytes: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_gc(*, vacuum: bool = False) -> GcResult:
    """
    Run all routine cleanup. Safe to call any time the DB isn't being
    written to actively (i.e. don't run during a scan). Cheap; finishes
    in well under a second on libraries up to ~500k tracks.

    Pass `vacuum=True` to additionally rewrite the database file
    compactly. VACUUM acquires an exclusive lock for its duration, which
    can be many seconds on a big library, so it's opt-in.
    """
    started = time.time()
    settings = get_settings()
    db_path = settings.database_path

    size_before = _file_size(db_path)
    conn = get_conn()

    log.info("gc: starting (vacuum=%s)", vacuum)

    # ---- 1. Empty albums and artists -----------------------------------
    # Always run, not gated on whether the scanner just deleted anything.
    # An album becomes empty by retagging just as easily as by file-loss.
    with transaction():
        empty_albums, empty_artists = queries.cleanup_empty_albums_and_artists()

    # ---- 2. Dangling starred rows --------------------------------------
    dangling_starred = _cleanup_dangling_starred(conn)

    # ---- 3. Orphan artwork files on disk -------------------------------
    art_files_removed, art_bytes_freed = _cleanup_orphan_artwork(
        artwork_dir=settings.artwork_cache_dir,
    )

    # ---- 4. Engine maintenance -----------------------------------------
    # WAL checkpoint is cheap and safe; always run.
    wal_done = _wal_checkpoint(conn)

    vacuumed = False
    if vacuum:
        # VACUUM cannot run inside a transaction; sqlite3 also requires
        # autocommit mode, so we set isolation_level=None temporarily.
        # Note that this acquires an exclusive lock — every other
        # connection will block. Acceptable for an explicit admin action.
        prior_isolation = conn.isolation_level
        try:
            conn.isolation_level = None
            conn.execute("VACUUM")
            vacuumed = True
        finally:
            conn.isolation_level = prior_isolation

    size_after = _file_size(db_path)
    finished = time.time()

    result = GcResult(
        started_at=started,
        finished_at=finished,
        duration_seconds=round(finished - started, 3),
        empty_albums_removed=empty_albums,
        empty_artists_removed=empty_artists,
        dangling_starred_removed=dangling_starred,
        orphan_artwork_files_removed=art_files_removed,
        orphan_artwork_bytes_freed=art_bytes_freed,
        wal_checkpointed=wal_done,
        vacuumed=vacuumed,
        db_size_before_bytes=size_before,
        db_size_after_bytes=size_after,
    )

    log.info(
        "gc: done in %.2fs — albums=%d artists=%d starred=%d artwork=%d files (%d bytes) "
        "wal=%s vacuum=%s db=%d→%d bytes",
        result.duration_seconds,
        empty_albums, empty_artists, dangling_starred,
        art_files_removed, art_bytes_freed,
        wal_done, vacuumed, size_before, size_after,
    )

    return result


# ---------------------------------------------------------------------------
# Implementation details
# ---------------------------------------------------------------------------

def _cleanup_dangling_starred(conn) -> int:
    """
    Remove `starred` rows whose target no longer exists.

    The starred table is polymorphic — `target_type` is 'track', 'album',
    or 'artist' — which is why SQLite can't enforce this with a foreign
    key. We simulate the constraint here.
    """
    total = 0
    with transaction():
        for kind, table in (("track", "tracks"), ("album", "albums"), ("artist", "artists")):
            cur = conn.execute(
                f"DELETE FROM starred "
                f"WHERE target_type = ? "
                f"  AND target_id NOT IN (SELECT id FROM {table})",
                (kind,),
            )
            total += cur.rowcount
    return total


def _cleanup_orphan_artwork(artwork_dir: str) -> tuple[int, int]:
    """
    Delete files in the artwork cache that are no longer referenced.

    We treat the file's basename (without extension) as the cover-art id,
    because that's exactly how `artwork.store_artwork()` writes them
    (sha1[:16].ext). Anything in the cache dir whose basename is NOT in
    `albums.cover_art_id` is unreachable and can be deleted.

    Returns (files_removed, bytes_freed).
    """
    if not os.path.isdir(artwork_dir):
        return 0, 0

    referenced: Set[str] = _referenced_cover_art_ids()

    removed = 0
    bytes_freed = 0
    try:
        with os.scandir(artwork_dir) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                name = entry.name
                # Strip extension to compare against cover_art_id.
                dot = name.rfind(".")
                stem = name[:dot] if dot >= 0 else name
                if stem in referenced:
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                try:
                    os.unlink(entry.path)
                    removed += 1
                    bytes_freed += size
                except OSError as e:
                    log.debug("gc: cannot remove orphan artwork %s: %s", entry.path, e)
    except OSError as e:
        log.warning("gc: cannot scan artwork cache %s: %s", artwork_dir, e)

    return removed, bytes_freed


def _referenced_cover_art_ids() -> Set[str]:
    """All cover_art_id values currently referenced by the albums table."""
    rows = get_conn().execute(
        "SELECT DISTINCT cover_art_id FROM albums WHERE cover_art_id IS NOT NULL"
    ).fetchall()
    return {row[0] for row in rows if row[0]}


def _wal_checkpoint(conn) -> bool:
    """
    Run a TRUNCATE checkpoint on the WAL.

    Returns True if it ran, False if WAL mode isn't active (e.g. the
    test suite forced rollback journal mode).
    """
    try:
        # The result is (busy, log_pages, checkpointed_pages). A non-zero
        # `busy` means readers held us off, but we still get cleanup on
        # the parts we could.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return True
    except Exception as e:
        log.debug("gc: wal_checkpoint failed: %s", e)
        return False


def _file_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0
