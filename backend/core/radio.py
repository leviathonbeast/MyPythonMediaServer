"""
Endless-queue continuation ("autoplay radio").

Given the tracks a listener has played recently, produce fresh tracks to keep
the queue going once it would otherwise run dry. This is the server half of the
player's opt-in endless mode: the client sends the recently-played track ids
(seeds) plus everything already in the queue (to exclude), and gets back a
short list of sonically similar tracks to append.

It's a thin orchestration layer over the existing pieces:

  * similarity.find_similar — nearest neighbours of one track in feature space.
  * library.logical_song_key — collapses duplicate recordings (a single + its
    album cut, a remaster, dupe rips) so the radio never offers the same song
    twice, the same dedup used by "song radio".

Seeding from *several* recent tracks (not just the last one) and interleaving
their neighbours round-robin keeps the continuation anchored to the recent
listening session rather than drifting off a single track.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from backend.db import queries
from backend.core import library, similarity

# Per seed, how many neighbours to pull before filtering. A small multiple of
# the requested count so that after removing already-queued tracks and
# duplicates there's still enough left to fill `count`.
_NEIGHBOURS_PER_SEED_FACTOR = 3


def continue_from(
    seed_ids: List[int],
    exclude_ids: List[int],
    count: int,
) -> List[Dict]:
    """Return up to `count` track rows to extend the queue.

    Args:
        seed_ids: recently-played track ids, most-recent-first preferred. Their
            sonic neighbours are the candidate pool.
        exclude_ids: track ids already in the queue / session — never returned,
            and their songs are barred so a different copy isn't suggested.
        count: maximum number of tracks to return.

    Returns track-row dicts (as from queries.get_track), de-duplicated by
    logical song. Empty when nothing has feature vectors yet (library not
    analysed) or no seed resolves.
    """
    if count <= 0 or not seed_ids:
        return []
    features = queries.get_all_track_features()
    if not features:
        return []

    exclude = set(exclude_ids)

    # One cache of track rows shared by the key function and the final hydrate,
    # so we never fetch the same id twice across this request.
    cache: Dict[int, Optional[dict]] = {}

    def track(track_id: int) -> Optional[dict]:
        if track_id not in cache:
            cache[track_id] = queries.get_track(track_id)
        return cache[track_id]

    def key_for(track_id: int):
        return library.logical_song_key(track(track_id))

    # Bar the logical songs already in the queue/session up front, so the radio
    # never returns "a different file of something you just heard".
    seen_keys = set()
    for tid in list(exclude) + list(seed_ids):
        k = key_for(tid)
        if k is not None:
            seen_keys.add(k)

    # Per-seed neighbour lists (each already de-duplicated and excluding the
    # seed's own copies via find_similar's key_for).
    per_seed = max(1, count * _NEIGHBOURS_PER_SEED_FACTOR)
    pools: List[List[int]] = []
    for sid in seed_ids:
        scored = similarity.find_similar(features, sid, per_seed, key_for=key_for)
        pools.append([tid for tid, _ in scored])

    # Round-robin merge: take the best remaining candidate from each seed in
    # turn. This blends the recent seeds instead of exhausting one seed's
    # neighbours before moving to the next.
    chosen: List[int] = []
    chosen_set: set[int] = set()
    while len(chosen) < count:
        progressed = False
        for pool in pools:
            picked = None
            while pool:
                tid = pool.pop(0)
                if tid in exclude or tid in chosen_set:
                    continue
                k = key_for(tid)
                if k is not None and k in seen_keys:
                    continue
                picked = tid
                break
            if picked is not None:
                chosen.append(picked)
                chosen_set.add(picked)
                k = key_for(picked)
                if k is not None:
                    seen_keys.add(k)
                progressed = True
                if len(chosen) >= count:
                    break
        if not progressed:
            break  # every pool exhausted

    return [row for tid in chosen if (row := track(tid)) is not None]
