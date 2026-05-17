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
from backend.core import deezer
from backend.db import get_conn, transaction, queries, init_thread_connection, _SCANNER_CACHE_PAGES
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
_cancel_event = threading.Event()


@dataclass
class RecoverArtworkProgress:
    """
    Snapshot of an artwork-recovery pass.

    Recovery is a targeted rescan: for every album whose cover_art_id is
    NULL we pick one of its tracks and try to re-extract embedded art
    (falling back to folder.jpg / cover.jpg etc). It's much cheaper than a
    full library scan because we only touch one file per album and skip
    all the metadata extraction the main scanner does.
    """
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Phase A — album cover art
    albums_total: int = 0
    albums_done: int = 0
    artwork_recovered: int = 0
    # Subset of `artwork_recovered` that came from the Deezer fallback
    # rather than embedded tags or a folder image. Useful for spotting
    # libraries where most of the recovery is driven by the network path.
    recovered_via_deezer: int = 0
    # Phase B — artist photos (always Deezer-sourced today)
    artists_total: int = 0
    artists_done: int = 0
    artist_images_recovered: int = 0
    # Aggregate across both phases.
    errors: int = 0
    # Coarse-grained phase label so the UI can label the progress bar.
    # One of: "albums" | "artists" | "" (idle/finished).
    phase: str = ""


_recover_progress = RecoverArtworkProgress()
_recover_lock = threading.Lock()
_recover_cancel = threading.Event()


def get_recover_progress() -> RecoverArtworkProgress:
    return _recover_progress


def cancel_recover_artwork() -> bool:
    if not _recover_progress.running:
        return False
    _recover_cancel.set()
    return True


def start_recover_artwork_async() -> bool:
    """Kick off recovery in a background thread. False if one is already running."""
    if not _recover_lock.acquire(blocking=False):
        return False
    _recover_cancel.clear()
    t = threading.Thread(target=_run_recover_artwork, name="muse-art-recover", daemon=True)
    t.start()
    return True


def _run_recover_artwork() -> None:
    init_thread_connection(_SCANNER_CACHE_PAGES)
    try:
        _run_recover_inner()
    finally:
        _recover_cancel.clear()
        _recover_lock.release()


