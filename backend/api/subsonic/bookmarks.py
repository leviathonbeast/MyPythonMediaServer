"""
Bookmarks — getBookmarks / createBookmark / deleteBookmark (Subsonic 1.9.0).

A bookmark is a saved playback position (in milliseconds) within a track, per
user — clients use them to resume long tracks (audiobooks, mixes, podcasts).
Storage is the `bookmarks` table (migration 9), one row per (user, track).

These are per-user: every endpoint scopes to ctx.user_id, so users never see or
clobber each other's bookmarks. createBookmark doubles as "update" — saving a
new position for an already-bookmarked track moves it (the query upserts).
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    track_to_subsonic,
)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@_double_register("getBookmarks")
def get_bookmarks(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """All of the calling user's bookmarks, each carrying its full song entry."""
    out: list[dict] = []
    for bm in queries.list_bookmarks(ctx.user_id):
        track = queries.get_track(bm["track_id"])
        if track is None:
            continue  # track deleted since the bookmark was made — skip
        out.append(
            {
                "entry": track_to_subsonic(track),
                "position": bm["position"],
                "username": ctx.username,
                "comment": bm.get("comment") or "",
                "created": _iso(bm["created"]),
                "changed": _iso(bm["changed"]),
            }
        )
    return responses.ok(
        {"bookmarks": {"bookmark": out}}, fmt=ctx.fmt, callback=ctx.callback
    )


@_double_register("createBookmark")
def create_bookmark(
    id: str = Query(...),
    position: int = Query(..., ge=0),
    comment: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Save (or move) a bookmark at `position` ms within track `id`."""
    kind, rid = library.parse_id(id)
    if kind != "track" or rid is None or queries.get_track(rid) is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Track not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    with transaction():
        queries.upsert_bookmark(ctx.user_id, rid, position, comment)
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("deleteBookmark")
def delete_bookmark(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Remove the calling user's bookmark for track `id`."""
    kind, rid = library.parse_id(id)
    if kind != "track" or rid is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Not a track id",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    with transaction():
        queries.delete_bookmark(ctx.user_id, rid)
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
