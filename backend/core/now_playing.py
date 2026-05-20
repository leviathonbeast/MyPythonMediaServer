"""
Now Playing Service
"""

from __future__ import annotations


import threading
from typing import Dict, List
import time
from dataclasses import dataclass

from backend.db import queries


@dataclass
class NowPlayingEntry:
    user_id: int
    track_id: int
    client: str  # the Subsonic c= param (e.g. "muse-web")
    started_at: int  # epoch seconds, first time this track was seen
    last_seen: int  # epoch seconds, most recent ping


_entries: Dict[int, NowPlayingEntry] = {}
_lock = threading.Lock()


def record(user_id, track_id, client) -> None:
    now = int(time.time())

    with _lock:
        existing_entry = _entries.get(user_id)

        if existing_entry is not None and existing_entry.track_id == track_id:
            existing_entry.last_seen = now
        else:
            _entries[user_id] = NowPlayingEntry(  # new track or first ping
                user_id=user_id,
                track_id=track_id,
                client=client,
                started_at=now,
                last_seen=now,
            )


def list_active(within_seconds: int = 300) -> List[NowPlayingEntry]:

    with _lock:
        cutoff = time.time() - within_seconds

        # Collect stale entries first
        stale_keys = [
            user_id for user_id, entry in _entries.items() if entry.last_seen < cutoff
        ]

        # Remove stale entries in place
        for user_id in stale_keys:
            del _entries[user_id]

        return list(_entries.values())


def clear_for_tests() -> None:
    with _lock:
        _entries.clear()


# Alice plays track 100 → record(1, 100, "muse-web")
# _entries.get(1) → None → else branch → entry created ✓
# Alice's SPA pings again 5s later (same track) → record(1, 100, "muse-web")
# _entries.get(1) → the prior entry → track_id == 100 → bump only last_seen ✓
# Alice clicks next, track 200 → record(1, 200, "muse-web")
# _entries.get(1) → prior entry → track_id (100) == 200 → False → else branch → entry replaced ✓
# 10 minutes pass with no activity, server calls list_active(300)
# cutoff = now - 300, alice's last_seen < cutoff → pruned → empty list ✓
# If your code produces those outcomes, the module is correct.