def _run_recover_inner() -> None:
    """
    Walk every album with a NULL cover_art_id, pick one of its tracks,
    extract embedded or folder artwork, and stamp the album row.

    Parallelism follows the same pattern as the main scanner: a thread pool
    does the file-read + decode work; the main thread is the only one
    writing to SQLite.
    """
    global _recover_progress
    _recover_progress = RecoverArtworkProgress(
        running=True, started_at=time.time(), phase="albums",
    )

    settings = get_settings()
    rows = (
        get_conn()
        .execute(
            """SELECT a.id, a.name AS album_name, ar.name AS artist_name,
                      MIN(t.path) AS track_path
                 FROM albums a
                 JOIN tracks t ON t.album_id = a.id
            LEFT JOIN artists ar ON ar.id = a.artist_id
                WHERE a.cover_art_id IS NULL
             GROUP BY a.id
               HAVING track_path IS NOT NULL"""
        )
        .fetchall()
    )
    targets: List[tuple] = [
        (row[0], row[1] or "", row[2] or "", row[3]) for row in rows
    ]
    _recover_progress.albums_total = len(targets)

    if not targets:
        _recover_progress.running = False
        _recover_progress.finished_at = time.time()
        log.info("recover-artwork: nothing to do — all albums have artwork or no tracks")
        return

    def _extract(album_id: int, album_name: str, artist_name: str, path: str) -> Optional[tuple]:
        """Return (album_id, art_bytes, via_deezer) or None when no art was found.

        Source order:
          1. Embedded art in the file's tags (fastest, no network).
          2. cover.jpg / folder.jpg etc next to the audio file.
          3. Deezer cover lookup by (artist, album) — Last.fm dropped
             cover-art from their API so Deezer is our only third-party
             fallback. Slow (network), but it's the only thing that
             helps when the user's library was ripped without tags
             and has no sidecar art.
        """
        try:
            meta = metadata.extract(path)
            art = meta.art_data
            if art is None:
                art = artwork.find_folder_artwork(path)
            if art is not None:
                return (album_id, art, False)
        except Exception as e:
            log.debug("recover-artwork: local extract failed for %s: %s", path, e)
            # Fall through to Deezer — a broken file shouldn't deny us the
            # chance to get art from the network.

        if artist_name and album_name:
            art = deezer.fetch_album_cover_bytes(artist_name, album_name)
            if art is not None:
                return (album_id, art, True)
        return None

    pool = ThreadPoolExecutor(max_workers=settings.scanner_workers)
    pending: List[tuple] = []
    batch_size = max(50, settings.scanner_batch_size)

    def _commit(batch: List[tuple]) -> None:
        if not batch:
            return
        with transaction():
            for album_id, art_bytes, via_deezer in batch:
                cover_id = artwork.store_artwork(art_bytes)
                if cover_id:
                    queries.set_album_cover_art(album_id, cover_id)
                    _recover_progress.artwork_recovered += 1
                    if via_deezer:
                        _recover_progress.recovered_via_deezer += 1

    try:
        futures = [
            pool.submit(_extract, aid, album_name, artist_name, path)
            for aid, album_name, artist_name, path in targets
        ]
        for fut in as_completed(futures):
            if _recover_cancel.is_set():
                for f in futures:
                    f.cancel()
                break
            _recover_progress.albums_done += 1
            try:
                result = fut.result()
            except Exception as e:
                _recover_progress.errors += 1
                log.debug("recover-artwork: worker failed: %s", e)
                continue
            if result is not None:
                pending.append(result)
                if len(pending) >= batch_size:
                    _commit(pending)
                    pending = []
    finally:
        pool.shutdown(wait=not _recover_cancel.is_set())

    if pending:
        _commit(pending)

    # ---- Phase B: artist photos ----------------------------------------
    # Same architecture as album recovery but with a different worker —
    # there's no embedded/folder fallback, just Deezer. We download once
    # per artist and stamp `artists.image_id`; from then on every artist
    # card on the library page is served from our local cache and the
    # client's getCoverArt response gets the same one-year immutable
    # cache headers as album covers.
    if not _recover_cancel.is_set():
        _recover_progress.phase = "artists"
        artist_targets = queries.list_artists_missing_image()
        _recover_progress.artists_total = len(artist_targets)

        def _fetch_artist(artist_id: int, name: str) -> Optional[tuple]:
            try:
                art = deezer.fetch_artist_image_bytes(name)
                return (artist_id, art) if art else None
            except Exception as e:
                log.debug("recover-artwork: artist fetch failed for %r: %s", name, e)
                return None

        def _commit_artists(batch: List[tuple]) -> None:
            if not batch:
                return
            with transaction():
                for artist_id, art_bytes in batch:
                    image_id = artwork.store_artwork(art_bytes)
                    if image_id:
                        queries.set_artist_image(artist_id, image_id)
                        _recover_progress.artist_images_recovered += 1

        if artist_targets:
            pool2 = ThreadPoolExecutor(max_workers=settings.scanner_workers)
            artist_pending: List[tuple] = []
            try:
                futures = [
                    pool2.submit(_fetch_artist, row["id"], row["name"])
                    for row in artist_targets
                ]
                for fut in as_completed(futures):
                    if _recover_cancel.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    _recover_progress.artists_done += 1
                    try:
                        result = fut.result()
                    except Exception as e:
                        _recover_progress.errors += 1
                        log.debug("recover-artwork: artist worker failed: %s", e)
                        continue
                    if result is not None:
                        artist_pending.append(result)
                        if len(artist_pending) >= batch_size:
                            _commit_artists(artist_pending)
                            artist_pending = []
            finally:
                pool2.shutdown(wait=not _recover_cancel.is_set())
            if artist_pending:
                _commit_artists(artist_pending)

    _recover_progress.phase = ""
    _recover_progress.running = False
    _recover_progress.finished_at = time.time()
    log.info(
        "recover-artwork: done — albums=%d/%d (%d via deezer), artists=%d/%d, errors=%d",
        _recover_progress.artwork_recovered,
        _recover_progress.albums_total,
        _recover_progress.recovered_via_deezer,
        _recover_progress.artist_images_recovered,
        _recover_progress.artists_total,
        _recover_progress.errors,
    )


