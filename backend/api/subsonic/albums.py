from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response, Request

from backend.db import queries
from backend.core import library
from backend.scanner import artwork as artwork_module

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)

# ---- getAlbumList & getAlbumList2 -----------------------------------------


def _album_list_response(
    key: str,
    type: str,
    size: int,
    offset: int,
    fromYear: Optional[int],
    toYear: Optional[int],
    genre: Optional[str],
    ctx: SubsonicContext,
) -> Response:
    """
    Shared body for getAlbumList and getAlbumList2.

    Both endpoints return the same album list; only the envelope key differs
    ("albumList" for the legacy folder-tag form, "albumList2" for the ID3-tag
    form). `key` selects which one to emit. The per-handler `size` cap stays
    on the endpoint signatures because the two endpoints advertise different
    maximums to clients.
    """
    albums = library.list_albums(
        type, size, offset, from_year=fromYear, to_year=toYear, genre=genre
    )
    return responses.ok({key: {"album": albums}}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getAlbumList")
def get_album_list(
    type: str = Query(default="alphabeticalByName"),
    size: int = Query(default=10, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    fromYear: Optional[int] = Query(default=None),
    toYear: Optional[int] = Query(default=None),
    genre: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return _album_list_response(
        "albumList", type, size, offset, fromYear, toYear, genre, ctx
    )


@_double_register("getAlbumList2")
def get_album_list2(
    type: str = Query(default="alphabeticalByName"),
    size: int = Query(default=10, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    fromYear: Optional[int] = Query(default=None),
    toYear: Optional[int] = Query(default=None),
    genre: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """ID3-tag-based version of getAlbumList. Same data shape for our purposes."""
    return _album_list_response(
        "albumList2", type, size, offset, fromYear, toYear, genre, ctx
    )


# ---- getAlbum (returns album with its songs) ------------------------------


@_double_register("getAlbum")
def get_album(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_album_with_tracks(id)
    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Album not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok({"album": payload}, fmt=ctx.fmt, callback=ctx.callback)


# ---- getSong (returns Track with its props) ------------------------------


@_double_register("getSong")
def get_song(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_song(id)  # get_song func found in library.py

    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Track not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    _, rid = library.parse_id(id)
    plays = queries.get_playcount_by_user(ctx.user_id, rid)
    payload["playCount"] = plays
    return responses.ok({"song": payload}, fmt=ctx.fmt, callback=ctx.callback)


# ---- getCoverArt ----------------------------------------------------------


@_double_register("getCoverArt")
def get_cover_art(
    request: Request,
    id: str = Query(...),
    size: Optional[int] = Query(
        default=None
    ),  # noqa: ARG001 — accepted, not implemented
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Serve a cached cover art file by hash id. `size` is accepted for protocol
    compatibility; on-the-fly resizing is a TODO (would use Pillow).

    The id is already a content hash, so the URL is immutable — we cache it
    for a year and use the id as the ETag so browsers get 304s on revalidation.
    """
    path = artwork_module.find_artwork_path(id)
    if path is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Cover art not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    etag = f'"{id}"'
    cache_headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": etag,
    }

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    suffix = path.suffix.lstrip(".").lower()
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
        suffix, "image/jpeg"
    )
    return Response(content=path.read_bytes(), media_type=media, headers=cache_headers)


# ----- getGenres ----------------------------------------------------
@_double_register("getGenres")
def get_genres(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    genres = queries.list_genre_count()

    return responses.ok(
        {
            "genres": {
                "genre": [
                    {
                        "value": g["genre"],
                        "songCount": g["songCount"],
                        "albumCount": g["albumCount"],
                    }
                    for g in genres
                ]
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ----- getSongsByGenres ----------------------------------------------------
@_double_register("getSongsByGenre")
def get_songs_by_genre(
    genre: str = Query(...),
    count: int = Query(default=10, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    musicFolderId: Optional[int] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    tracks = queries.list_song_by_genre(genre, count, offset, musicFolderId)

    return responses.ok(
        {"songsByGenre": {"song": [library.track_to_subsonic(t) for t in tracks]}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
