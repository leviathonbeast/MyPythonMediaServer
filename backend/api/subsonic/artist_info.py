from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.core import library, lastfm, deezer

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


def _build_artist_info(id: str, ctx: SubsonicContext, key: str) -> Response:
    """
    Shared body for getArtistInfo and getArtistInfo2.

    The two Subsonic endpoints return identical data; the only difference is
    the response envelope key ("artistInfo" vs "artistInfo2"). `key` selects
    which one to emit so the lookup, bio fetch, and image fetch are written
    only once.
    """
    internal_id = library.parse_artist_id(id)
    if internal_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Invalid artist id",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    artist = queries.get_artist(internal_id)
    if artist is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Artist not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    bio = lastfm.get_artist_bio(artist["name"])
    images = deezer.get_artist_images(artist["name"])

    return responses.ok(
        {
            key: {
                "biography": bio.summary if bio else None,
                "musicBrainzId": artist.get("musicbrainz_id") or "",
                "smallImageUrl": images.get("picture_small") if images else None,
                "mediumImageUrl": images.get("picture_medium") if images else None,
                "largeImageUrl": images.get("picture_xl") if images else None,
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("getArtistInfo")
def get_artist_info(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return _build_artist_info(id, ctx, "artistInfo")


@_double_register("getArtistInfo2")
def get_artist_info_2(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return _build_artist_info(id, ctx, "artistInfo2")


@_double_register("getRandomSongs")
def get_random_songs(
    size: int = Query(default=10, ge=1, le=500),
    genre: Optional[str] = Query(default=None),
    fromYear: Optional[int] = Query(default=None),
    toYear: Optional[int] = Query(default=None),
    musicFolderId: Optional[int] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    tracks = queries.list_random_songs(
        size=size,
        genre=genre,
        from_year=fromYear,
        to_year=toYear,
        music_folder_id=musicFolderId,
    )
    return responses.ok(
        {"randomSongs": {"song": [library.track_to_subsonic(t) for t in tracks]}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("getTopSongs")
def get_top_songs(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return responses.ok(
        {"topSongs": {"song": []}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
