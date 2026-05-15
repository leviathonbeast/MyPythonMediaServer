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
def get_playlists(
    username: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    List playlists. (Subsonic 1.0.0; `username` since 1.8.0)

    With no `username`, returns playlists owned by the caller plus all public
    ones. If `username` is given, returns that user's playlists — only admins
    may impersonate another user.
    """
    target_user_id = ctx.user_id
    if username and username != ctx.username:
        if not ctx.is_admin:
            return responses.error(
                responses.ERR_NOT_AUTHORIZED,
                "Admin required to view another user's playlists",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )
        other = queries.get_user_by_username(username)
        if other is None:
            return responses.error(
                responses.ERR_NOT_FOUND,
                f"User '{username}' not found",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )
        target_user_id = other["id"]

    payload = queries.list_playlists(target_user_id)
    playlists = [_playlist_row_to_subsonic(row) for row in payload]
    return responses.ok(
        {"playlists": {"playlist": playlists}}, fmt=ctx.fmt, callback=ctx.callback
    )


# ---- getPlaylist return playlist and tracks ----------------------


@_double_register("getPlaylist")
def get_playlist(
    id: int = Query(...), ctx: SubsonicContext = Depends(subsonic_context)
) -> Response:
    payload = queries.get_playlist(id)
    if payload is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Playlist not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    # Caller must own the playlist, be admin, or the playlist must be public.
    if (
        payload["owner_id"] != ctx.user_id
        and not payload.get("is_public")
        and not ctx.is_admin
    ):
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Not authorized to view this playlist",
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
    name: Optional[str] = Query(default=None),
    playlistId: Optional[int] = Query(default=None),
    songId: Optional[list[str]] = Query(default=[]),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Create or replace a playlist. (Subsonic 1.2.0)

    If `playlistId` is given the existing playlist is replaced with the new
    song set; otherwise a new playlist is created with `name`. `songId`
    values are accepted in Subsonic prefixed form ("tr-123") or as raw ints.
    """
    track_ids: list[int] = []
    for raw in (songId or []):
        _, rid = library.parse_id(str(raw))
        if rid is not None:
            track_ids.append(rid)

    with transaction():
        if playlistId is not None:
            existing = queries.get_playlist(playlistId)
            if existing is None:
                return responses.error(
                    responses.ERR_NOT_FOUND,
                    "Playlist not found",
                    fmt=ctx.fmt,
                    callback=ctx.callback,
                )
            if existing["owner_id"] != ctx.user_id and not ctx.is_admin:
                return responses.error(
                    responses.ERR_NOT_AUTHORIZED,
                    "Not your playlist",
                    fmt=ctx.fmt,
                    callback=ctx.callback,
                )
            queries.replace_playlist_tracks(playlistId, track_ids)
            if name is not None:
                queries.update_playlist(playlistId, name, None, None)
            new_id = playlistId
        else:
            if not name:
                return responses.error(
                    responses.ERR_PARAMETER,
                    "Required parameter 'name' is missing",
                    fmt=ctx.fmt,
                    callback=ctx.callback,
                )
            new_id = queries.create_playlist(name, ctx.user_id, track_ids)

    playlist = queries.get_playlist(new_id)
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
    public: Optional[bool] = Query(default=None),
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
    id: int = Query(...), ctx: SubsonicContext = Depends(subsonic_context)
) -> Response:
    with transaction():
        row = queries.delete_playlist(id, ctx.user_id)
    if not row:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Playlist not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
