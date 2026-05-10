"""
Library scanner.

Public entry point: `scan_all()` rescans every configured music folder.

Design for 100k–500k tracks
---------------------------
1. Walk the filesystem; for each candidate audio file, check (mtime, size)
   against the existing DB row. If unchanged, skip — no tag parse, no hash,
   no DB write. This is THE optimisation; without it, rescans take hours.

2. Process new/changed files in a thread pool. Tag reading is mostly I/O
   (read file header, pull tags) so threads + GIL is fine.

3. Batch DB writes inside an explicit transaction. SQLite's per-statement
   transaction overhead is significant; one big transaction per N inserts
   is 10x+ faster.

4. After the scan, sweep tracks whose paths no longer exist on disk, then
   recompute denormalised aggregates (album.track_count etc).

Locking
-------
A module-level threading.Lock prevents two scans from running concurrently.
The API exposes start_scan() which returns immediately if a scan is already
running.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import get_settings
from backend.db import get_conn, transaction, queries
from . import artwork, metadata
from .walker import walk_audio_files

log = logging.getLogger(__name__)


@dataclass
class ScanProgress:
    """Snapshot of a scan in progress (or last completed). Exposed via API."""
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    folders_total: int = 0
    folders_done: int = 0
    files_seen: int = 0
    # Set after phase 1 completes for the current folder; lets the UI show
    # a real percentage during the slow parse + write stage. We don't sum
    # across all folders up-front because each folder is walked fresh, so
    # we'd have to walk twice to know the grand total before starting.
    files_to_parse: int = 0
    # Incremented as each parsed record returns from the worker pool. This
    # is the counter that "moves" during the long phase 2.
    files_parsed: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    errors: int = 0
    current_folder: Optional[str] = None
    error_messages: List[str] = field(default_factory=list)


_progress = ScanProgress()
_lock = threading.Lock()


def get_progress() -> ScanProgress:
    """Read-only snapshot of scan progress."""
    return _progress


def start_scan_async() -> bool:
    """
    Kick off a scan in a background thread. Returns False if one is already running.

    Used by the API endpoint that triggers a manual rescan.
    """
    if not _lock.acquire(blocking=False):
        return False
    t = threading.Thread(target=_run_scan, name="muse-scanner", daemon=True)
    t.start()
    return True


def scan_all_blocking() -> ScanProgress:
    """Synchronous version (used by --scan CLI flag and tests)."""
    with _lock:
        _run_scan_inner()
    return _progress


def _run_scan() -> None:
    """Internal: drive the scan and release the lock when done."""
    try:
        _run_scan_inner()
    finally:
        _lock.release()


def _run_scan_inner() -> None:
    """The actual scan. Called with _lock held."""
    settings = get_settings()
    global _progress
    _progress = ScanProgress(running=True, started_at=time.time())

    folders = queries.list_music_folders()
    _progress.folders_total = len(folders)

    extensions = set(e.lower() for e in settings.audio_extensions)

    for folder in folders:
        _progress.current_folder = folder["path"]
        try:
            _scan_folder(folder, extensions)
        except Exception as e:
            log.exception("scan failed for folder %s", folder["path"])
            _progress.errors += 1
            _progress.error_messages.append(f"{folder['path']}: {e}")
        _progress.folders_done += 1

    _progress.current_folder = None

    # Routine GC: prune dangling references and orphan artwork files now
    # that the scan is settled. Cheap; finishes in well under a second.
    # We deliberately don't VACUUM here — that's expensive and only worth
    # it occasionally; users can trigger it from /api/maintenance/gc.
    try:
        from backend.db.maintenance import run_gc
        run_gc(vacuum=False)
    except Exception:
        # GC failures must never poison a successful scan.
        log.exception("post-scan GC failed (non-fatal)")

    _progress.running = False
    _progress.finished_at = time.time()
    log.info(
        "scan complete: +%d ~%d -%d skipped=%d errors=%d",
        _progress.files_added, _progress.files_updated,
        _progress.files_removed, _progress.files_skipped, _progress.errors,
    )


def _scan_folder(folder: Dict[str, Any], extensions: set) -> None:
    """Scan a single music folder."""
    settings = get_settings()
    folder_id = folder["id"]
    root = folder["path"]

    # Pre-flight existence check. We log clearly because "I configured
    # /mnt/music but nothing happened" is a common confusion: if the
    # mount isn't there at scan time the walker would yield nothing
    # silently. This makes the failure mode legible.
    if not os.path.isdir(root):
        log.warning(
            "scan: music folder %r is not a readable directory — "
            "is the mount actually present? (id=%s, name=%r)",
            root, folder_id, folder["name"],
        )
        _progress.errors += 1
        return

    log.info("scan: walking folder id=%s name=%r at %s", folder_id, folder["name"], root)

    # Existing tracks in this folder, keyed by path. We pop from this dict as
    # we see files; whatever's left at the end has been deleted from disk.
    existing = queries.get_existing_paths_for_folder(folder_id)
    log.debug("scan: %d tracks in DB for folder %s", len(existing), root)

    # Files needing fresh metadata extraction. (path, stat) tuples.
    to_parse: List[tuple] = []

    # ---- Phase 1: walk + diff against DB --------------------------------
    # This phase is sequential — scandir is fast, no benefit in parallelising
    # and the DB is single-writer anyway.
    for path, st in walk_audio_files(root, extensions):
        _progress.files_seen += 1
        prior = existing.pop(path, None)
        if prior is not None:
            _, prior_mtime, prior_size = prior
            if int(st.st_mtime) == prior_mtime and st.st_size == prior_size:
                _progress.files_skipped += 1
                continue
            # Changed — re-parse.
            to_parse.append((path, st, "update"))
        else:
            to_parse.append((path, st, "new"))

    log.info(
        "scan: folder %s — %d files seen, %d to parse, %d to remove",
        root, _progress.files_seen, len(to_parse), len(existing),
    )

    # Anything still in `existing` no longer exists on disk. Drop it now,
    # before we start the slow phase 2, so deletions are reflected in the
    # UI immediately.
    stale_ids = [tid for (tid, _, _) in existing.values()]
    if stale_ids:
        with transaction():
            queries.delete_tracks(stale_ids)
        _progress.files_removed += len(stale_ids)

    # Surface the count for the UI so it can show a real percentage during
    # phase 2. Reset files_parsed each folder — we only show progress for
    # the current folder.
    _progress.files_to_parse = len(to_parse)
    _progress.files_parsed = 0

    if not to_parse:
        return

    # ---- Phase 2 + 3: parse in parallel, commit in batches as we go ----
    #
    # Why streaming instead of "parse all, then write all":
    #   * Memory: 52k+ track records in a single list before any DB write
    #     adds up; streaming caps it at one batch.
    #   * Crash safety: a kill -9 half-way through a 30-minute scan now
    #     loses one batch (~200 tracks), not the whole thing.
    #   * Visible progress: the UI sees `files_parsed` advance in real
    #     time as workers finish, and `files_added/updated` advance as
    #     batches commit.
    #
    # SQLite is single-writer, so DB writes still happen sequentially
    # on the main thread inside `transaction()` — the parallelism is
    # purely on the metadata-extraction side, which is the slow part.

    touched_albums: set = set()
    touched_artists: set = set()
    batch_size = settings.scanner_batch_size
    pending: List[Dict[str, Any]] = []

    def _commit(batch: List[Dict[str, Any]]) -> None:
        """Write one batch of parsed records to SQLite in a single transaction."""
        if not batch:
            return
        with transaction():
            for rec in batch:
                # Resolve artist / album ids. The scanner uses album_artist
                # for the album, falling back to artist. A track with
                # artist="Featured Guest" on an album with album_artist=
                # "Main Artist" lives under "Main Artist" — correct behaviour.
                track_artist_name = rec["_artist"]
                album_artist_name = rec["_album_artist"] or track_artist_name

                track_artist_id = queries.upsert_artist(track_artist_name)
                album_artist_id = (
                    track_artist_id
                    if album_artist_name == track_artist_name
                    else queries.upsert_artist(album_artist_name)
                )
                album_id = queries.upsert_album(
                    artist_id=album_artist_id,
                    name=rec["_album"],
                    year=rec.get("year"),
                    genre=rec.get("genre"),
                    release_type=rec.get("_release_type"),
                )

                track_row = {
                    "album_id":        album_id,
                    "artist_id":       track_artist_id,
                    "music_folder_id": folder_id,
                    "path":            rec["path"],
                    "title":           rec["title"],
                    "track_number":    rec.get("track_number"),
                    "disc_number":     rec.get("disc_number"),
                    "duration":        rec.get("duration"),
                    "bitrate":         rec.get("bitrate"),
                    "size":            rec["size"],
                    "suffix":          rec["suffix"],
                    "content_type":    rec["content_type"],
                    "year":            rec.get("year"),
                    "genre":           rec.get("genre"),
                    "mtime":           rec["mtime"],
                    "content_hash":    None,  # populated by a future enrichment pass
                    "last_scanned":    int(time.time()),
                }
                queries.upsert_track(track_row)

                # Cover art: store once per album (first track wins).
                if rec.get("art_data") is not None:
                    cover_id = artwork.store_artwork(rec["art_data"])
                    if cover_id:
                        queries.set_album_cover_art(album_id, cover_id)

                touched_albums.add(album_id)
                touched_artists.add(album_artist_id)

                if rec["_kind"] == "new":
                    _progress.files_added += 1
                else:
                    _progress.files_updated += 1

    with ThreadPoolExecutor(max_workers=settings.scanner_workers) as pool:
        future_to_item = {
            pool.submit(_parse_one, path, st, folder_id): (path, st, kind)
            for (path, st, kind) in to_parse
        }
        for fut in as_completed(future_to_item):
            path, st, kind = future_to_item[fut]
            try:
                rec = fut.result()
                rec["_kind"] = kind
                pending.append(rec)
            except Exception as e:
                _progress.errors += 1
                _progress.error_messages.append(f"{path}: {e}")

            _progress.files_parsed += 1

            # Commit as soon as we have a full batch. This is the reason
            # progress feels snappy: writes start happening seconds into
            # phase 2, not after every file has been parsed.
            if len(pending) >= batch_size:
                _commit(pending)
                pending = []

    # Drain any partial batch left over.
    if pending:
        _commit(pending)

    # ---- Phase 4: recompute aggregates ---------------------------------
    with transaction():
        for aid in touched_albums:
            queries.update_album_aggregates(aid)
        for aid in touched_artists:
            queries.update_artist_aggregates(aid)
        # Tidy up albums/artists that lost their last track. We run this
        # unconditionally — albums can become empty via retagging without
        # any track being deleted from disk (e.g. fixing the album name
        # on every track moves them to a new album row), so gating on
        # stale_ids would miss real empties.
        queries.cleanup_empty_albums_and_artists()


def _parse_one(path: str, st, folder_id: int) -> Dict[str, Any]:
    """
    Extract metadata for a single file. Runs in a worker thread.

    Returns a dict with the fields the scanner needs. Underscored keys (_artist,
    _album, _album_artist) carry the raw names; the main thread resolves them
    to ids.
    """
    meta = metadata.extract(path)

    # If no embedded art, look for cover.jpg etc next to the file.
    art_data = meta.art_data
    if art_data is None:
        art_data = artwork.find_folder_artwork(path)

    suffix = Path(path).suffix.lstrip(".").lower()
    return {
        "path":         path,
        "title":        meta.title,
        "_artist":      meta.artist,
        "_album_artist": meta.album_artist,
        "_album":       meta.album,
        "_release_type": meta.release_type,
        "track_number": meta.track_number,
        "disc_number":  meta.disc_number,
        "duration":     meta.duration,
        "bitrate":      meta.bitrate,
        "size":         st.st_size,
        "mtime":        int(st.st_mtime),
        "year":         meta.year,
        "genre":        meta.genre,
        "suffix":       suffix,
        "content_type": metadata.get_content_type(suffix),
        "art_data":     art_data,
    }
