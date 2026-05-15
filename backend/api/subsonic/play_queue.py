from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.db.connection import transaction
from backend.core import library

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


@_double_register("getPlayQueue")
def get_play_queue(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    payload = queries.get_play_queue(id)

    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Queue Not Found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok({"playQueue": payload}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("savePlayQueue")
def save_play_queue(
    id: list[str] = Query(default=[]),
    current: Optional[str] = Query(default=None),
    position: Optional[int] = Query(default=0),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    # Convert "tr-123" style ids to internal ints; drop anything we can't parse
    # so a single garbage id doesn't fail the whole save.
    track_ids: list[int] = []
    for raw in id:
        kind, internal = library.parse_id(raw)
        if kind == "track" and internal is not None:
            track_ids.append(internal)

    current_id: Optional[int] = None
    if current:
        kind, internal = library.parse_id(current)
        if kind == "track" and internal is not None:
            current_id = internal

    with transaction():
        queries.save_play_queue(
            user_id=ctx.user_id,
            track_ids=track_ids,
            current_track_id=current_id,
            position_ms=position or 0,
            client=ctx.client,
        )

    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
