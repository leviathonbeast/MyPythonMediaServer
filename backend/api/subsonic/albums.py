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
    # Spec marks `type` as required; we default to alphabeticalByName so that
    # clients which omit it (and our internal smoke tests) get a usable result
    # rather than a 422.
    type: str = Query(default="alphabeticalByName"),
    size: int = Query(default=10, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    fromYear: Optional[int] = Query(default=None),
    toYear: Optional[int] = Query(default=None),
    genre: Optional[str] = Query(default=None),
    musicFolderId: Optional[int] = Query(default=None),  # noqa: ARG001 — accepted, scoping NYI
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
    musicFolderId: Optional[int] = Query(default=None),  # noqa: ARG001 — accepted, scoping NYI
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


def _resolve_cover_art_id(raw: str) -> Optional[str]:
    """Map a Subsonic id to its stored artwork hash.

    Clients are inconsistent about what they pass as a cover-art id. Some
    use the content hash we emit in `coverArt` (e.g. from track/album
    serialisation); others pass the owning entity's id — getStarred even
    emits `al-`/`ar-` prefixed coverArt deliberately, so a spec-following
    client will hand those back here. Resolve the three prefixed forms to
    the underlying album-cover hash:

      tr-<n> → the track's album cover
      al-<n> → the album's cover
      ar-<n> → the artist's representative album cover

    A value with no recognised prefix is assumed to already be a content
    hash and returned unchanged (find_artwork_path validates its shape).
    Returns None when the entity exists but has no artwork, or the id is
    unresolvable.
    """
    if raw.startswith(library.TRACK_PREFIX):
        _, rid = library.parse_id(raw)
        track = queries.get_track(rid) if rid is not None else None
        return track.get("cover_art_id") if track else None
    if raw.startswith(library.ALBUM_PREFIX):
        _, rid = library.parse_id(raw)
        album = queries.get_album(rid) if rid is not None else None
        return album.get("cover_art_id") if album else None
    if raw.startswith(library.ARTIST_PREFIX):
        _, rid = library.parse_id(raw)
        return queries.get_artist_cover_art_id(rid) if rid is not None else None
    return raw


@_double_register("getCoverArt")
def get_cover_art(
    request: Request,
    id: str = Query(...),
    size: Optional[int] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Serve a cached cover art file by id, optionally resized.

    `id` may be a content hash or a `tr-`/`al-`/`ar-` entity id (clients
    differ — see _resolve_cover_art_id), which we resolve to the backing
    hash before touching disk.

    When `size` is provided, returns a `<= size`x`<= size` JPEG variant
    (aspect preserved). Variants are cached on disk as `<hash>_<size>.jpg`
    next to the source; the first request takes the resize hit (~50-200ms
    for a typical 2-3MB FLAC embed), every subsequent request is a plain
    file read. Sources smaller than `size` are served unchanged — no
    point upscaling.

    The ETag keys on the resolved (hash, size) rather than the request id,
    so two entities sharing one album cover get the same ETag and browsers
    get 304s on revalidation regardless of which id form they used.
    """
    cover_id = _resolve_cover_art_id(id)
    if cover_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Cover art not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    if size is not None and size > 0:
        path = artwork_module.resize_cached(cover_id, size)
    else:
        path = artwork_module.find_artwork_path(cover_id)

    if path is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Cover art not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    etag = f'"{cover_id}-{size}"' if size else f'"{cover_id}"'
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
