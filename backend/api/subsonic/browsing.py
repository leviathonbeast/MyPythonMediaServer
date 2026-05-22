from __future__ import annotations

import time as _t
from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.core import library

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
    _album_to_directory_child,
)

# ---- getIndexes -----------------------------------------------------------


@_double_register("getIndexes")
def get_indexes(
    ctx: SubsonicContext = Depends(subsonic_context),
    musicFolderId: Optional[int] = Query(default=None),
    ifModifiedSince: Optional[int] = Query(
        default=None
    ),  # noqa: ARG001 — accepted, unused
) -> Response:
    """
    Top-level browse. Returns artists grouped by first letter.

    musicFolderId could narrow the result; we currently return the whole
    library. TODO: pass it down through queries.list_artists_indexed().
    """
    payload = library.get_indexes()
    # Subsonic envelope key is "indexes", with "lastModified" + the index list.

    return responses.ok(
        {
            "indexes": {
                "lastModified": int(_t.time() * 1000),
                "ignoredArticles": "The El La Los Las Le Les",
                **payload,
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- getArtists -----------------------------------------------------


@_double_register("getArtists")
def get_artists(
    ctx: SubsonicContext = Depends(subsonic_context),
    musicFolderId: Optional[int] = Query(default=None),
) -> Response:

    artists = library.get_indexes()
    return responses.ok(
        {
            "artists": {
                "ignoredArticles": "The An A Die Das Ein Eine Les Le La",
                **artists,
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- getArtist -----------------------------------------------------


@_double_register("getArtist")
def get_artist(
    ctx: SubsonicContext = Depends(subsonic_context),
    id: str = Query(...),
) -> Response:

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

    albums = queries.list_artist_albums(internal_id)

    artist_payload = {
        "id": library.make_artist_id(artist["id"]),  # returns "ar-123"
        "name": artist["name"],
        "coverArt": artist.get("coverArt"),
        "albumCount": len(albums),
    }

    # Per-user + average ratings (setRating). Omitted when unrated so we never
    # send a misleading 0-star value for a never-rated artist.
    user_rating = queries.get_user_rating(ctx.user_id, "artist", internal_id)
    if user_rating is not None:
        artist_payload["userRating"] = user_rating
    avg_rating = queries.get_average_rating("artist", internal_id)
    if avg_rating is not None:
        artist_payload["averageRating"] = avg_rating

    return responses.ok(
        {
            "artist": {
                **artist_payload,
                "album": [
                    _album_to_directory_child(al, artist["name"]) for al in albums
                ],
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- getMusicDirectory ----------------------------------------------------


@_double_register("getMusicDirectory")
def get_music_directory(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_music_directory(id)
    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Directory not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok({"directory": payload}, fmt=ctx.fmt, callback=ctx.callback)
