from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.db.connection import transaction
from backend.core import library
from datetime import datetime, timezone

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


@_double_register("getPlayQueue")
def get_play_queue(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    payload = queries.get_play_queue(ctx.user_id)

    if payload is None:
        return responses.ok(fmt=ctx.fmt, callback=ctx.callback)

    play_queue = {
        "current": (
            library.make_track_id(payload["current_id"])
            if payload["current_id"] is not None
            else None
        ),
        "position": payload["position_ms"],
        "username": payload["owner"],
        "changed": datetime.fromtimestamp(
            payload["changed_at"], tz=timezone.utc
        ).isoformat(),
        "changedBy": payload["changed_by"],
        "entry": [library.track_to_subsonic(t) for t in payload["tracks"]],
    }
    return responses.ok({"playQueue": play_queue}, fmt=ctx.fmt, callback=ctx.callback)


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

    # Some clients (e.g. Airdrome) misuse the Subsonic `c=` param and send
    # the server URL instead of their own name. Fall back to "unknown" so
    # changedBy stays meaningful in the response.
    client = ctx.client
    if client.startswith(("http://", "https://")):
        client = "unknown"

    with transaction():
        queries.save_play_queue(
            user_id=ctx.user_id,
            track_ids=track_ids,
            current_track_id=current_id,
            position_ms=position or 0,
            client=client,
        )

    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
