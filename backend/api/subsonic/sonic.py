"""
OpenSubsonic "sonicSimilarity" extension — getSonicSimilarTracks & findSonicPath.

Both endpoints answer "given the sonic fingerprint of a track, which other tracks
are near it?" — getSonicSimilarTracks returns the nearest neighbours; findSonicPath
returns an ordered walk from one track to another through feature space.

The actual maths lives in backend/core/similarity.py (pure functions over vectors);
the feature vectors are produced by the analysis pass (backend/core/sonic_analysis.py)
and stored in track_features. This module just adapts ids ↔ track rows and builds the
response envelope.

Response shape note: unlike most endpoints, `sonicMatch` is a flat *array* of
{entry, similarity} objects — not the usual {key: {entry: [...]}} wrapper.
"""

from __future__ import annotations

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


def _sonic_match(scored: list[tuple[int, float]]) -> list[dict]:
    """Turn [(track_id, similarity)] into the spec's sonicMatch array, hydrating
    each id into a full song object. Tracks that vanished since analysis are
    skipped rather than emitted as broken entries."""
    out: list[dict] = []
    for track_id, sim in scored:
        track = queries.get_track(track_id)
        if track is None:
            continue
        out.append(
            {
                "entry": library.track_to_subsonic(track),
                "similarity": round(float(sim), 4),
            }
        )
    return out


@_double_register("getSonicSimilarTracks")
def get_sonic_similar_tracks(
    id: str = Query(...),
    count: int = Query(default=10, ge=1, le=500),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Tracks sonically similar to `id`, most-similar first. Empty sonicMatch
    when the track has no feature vector yet (analysis not run)."""
    kind, rid = library.parse_id(id)
    if kind != "track" or rid is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Not a track id",
            fmt=ctx.fmt, callback=ctx.callback,
        )

    scored = similarity.find_similar(queries.get_all_track_features(), rid, count)
    return responses.ok(
        {"sonicMatch": _sonic_match(scored)}, fmt=ctx.fmt, callback=ctx.callback
    )


@_double_register("findSonicPath")
def find_sonic_path(
    startSongId: str = Query(...),
    endSongId: str = Query(...),
    count: int = Query(default=25, ge=2, le=500),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """An ordered path of tracks from `startSongId` to `endSongId` through sonic
    feature space. Empty sonicMatch when either endpoint lacks a feature vector."""
    _, start_rid = library.parse_id(startSongId)
    _, end_rid = library.parse_id(endSongId)
    if start_rid is None or end_rid is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Not a track id",
            fmt=ctx.fmt, callback=ctx.callback,
        )

    scored = similarity.find_path(
        queries.get_all_track_features(), start_rid, end_rid, count
    )
    return responses.ok(
        {"sonicMatch": _sonic_match(scored)}, fmt=ctx.fmt, callback=ctx.callback
    )
