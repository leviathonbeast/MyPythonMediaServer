from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.core.auth import hash_password, encrypt_password
from backend.db import queries
from backend.db.connection import transaction
from backend.db.errors import IntegrityError

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
    _user_row_to_subsonic,
)


def _decode_password(p: str) -> str:
    """Decode Subsonic's optional enc:<hex> password encoding."""
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return p
    return p


# ---------------------------------------------------------------------------
# User management (Subsonic 1.3.0 + OpenSubsonic)
# ---------------------------------------------------------------------------


@_double_register("getUser")
def get_user(
    username: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Return a user object. Non-admins may only view their own account."""
    if username != ctx.username and not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Not authorized",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    user = queries.get_user_by_username(username)
    if user is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "User not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(
        {"user": _user_row_to_subsonic(user)}, fmt=ctx.fmt, callback=ctx.callback
    )


@_double_register("getUsers")
def get_users(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """Return all users. Admin only. (Subsonic 1.8.0)"""
    if not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Admin required",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    all_users = queries.list_users()
    return responses.ok(
        {"users": {"user": [_user_row_to_subsonic(u) for u in all_users]}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("createUser")
def create_user(
    username: str = Query(...),
    password: str = Query(...),
    email: Optional[str] = Query(default=None),
    ldapAuthenticated: bool = Query(default=False),  # accepted, not implemented
    adminRole: bool = Query(default=False),
    settingsRole: bool = Query(default=True),
    streamRole: bool = Query(default=True),
    jukeboxRole: bool = Query(default=False),
    downloadRole: bool = Query(default=False),
    uploadRole: bool = Query(default=False),
    playlistRole: bool = Query(default=True),
    coverArtRole: bool = Query(default=False),
    commentRole: bool = Query(default=False),
    podcastRole: bool = Query(default=False),
    shareRole: bool = Query(default=False),
    videoConversionRole: bool = Query(default=False),
    maxBitRate: Optional[int] = Query(default=None),
    musicFolderId: Optional[list[int]] = Query(default=None),  # noqa: ARG001 — accepted, per-user folder restrictions NYI
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Create a new user. Admin only. (Subsonic 1.1.0)"""
    if not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Admin required",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    plaintext = _decode_password(password)
    try:
        with transaction():
            new_id = queries.create_user(
                username,
                hash_password(plaintext),
                is_admin=adminRole,
                email=email,
                settings_role=settingsRole,
                stream_role=streamRole,
                jukebox_role=jukeboxRole,
                download_role=downloadRole,
                upload_role=uploadRole,
                playlist_role=playlistRole,
                cover_art_role=coverArtRole,
                comment_role=commentRole,
                podcast_role=podcastRole,
                share_role=shareRole,
                video_conversion_role=videoConversionRole,
            )
            queries.update_encrypted_password(new_id, encrypt_password(plaintext))
            if maxBitRate is not None:
                queries.update_user(username, max_bit_rate=maxBitRate)
    except IntegrityError:
        return responses.error(
            responses.ERR_GENERIC,
            f"Username '{username}' already exists",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("updateUser")
def update_user(
    username: str = Query(...),
    password: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    adminRole: Optional[bool] = Query(default=None),
    settingsRole: Optional[bool] = Query(default=None),
    streamRole: Optional[bool] = Query(default=None),
    jukeboxRole: Optional[bool] = Query(default=None),
    downloadRole: Optional[bool] = Query(default=None),
    uploadRole: Optional[bool] = Query(default=None),
    playlistRole: Optional[bool] = Query(default=None),
    coverArtRole: Optional[bool] = Query(default=None),
    commentRole: Optional[bool] = Query(default=None),
    podcastRole: Optional[bool] = Query(default=None),
    shareRole: Optional[bool] = Query(default=None),
    videoConversionRole: Optional[bool] = Query(default=None),
    maxBitRate: Optional[int] = Query(default=None),
    musicFolderId: Optional[list[int]] = Query(default=None),  # noqa: ARG001 — accepted, per-user folder restrictions NYI
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Update an existing user. Admin only. (Subsonic 1.10.1)"""
    if not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Admin required",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    plaintext = _decode_password(password) if password else None
    password_hash = hash_password(plaintext) if plaintext else None

    with transaction():
        found = queries.update_user(
            username,
            password_hash=password_hash,
            email=email,
            is_admin=adminRole,
            settings_role=settingsRole,
            stream_role=streamRole,
            jukebox_role=jukeboxRole,
            download_role=downloadRole,
            upload_role=uploadRole,
            playlist_role=playlistRole,
            cover_art_role=coverArtRole,
            comment_role=commentRole,
            podcast_role=podcastRole,
            share_role=shareRole,
            video_conversion_role=videoConversionRole,
            max_bit_rate=maxBitRate,
        )
        if found and plaintext:
            user = queries.get_user_by_username(username)
            if user:
                queries.update_encrypted_password(user["id"], encrypt_password(plaintext))

    if not found:
        return responses.error(
            responses.ERR_NOT_FOUND,
            f"User '{username}' not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("deleteUser")
def delete_user(
    username: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Delete a user. Admin only. (Subsonic 1.3.0)"""
    if not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Admin required",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    if username == ctx.username:
        return responses.error(
            responses.ERR_GENERIC,
            "Cannot delete your own account",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    with transaction():
        deleted = queries.delete_user_by_username(username)
    if not deleted:
        return responses.error(
            responses.ERR_NOT_FOUND,
            f"User '{username}' not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("changePassword")
def change_password(
    username: str = Query(...),
    password: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Change a user's password. Non-admins can only change their own. (Subsonic 1.1.0)
    """
    if username != ctx.username and not ctx.is_admin:
        return responses.error(
            responses.ERR_NOT_AUTHORIZED,
            "Not authorized",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    user = queries.get_user_by_username(username)
    if user is None:
        return responses.error(
            responses.ERR_NOT_FOUND,
            "User not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    plaintext = _decode_password(password)
    with transaction():
        queries.update_user_password(user["id"], hash_password(plaintext))
        queries.update_encrypted_password(user["id"], encrypt_password(plaintext))
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
