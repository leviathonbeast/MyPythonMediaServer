"""
Sonic feature analysis pass.

Walks the library, runs `similarity.extract_features` on each track, and stores
the resulting vector via `queries.upsert_track_features`. This is what actually
populates `track_features` — the storage layer and the similarity math are inert
until this has run.

Why it's a separate, opt-in job (not a scanner phase)
-----------------------------------------------------
DSP analysis is ~1–5 s/track — orders of magnitude slower than the tag reads the
file scanner does. Running it inside every scan would turn a routine rescan into
an hours-long job. So it's an explicit background pass, triggered by the admin
`/api/analyze` endpoint, with its own progress and cancel — mirroring the file
scanner's lifecycle.

Incremental by default
----------------------
`force=False` only analyses tracks with no current-version feature row
(`queries.get_tracks_needing_features`). `force=True` re-analyses everything —
needed after a feature-layout change (bump `FEATURE_VERSION`).

Threading
---------
Extraction (CPU/IO heavy, no DB) runs on a worker pool; DB writes happen on the
driver thread in batches inside a single `transaction()` each — the same
"parallel parse, serial write" shape the scanner uses, because SQLite is a
single writer.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Tuple

from backend.config import get_settings
from backend.db import (
    transaction,
    queries,
    init_thread_connection,
    _SCANNER_CACHE_PAGES,
)
from backend.core.similarity import extract_features, FEATURE_VERSION

log = logging.getLogger(__name__)

# How many vectors to accumulate before flushing them in one transaction.
_WRITE_BATCH = 50


@dataclass
class AnalyzeProgress:
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    total: int = 0          # tracks selected for this run
    analyzed: int = 0       # successfully extracted + stored
    failed: int = 0         # extract_features returned None (bad/undecodable)
    current: Optional[str] = None  # path currently being processed


_progress = AnalyzeProgress()
_lock = threading.Lock()
_cancel_event = threading.Event()


def get_analyze_progress() -> AnalyzeProgress:
    return _progress


def cancel_analyze() -> bool:
    """Request cancellation of a running pass. Returns False if none is running."""
    if _progress.running:
        _cancel_event.set()
        return True
    return False


def start_analyze_async(force: bool = False) -> bool:
    """Kick off an analysis pass in a background thread. Returns False if one is
    already running (the lock is held)."""
    if not _lock.acquire(blocking=False):
        return False
    _cancel_event.clear()
    t = threading.Thread(
        target=_run, args=(force,), name="muse-analyzer", daemon=True
    )
    t.start()
    return True


def analyze_all_blocking(force: bool = False) -> AnalyzeProgress:
    """Synchronous version (CLI / tests). See module docstring for `force`."""
    with _lock:
        _run_inner(force)
    return _progress


def _run(force: bool) -> None:
    """Background entry point: give this thread its own (larger-cache) DB
    connection, run the pass, always release the lock."""
    init_thread_connection(_SCANNER_CACHE_PAGES)
    try:
        _run_inner(force)
    finally:
        _cancel_event.clear()
        _lock.release()


def _run_inner(force: bool) -> None:
    global _progress
    _progress = AnalyzeProgress(running=True, started_at=time.time())
    try:
        targets: List[Tuple[int, str]] = (
            queries.get_all_track_paths()
            if force
            else queries.get_tracks_needing_features(FEATURE_VERSION)
        )
        _progress.total = len(targets)
        if not targets:
            return

        settings = get_settings()
        batch: List[Tuple[int, list]] = []

        # Extraction is pure (no DB), so it parallelises cleanly. Results come
        # back to this driver thread, which is the only one that writes.
        with ThreadPoolExecutor(max_workers=settings.scanner_workers) as pool:
            futures = {
                pool.submit(extract_features, path): (tid, path)
                for tid, path in targets
            }
            for fut in as_completed(futures):
                if _cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                tid, path = futures[fut]
                _progress.current = path
                vec = fut.result()
                if vec is None:
                    _progress.failed += 1
                    continue
                batch.append((tid, vec))
                if len(batch) >= _WRITE_BATCH:
                    _flush(batch)
                    batch = []

        if batch and not _cancel_event.is_set():
            _flush(batch)
    except Exception:
        log.exception("sonic analysis pass failed")
    finally:
        _progress.running = False
        _progress.current = None
        _progress.finished_at = time.time()
        log.info(
            "sonic analysis complete: analyzed=%d failed=%d of %d",
            _progress.analyzed,
            _progress.failed,
            _progress.total,
        )


def _flush(batch: List[Tuple[int, list]]) -> None:
    """Persist a batch of (track_id, vector) in one transaction."""
    with transaction():
        for tid, vec in batch:
            queries.upsert_track_features(tid, vec, FEATURE_VERSION)
    _progress.analyzed += len(batch)
