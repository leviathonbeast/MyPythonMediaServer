"""
Periodic-rescan watcher.

Polls (does not subscribe to filesystem events) because Muse's typical
deployment has music on an NFS/SMB share modified from another machine,
and the Linux kernel does not fire inotify events for remote writes —
watchdog/pyinotify would silently never fire. Polling works regardless
of how files arrive on the share.

The watcher is just a smarter trigger for the existing diff-based scanner:
every `interval` seconds it calls `start_scan_async()`, which is a no-op
when a scan is already running (the scanner module owns a non-blocking
lock). Phase 1's mtime+size short-circuit means a no-change pass over a
50k-file library completes in seconds, so the overhead is negligible.

Lifecycle is owned by main.py's lifespan: `start_watcher()` on startup,
`stop_watcher()` on shutdown. The thread waits on an Event so shutdown
isn't blocked for up to `interval` seconds.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from backend.config import get_settings

from .scanner import start_scan_async

log = logging.getLogger(__name__)

# Don't allow intervals below this regardless of config — protects the
# server from a typo (interval=1) that would spin the scanner constantly.
_MIN_INTERVAL_SECONDS = 30

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def start_watcher() -> bool:
    """Start the background watcher thread.

    Returns True if the thread was started, False if the watcher is disabled
    in settings or already running.
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        log.debug("watcher: already running")
        return False

    settings = get_settings()
    if not settings.scanner_watch_enabled:
        log.debug("watcher: disabled in settings")
        return False

    _stop_event.clear()
    _thread = threading.Thread(
        target=_run_loop,
        name="muse-watcher",
        daemon=True,
    )
    _thread.start()
    log.info(
        "watcher: started (interval=%ds)",
        max(_MIN_INTERVAL_SECONDS, settings.scanner_watch_interval_seconds),
    )
    return True


def stop_watcher(timeout: float = 5.0) -> None:
    """Signal the watcher to stop and wait briefly for the thread to exit.

    Safe to call when the watcher isn't running.
    """
    global _thread
    if _thread is None:
        return
    _stop_event.set()
    _thread.join(timeout=timeout)
    if _thread.is_alive():
        # Daemon thread; will be killed at process exit anyway.
        log.warning("watcher: did not exit within %.1fs", timeout)
    _thread = None


def _run_loop() -> None:
    settings = get_settings()
    interval = max(_MIN_INTERVAL_SECONDS, settings.scanner_watch_interval_seconds)

    # Event.wait returns True if the event was set during the wait, False on
    # timeout. So `if _stop_event.wait(interval): break` gives us a
    # cancellable sleep that wakes immediately on shutdown.
    while True:
        if _stop_event.wait(timeout=interval):
            break

        started = start_scan_async()
        if started:
            log.info("watcher: triggered scan")
        else:
            log.debug("watcher: tick skipped (scan already running)")

    log.info("watcher: stopped")
