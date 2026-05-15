from __future__ import annotations


from fastapi import Depends, Query, Response

from backend.db import queries
from backend.core import library, deezer

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


def _build_album_info(id: str, ctx: SubsonicContext, key: str) -> Response:
    """
    Shared body for getAlbumInfo and getAlbumInfo2.

    The two Subsonic endpoints return identical data; the only difference is
    the response envelope key ("artistInfo" vs "artistInfo2"). `key` selects
    which one to emit so the lookup, bio fetch, and image fetch are written
    only once.
    """

    kind, internal_id = library.parse_id(id)

    if kind != "album" or internal_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Invalid album id",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    album = queries.get_album(internal_id)
    if album is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Album not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    images = deezer.get_album_images(album["artist_name"], album["name"])

    return responses.ok(
        {
            key: {
                "notes": None,
                "musicBrainzId": "",
                "lastFmUrl": "",
                "smallImageUrl": images.get("cover_small") if images else None,
                "mediumImageUrl": images.get("cover_medium") if images else None,
                "largeImageUrl": images.get("cover_xl") if images else None,
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("getAlbumInfo")
def get_album_info(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return _build_album_info(id, ctx, "albumInfo")


@_double_register("getAlbumInfo2")
def get_album_info2(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return _build_album_info(id, ctx, "albumInfo")
