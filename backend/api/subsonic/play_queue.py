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


@_double_register("savePlayQueueByIndex")
def save_play_queue_by_index(
    id: list[str] = Query(default=[]),
    currentIndex: Optional[int] = Query(default=None, ge=0),
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

    if track_ids and currentIndex is None:
        return responses.error(
            responses.ERR_PARAMETER,
            "currentIndex is required when song id is provided",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    if currentIndex is not None and currentIndex >= len(track_ids):
        return responses.error(
            responses.ERR_PARAMETER,
            "currentIndex out of range",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    # explicitly valid
    current_index = currentIndex

    # Some clients (e.g. Airdrome) misuse the Subsonic `c=` param and send
    # the server URL instead of their own name. Fall back to "unknown" so
    # changedBy stays meaningful in the response.
    client = ctx.client
    if client.startswith(("http://", "https://")):
        client = "unknown"

    with transaction():
        queries.save_play_queue_by_index(
            user_id=ctx.user_id,
            track_ids=track_ids,
            current_index=current_index,
            position_ms=position or 0,
            client=client,
        )

    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getPlayQueueByIndex")
def get_play_queue_by_index(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = queries.get_play_queue(ctx.user_id)

    if payload is None:
        return responses.ok(fmt=ctx.fmt, callback=ctx.callback)

    # Resolve currentIndex:
    #   1. Prefer the value stored by savePlayQueueByIndex (current_position).
    #   2. Fall back to locating current_id within the queue, for queues
    #      saved by the legacy savePlayQueue path (which doesn't populate
    #      current_position). This fallback picks the FIRST occurrence and
    #      is lossy on duplicates — but legacy clients couldn't
    #      disambiguate them either, so the answer matches their model.
    current_index = payload.get("current_position")
    if current_index is None and payload.get("current_id") is not None:
        for i, t in enumerate(payload["tracks"]):
            if t["id"] == payload["current_id"]:
                current_index = i
                break

    play_queue_by_index = {
        "currentIndex": current_index if current_index is not None else 0,
        "position": payload["position_ms"],
        "username": payload["owner"],
        "changed": datetime.fromtimestamp(
            payload["changed_at"], tz=timezone.utc
        ).isoformat(),
        "changedBy": payload["changed_by"],
        "entry": [library.track_to_subsonic(t) for t in payload["tracks"]],
    }
    return responses.ok(
        {"playQueueByIndex": play_queue_by_index},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
