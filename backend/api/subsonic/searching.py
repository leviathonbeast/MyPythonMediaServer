from __future__ import annotations

from fastapi import Depends, Query, Response

from backend.core import search

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)

# ---- search3 --------------------------------------------------------------


@_double_register("search3")
def do_search3(
    query: str = Query(...),
    artistCount: int = Query(default=20, ge=0, le=500),
    albumCount: int = Query(default=20, ge=0, le=500),
    songCount: int = Query(default=20, ge=0, le=500),
    artistOffset: int = Query(default=0, ge=0),
    albumOffset: int = Query(default=0, ge=0),
    songOffset: int = Query(default=0, ge=0),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    result = search.search3(
        query, artistCount, albumCount, songCount, artistOffset, albumOffset, songOffset
    )
    return responses.ok({"searchResult3": result}, fmt=ctx.fmt, callback=ctx.callback)


# ---- search2 --------------------------------------------------------------


@_double_register("search2")
def do_search2(
    query: str = Query(...),
    artistCount: int = Query(default=20, ge=0, le=500),
    albumCount: int = Query(default=20, ge=0, le=500),
    songCount: int = Query(default=20, ge=0, le=500),
    artistOffset: int = Query(default=0, ge=0),
    albumOffset: int = Query(default=0, ge=0),
    songOffset: int = Query(default=0, ge=0),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    result = search.search3(
        query, artistCount, albumCount, songCount, artistOffset, albumOffset, songOffset
    )
    return responses.ok({"searchResult2": result}, fmt=ctx.fmt, callback=ctx.callback)