def get_progress() -> ScanProgress:
    """Read-only snapshot of scan progress."""
    return _progress


def cancel_scan() -> bool:
    """Request cancellation of a running scan. Returns True if one was running."""
    if not _progress.running:
        return False
    _cancel_event.set()
    return True


def start_scan_async() -> bool:
    """
    Kick off a scan in a background thread. Returns False if one is already running.

    Used by the API endpoint that triggers a manual rescan.
    """
    if not _lock.acquire(blocking=False):
        return False
    _cancel_event.clear()
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
    # Give this thread a larger SQLite page cache before any DB access.
    # API threads share the smaller default; the scanner gets more because
    # it reads every path, artist, and album row in the library.
    init_thread_connection(_SCANNER_CACHE_PAGES)
    try:
        _run_scan_inner()
    finally:
        _cancel_event.clear()
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
        if _cancel_event.is_set():
            log.info("scan: cancelled before folder %s", folder["path"])
            break
        _progress.current_folder = folder["path"]
        try:
            _scan_folder(folder, extensions)
        except Exception as e:
            log.exception("scan failed for folder %s", folder["path"])
            _progress.errors += 1
            _progress.error_messages.append(f"{folder['path']}: {e}")
        _progress.folders_done += 1

    _progress.current_folder = None

    if _cancel_event.is_set():
        log.info("scan: cancelled")
    else:
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
    touched_albums: set = set()
    for path, st in walk_audio_files(root, extensions):
        _progress.files_seen += 1
        prior = existing.pop(path, None)
        if prior is not None:
            _, prior_mtime, prior_size, prior_album_id = prior
            if int(st.st_mtime) == prior_mtime and st.st_size == prior_size:
                _progress.files_skipped += 1
                touched_albums.add(prior_album_id) # even skipped files contribute to album aggregates
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
    stale_ids = [tid for (tid, _, _, _) in existing.values()]
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

                track_artist_id = queries.upsert_artist(
                    track_artist_name,
                    musicbrainz_id=rec.get("_mb_artist_id"),
                )
                if album_artist_name == track_artist_name:
                    album_artist_id = track_artist_id
                else:
                    album_artist_id = queries.upsert_artist(
                        album_artist_name,
                        musicbrainz_id=rec.get("_mb_albumartist_id"),
                    )
                album_id = queries.upsert_album(
                    artist_id=album_artist_id,
                    name=rec["_album"],
                    year=rec.get("year"),
                    genre=rec.get("genre"),
                    release_type=rec.get("_release_type"),
                    musicbrainz_id=rec.get("_mb_album_id"),
                    musicbrainz_releasegroup_id=rec.get("_mb_releasegroup_id"),
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
                    "musicbrainz_id":  rec.get("_mb_track_id"),
                }
                queries.upsert_track(track_row)

                # Cover art: the worker thread already hashed + wrote the
                # bytes to disk (see _parse_one), so we only stamp the
                # album row here. First track wins; later tracks for the
                # same album overwrite the cover_art_id harmlessly because
                # the hash is content-derived and identical art produces
                # the same hash.
                art_id = rec.pop("art_id", None)
                if art_id:
                    queries.set_album_cover_art(album_id, art_id)

                touched_albums.add(album_id)
                touched_artists.add(album_artist_id)

                if rec["_kind"] == "new":
                    _progress.files_added += 1
                else:
                    _progress.files_updated += 1

    pool = ThreadPoolExecutor(max_workers=settings.scanner_workers)
    try:
        future_to_item = {
            pool.submit(_parse_one, path, st, folder_id): (path, st, kind)
            for (path, st, kind) in to_parse
        }
        for fut in as_completed(future_to_item):
            if _cancel_event.is_set():
                for f in future_to_item:
                    f.cancel()
                break
            path, st, kind = future_to_item.pop(fut)
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
            # Wrapped in try/except so a transient commit failure (DB
            # locked, weird metadata, etc.) doesn't kill the whole scan —
            # we log it, count it as a batch-worth of errors, and keep
            # the loop alive so the remaining 45k+ files still get a
            # chance to be processed.
            if len(pending) >= batch_size:
                try:
                    _commit(pending)
                except Exception as e:
                    log.exception(
                        "scan: batch commit failed (size=%d); dropping batch and continuing",
                        len(pending),
                    )
                    _progress.errors += 1
                    _progress.error_messages.append(f"commit failed: {e}")
                pending = []
    finally:
        # NEVER wait=True here. Worker threads are doing slow NAS file
        # reads (mutagen) and may take minutes to return on a stalled
        # mount. Waiting deadlocks the scanner thread. wait=False returns
        # immediately; the worker threads keep running in the background
        # but their results are discarded since nobody reads the futures.
        # They die naturally when the process exits or when GC reaps the
        # executor. Trading some wasted CPU for liveness.
        pool.shutdown(wait=False)

    if _cancel_event.is_set():
        return

    # Drain any partial batch left over. Same defensive wrap — a final
    # failure shouldn't lose the per-folder aggregate-recompute that
    # happens further down.
    if pending:
        try:
            _commit(pending)
        except Exception as e:
            log.exception("scan: final drain commit failed (size=%d)", len(pending))
            _progress.errors += 1
            _progress.error_messages.append(f"final commit failed: {e}")

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

    This function is called by ThreadPoolExecutor — potentially dozens of times
    in parallel. Because it only reads files (no DB writes), it's safe to run
    concurrently.

    Returns a dict with the fields the scanner needs. Keys prefixed with _
    (like _artist, _album) carry raw name strings. The main thread is the only
    one that converts those names to database IDs via upsert_artist/upsert_album,
    because SQLite only allows one writer at a time.
    """
    meta = metadata.extract(path)

    # If no embedded art, look for cover.jpg etc next to the file.
    art_data = meta.art_data
    if art_data is None:
        art_data = artwork.find_folder_artwork(path)

    # Hash + write the artwork file HERE in the worker thread, NOT later
    # inside the main thread's transaction. Reason: artwork can be several
    # MB of FLAC-embedded JPEG. Doing the disk write inside the per-batch
    # SQLite transaction keeps the write lock open for as long as the slow
    # filesystem takes, which on NAS-backed setups means seconds — long
    # enough to make every concurrent API write (e.g. login) time out.
    # store_artwork() is idempotent on identical bytes, so the race
    # between workers writing the same hash is harmless.
    art_id: Optional[str] = None
    if art_data is not None:
        try:
            art_id = artwork.store_artwork(art_data)
        except OSError as e:
            log.debug("scan: store_artwork failed for %s: %s", path, e)
        art_data = None  # release reference; bytes can be multi-MB

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
        # Pre-stored on disk by the worker; _commit just stamps the hash.
        "art_id":       art_id,
        # MBIDs — _commit threads these through the relevant upsert helpers.
        # Underscored to distinguish them from columns persisted directly.
        "_mb_track_id":       meta.musicbrainz_track_id,
        "_mb_album_id":       meta.musicbrainz_album_id,
        "_mb_releasegroup_id": meta.musicbrainz_releasegroup_id,
        "_mb_artist_id":      meta.musicbrainz_artist_id,
        "_mb_albumartist_id": meta.musicbrainz_albumartist_id,
    }
