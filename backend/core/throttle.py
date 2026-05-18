"""
Failed-auth throttle for the Subsonic endpoint.

Why a custom throttle rather than slowapi:
    slowapi's `@limiter.limit` decorator works on route functions, but the
    auth check for `/rest/*` happens inside the `subsonic_context`
    dependency. We need to throttle BEFORE bcrypt runs (otherwise a brute-
    forcer can keep the CPU busy even if every attempt fails), and we want
    to count failures per (IP, username) so a single attacker IP grinding
    one username doesn't lock out other clients on the same NAT.

Scheme:
    Sliding window per (ip, username). After `MAX_FAILURES` failures
    inside `WINDOW_SECONDS`, further attempts are rejected with the
    same Subsonic error code as a wrong password — the attacker can't
    distinguish "throttled" from "wrong password", which keeps the
    enumeration surface flat.

    Successful authentications drop the window for that key. Subsonic
    clients re-auth on every request, so a single successful hit clears
    a stale failure counter immediately.

Memory:
    Bounded by `_MAX_KEYS`. The cleanup runs lazily on insert: when the
    table exceeds the cap, entries older than the window are dropped
    first; if that doesn't reclaim enough space the oldest entries are
    evicted regardless. Worst-case memory ~64 KiB per key * 4096 = 256 MiB
    of fake (ip, user) pairs an attacker can force, which is fine.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

# Tunables. Defaults assume an interactive client (Symfonium, Feishin, etc.)
# might try a wrong password 2–3 times before the user types the right one.
# 10 failures per minute leaves room for that without offering brute force any
# meaningful budget — at 10/min the search space for a 10-character random
# password is ~280 years.
MAX_FAILURES = 10
WINDOW_SECONDS = 60.0
_MAX_KEYS = 4096

_failures: Dict[Tuple[str, str], Deque[float]] = {}
_lock = threading.Lock()


def is_blocked(ip: str, username: str) -> bool:
    """Return True if (ip, username) has exhausted its failure budget."""
    now = time.monotonic()
    key = (ip, username)
    with _lock:
        dq = _failures.get(key)
        if dq is None:
            return False
        _trim(dq, now)
        return len(dq) >= MAX_FAILURES


def record_failure(ip: str, username: str) -> None:
    """Append a failure timestamp for (ip, username)."""
    now = time.monotonic()
    key = (ip, username)
    with _lock:
        dq = _failures.get(key)
        if dq is None:
            if len(_failures) >= _MAX_KEYS:
                _evict(now)
            dq = deque()
            _failures[key] = dq
        dq.append(now)
        _trim(dq, now)


def record_success(ip: str, username: str) -> None:
    """Clear the failure counter for (ip, username)."""
    key = (ip, username)
    with _lock:
        _failures.pop(key, None)


def _trim(dq: Deque[float], now: float) -> None:
    """Drop entries older than the window."""
    cutoff = now - WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()


def _evict(now: float) -> None:
    """Reclaim space by dropping expired keys, then oldest if still full."""
    cutoff = now - WINDOW_SECONDS
    stale = [k for k, dq in _failures.items() if not dq or dq[-1] < cutoff]
    for k in stale:
        _failures.pop(k, None)
    if len(_failures) < _MAX_KEYS:
        return
    # Still over cap: drop the half with the oldest most-recent failure.
    by_recency = sorted(_failures.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
    for k, _ in by_recency[: _MAX_KEYS // 2]:
        _failures.pop(k, None)


def reset_for_tests() -> None:
    """Wipe state. Tests only."""
    with _lock:
        _failures.clear()
