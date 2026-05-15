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
    _playlist_row_to_subsonic,
)

# ---- getPlaylists get table and return owners and public ----------------------


@_double_register("getPlaylists")
def get_playlists(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # query playlists table, return owner's + public playlists.
    payload = queries.list_playlists(ctx.user_id)

    playlists = [_playlist_row_to_subsonic(row) for row in payload]

    return responses.ok(
        {"playlists": {"playlist": playlists}}, fmt=ctx.fmt, callback=ctx.callback
    )


# ---- getPlaylist return playlist and tracks ----------------------


@_double_register("getPlaylist")
def get_playlist(
    id: str = Query(...), ctx: SubsonicContext = Depends(subsonic_context)
) -> Response:
    # load playlist + entries.
    payload = queries.get_playlist(id)
    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Playlists not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(
        {"playlist": _playlist_row_to_subsonic(payload)},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("createPlaylist")
def create_playlist(
    name: str = Query(...),
    songId: Optional[list[int]] = Query(default=[]),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    # insert playlist + tracks.
    with transaction():
        playlist_id = queries.create_playlist(name, ctx.user_id, songId)
    playlist = queries.get_playlist(playlist_id)
    return responses.ok(
        {"playlist": _playlist_row_to_subsonic(playlist)},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("updatePlaylist")
def update_playlist(
    playlistId: int = Query(...),
    name: Optional[str] = Query(default=None),
    comment: Optional[str] = Query(default=None),
    public: Optional[bool] = Query(default=True),
    songIdToAdd: Optional[list[str]] = Query(default=[]),
    songIndexToRemove: Optional[list[int]] = Query(default=[]),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    # update
    with transaction():
        playlist = queries.get_playlist(playlistId)
        if playlist is None:
            return responses.error(
                responses.ERR_NOT_FOUND,
                "Playlist not found",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )
        if playlist["owner_id"] != ctx.user_id:
            return responses.error(
                responses.ERR_NOT_AUTHORIZED,
                "Not your playlist",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )

        queries.update_playlist(playlistId, name, comment, public)
        if songIdToAdd:
            track_ids = [library.parse_id(sid)[1] for sid in songIdToAdd]
            queries.add_tracks_to_playlist(playlistId, track_ids)
        if songIndexToRemove:
            queries.remove_tracks_from_playlist(playlistId, songIndexToRemove)
    return responses.ok(
        fmt=ctx.fmt, callback=ctx.callback
    )  # no need to return anything


@_double_register("deletePlaylist")
def delete_playlist(
    playlist_id: int = Query(...), ctx: SubsonicContext = Depends(subsonic_context)
) -> Response:

    with transaction():
        row = queries.delete_playlist(playlist_id, ctx.user_id)
    if not row:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "playlist not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
