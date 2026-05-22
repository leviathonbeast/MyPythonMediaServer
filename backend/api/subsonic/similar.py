"""
Core Subsonic "similar songs" endpoints — getSimilarSongs & getSimilarSongs2.

Both build an "artist radio": given an id, return a collection of songs sonically
related to it. The Subsonic spec phrases this as "a random collection of songs from
the given artist and similar artists" (canonically Last.fm-driven). Muse has no
Last.fm similar-artists data, but it *does* have per-track DSP fingerprints in
track_features — so we reuse the sonic-similarity engine instead:

  pick a random seed track from the requested entity  →  similarity.find_similar()
  →  [seed, ...nearest neighbours]  (capped at `count`).

The seed is prepended so the queue actually starts with the requested artist (the
"from the given artist" half of the spec); its neighbours supply the "and similar
artists" half. A fresh random seed each call gives the "random collection" variety.

Difference between the two endpoints (identical bodies otherwise):
  - getSimilarSongs   accepts an artist / album / song id (the directory-browsing form).
  - getSimilarSongs2  accepts an artist id only (the ID3 form).
Response key differs to match: "similarSongs" vs "similarSongs2".

Both use the standard nested wrapper {key: {"song": [...]}} — NOT the flat
sonicMatch array of the sonicSimilarity extension (see sonic.py). When the seed has
no sonic neighbours (library not analysed yet, or this seed has no feature vector),
we return an ok envelope with an empty song list, mirroring getSonicSimilarTracks /
getTopSongs rather than erroring.
"""

from __future__ import annotations

import random
from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.core import library, similarity

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


def _build_similar(
    seed_id: Optional[int],
    count: int,
    ctx: SubsonicContext,
    key: str,
) -> Response:
    """Shared body: from a resolved seed track id, build the {key: {song: [...]}}
    response. `seed_id is None` means the id didn't resolve to anything we can
    seed from → ERR_NOT_FOUND."""
    if seed_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )

    # find_similar excludes the seed itself, so ask for count-1 and prepend the
    # seed below. Empty result = no feature vectors (seed unanalysed or whole
    # library unanalysed) → empty song list, not an error.
    scored = similarity.find_similar(
        queries.get_all_track_features(), seed_id, count - 1
    )
    if not scored:
        return responses.ok(
            {key: {"song": []}}, fmt=ctx.fmt, callback=ctx.callback
        )

    seed_row = queries.get_track(seed_id)
    songs = [library.track_to_subsonic(seed_row)] if seed_row else []
    for track_id, _sim in scored:
        row = queries.get_track(track_id)
        if row is None:
            continue  # vanished since analysis — skip rather than emit a broken entry
        songs.append(library.track_to_subsonic(row))

    return responses.ok(
        {key: {"song": songs[:count]}}, fmt=ctx.fmt, callback=ctx.callback
    )


def _random_track_id(rows: list) -> Optional[int]:
    """Pick a random track id from a list of track-row dicts, or None if empty."""
    return random.choice(rows)["id"] if rows else None


@_double_register("getSimilarSongs")
def get_similar_songs(
    id: str = Query(...),
    count: int = Query(default=50, ge=1, le=500),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Songs sonically similar to `id` (an artist, album, or song). For an
    artist/album we seed from a random track within it; for a song the track
    itself is the seed."""
    kind, rid = library.parse_id(id)
    seed_id: Optional[int] = None
    if rid is not None:
        if kind == "track":
            seed_id = rid if queries.get_track(rid) is not None else None
        elif kind == "album":
            seed_id = _random_track_id(queries.list_album_tracks(rid))
        elif kind == "artist":
            seed_id = _random_track_id(queries.list_artist_tracks(rid))
    return _build_similar(seed_id, count, ctx, "similarSongs")


@_double_register("getSimilarSongs2")
def get_similar_songs_2(
    id: str = Query(...),
    count: int = Query(default=50, ge=1, le=500),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """ID3 form: `id` is always an artist. Seeds from a random track in the
    artist's catalogue. parse_artist_id (not parse_id) so a bare integer is
    read as an artist id, not a track id."""
    artist_id = library.parse_artist_id(id)
    seed_id = (
        _random_track_id(queries.list_artist_tracks(artist_id))
        if artist_id is not None
        else None
    )
    return _build_similar(seed_id, count, ctx, "similarSongs2")
