"""
Subsonic / OpenSubsonic API router.

Mounts under /rest and implements the OpenSubsonic 1.16.1 specification.

How the Subsonic protocol works (for a novice reader)
-----------------------------------------------------
Every endpoint:
  1. Always returns HTTP 200 — even on errors. The actual success/failure
     is indicated by {"subsonic-response": {"status": "ok" | "failed", ...}}
     in the body. This is a deliberate protocol design: if you returned HTTP
     401, clients would show "network error" instead of "wrong password".

  2. Supports three response formats:
       ?f=json   → JSON (default, used by modern clients and the web UI)
       ?f=xml    → XML (legacy clients)
       ?f=jsonp  → JSONP (ancient browser workaround, rarely used today)

  3. Authenticates every request via query parameters:
       ?u=username&p=password       (plaintext)
       ?u=username&t=token&s=salt   (MD5 of password+salt — more common)

  4. Each endpoint is available at both /rest/getAlbum and /rest/getAlbum.view
     because old clients append .view. We register both using @_double_register.

Implemented:
    /rest/ping, /rest/getLicense, /rest/getMusicFolders
    /rest/getIndexes, /rest/getMusicDirectory
    /rest/stream, /rest/download
    /rest/search3
    /rest/getAlbumList, /rest/getAlbumList2, /rest/getAlbum, /rest/getSong
    /rest/getCoverArt
    /rest/getUser, /rest/getUsers, /rest/createUser, /rest/updateUser
    /rest/deleteUser, /rest/changePassword
    /rest/getOpenSubsonicExtensions

Stubs (valid empty responses — clients won't error, but no real data):
    /rest/getPlaylists, /rest/getPlaylist, /rest/createPlaylist
    /rest/getStarred, /rest/star, /rest/unstar
    /rest/scrobble, /rest/getNowPlaying
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response

import sqlite3

from backend.config import get_settings
from backend.core import library, search
from backend.core.auth import hash_password, _decode_subsonic_password
from backend.db import queries
from backend.scanner import artwork as artwork_module
from backend.streaming import stream_track

# ---------------------------------------------------------------------------
# Helper: build a Subsonic User object from a DB row
# ---------------------------------------------------------------------------

def _user_row_to_subsonic(user: dict) -> dict:
    """
    Convert an internal user dict (from queries.get_user_by_username) to the
    shape that the Subsonic/OpenSubsonic spec requires in getUser / getUsers.

    The DB stores roles as integers (0 or 1) because SQLite has no boolean type.
    bool() converts them back to True/False for JSON/XML clients. The .get()
    calls with defaults handle the case where a column is missing from an older
    DB row that hasn't been migrated yet.
    """
    return {
        "username":            user["username"],
        "email":               user.get("email") or "",
        "scrobblingEnabled":   bool(user.get("scrobbling_enabled", False)),
        "maxBitRate":          user.get("max_bit_rate", 0),
        "adminRole":           bool(user.get("is_admin", False)),
        "settingsRole":        bool(user.get("settings_role", True)),
        "downloadRole":        bool(user.get("download_role", False)),
        "uploadRole":          bool(user.get("upload_role", False)),
        "playlistRole":        bool(user.get("playlist_role", True)),
        "coverArtRole":        bool(user.get("cover_art_role", False)),
        "commentRole":         bool(user.get("comment_role", False)),
        "podcastRole":         bool(user.get("podcast_role", False)),
        "streamRole":          bool(user.get("stream_role", True)),
        "jukeboxRole":         bool(user.get("jukebox_role", False)),
        "shareRole":           bool(user.get("share_role", False)),
        "videoConversionRole": bool(user.get("video_conversion_role", False)),
    }

from . import responses
from .deps import SubsonicAuthError, SubsonicContext, subsonic_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/rest", tags=["subsonic"])


def _double_register(path: str):
    """
    Decorator to register the same handler at /rest/<path> AND /rest/<path>.view.

    Why both? The original Subsonic server (Java) appended `.view` to every
    endpoint URL, e.g. `/rest/getIndexes.view`. Modern clients dropped the
    suffix, but older clients (DSub, iSub, early play:Sub) still hard-code
    the `.view` form. Rather than duplicating every function we use this
    helper to register one function at both paths automatically.

    Usage:
        @_double_register("ping")      # registers /rest/ping AND /rest/ping.view
        def ping(ctx=Depends(subsonic_context)):
            ...
    """
    def decorator(fn):
        router.add_api_route(f"/{path}",      fn, methods=["GET", "POST"])
        router.add_api_route(f"/{path}.view", fn, methods=["GET", "POST"])
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Centralised error handler. Each endpoint can raise SubsonicAuthError to bail.
# ---------------------------------------------------------------------------

def _handle_auth_error(e: SubsonicAuthError) -> Response:
    return responses.error(e.code, e.message, fmt=e.fmt, callback=e.callback)


# ===========================================================================
# Phase 1: working endpoints
# ===========================================================================

# ---- ping -----------------------------------------------------------------

@_double_register("ping")
def ping(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """
    Health check + auth probe. Clients call this to verify their credentials.

    Returns just the envelope, no payload.
    """
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


# ---- getLicense (most clients call this; cheap to satisfy) ----------------

@_double_register("getLicense")
def get_license(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """
    Subsonic upstream uses this to gate features behind a paid license.
    We're FOSS, so we always say 'valid'.
    """
    return responses.ok(
        {"license": {"valid": True, "email": "noreply@example.com", "trialExpires": None, "licenseExpires": None}},
        fmt=ctx.fmt, callback=ctx.callback,
    )


# ---- getMusicFolders ------------------------------------------------------

@_double_register("getMusicFolders")
def get_music_folders(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    folders = queries.list_music_folders()
    return responses.ok(
        {"musicFolders": {"musicFolder": [{"id": f["id"], "name": f["name"]} for f in folders]}},
        fmt=ctx.fmt, callback=ctx.callback,
    )


# ---- getIndexes -----------------------------------------------------------

@_double_register("getIndexes")
def get_indexes(
    ctx: SubsonicContext = Depends(subsonic_context),
    musicFolderId: Optional[int] = Query(default=None),
    ifModifiedSince: Optional[int] = Query(default=None),  # noqa: ARG001 — accepted, unused
) -> Response:
    """
    Top-level browse. Returns artists grouped by first letter.

    musicFolderId could narrow the result; we currently return the whole
    library. TODO: pass it down through queries.list_artists_indexed().
    """
    payload = library.get_indexes()
    # Subsonic envelope key is "indexes", with "lastModified" + the index list.
    import time as _t
    return responses.ok(
        {"indexes": {"lastModified": int(_t.time() * 1000), "ignoredArticles": "The El La Los Las Le Les", **payload}},
        fmt=ctx.fmt, callback=ctx.callback,
    )


# ---- getMusicDirectory ----------------------------------------------------

@_double_register("getMusicDirectory")
def get_music_directory(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_music_directory(id)
    if payload is None:
        return responses.error(responses.ERR_NOT_FOUND, "Directory not found", fmt=ctx.fmt, callback=ctx.callback)
    return responses.ok({"directory": payload}, fmt=ctx.fmt, callback=ctx.callback)


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
    result = search.search3(query, artistCount, albumCount, songCount,
                            artistOffset, albumOffset, songOffset)
    return responses.ok({"searchResult3": result}, fmt=ctx.fmt, callback=ctx.callback)


# ---- getAlbumList & getAlbumList2 -----------------------------------------

@_double_register("getAlbumList")
def get_album_list(
    type: str = Query(default="alphabeticalByName"),
    size: int = Query(default=10, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    fromYear: Optional[int] = Query(default=None),
    toYear: Optional[int] = Query(default=None),
    genre: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    albums = library.list_albums(type, size, offset, from_year=fromYear, to_year=toYear, genre=genre)
    return responses.ok({"albumList": {"album": albums}}, fmt=ctx.fmt, callback=ctx.callback)


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
    albums = library.list_albums(type, size, offset, from_year=fromYear, to_year=toYear, genre=genre)
    return responses.ok({"albumList2": {"album": albums}}, fmt=ctx.fmt, callback=ctx.callback)


# ---- getAlbum (returns album with its songs) ------------------------------

@_double_register("getAlbum")
def get_album(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_album_with_tracks(id)
    if payload is None:
        return responses.error(responses.ERR_NOT_FOUND, "Album not found", fmt=ctx.fmt, callback=ctx.callback)
    return responses.ok({"album": payload}, fmt=ctx.fmt, callback=ctx.callback)

# ---- getSong (returns Track with its props) ------------------------------

@_double_register("getSong")
def get_song(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    payload = library.get_song(id) # get_song func found in library.py
    if payload is None:
        return responses.error(responses.ERR_NOT_FOUND, "Track not found", fmt=ctx.fmt, callback=ctx.callback)
    return responses.ok({"song": payload}, fmt=ctx.fmt, callback=ctx.callback)

# ---- stream ---------------------------------------------------------------

@_double_register("stream")
def stream(
    request: Request,
    id: str = Query(...),
    maxBitRate: Optional[int] = Query(default=None, ge=0, le=2000),
    format: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Audio streaming with optional on-the-fly transcoding.

    Supports range requests (for raw streaming) and chunked transfer (for
    transcoded streams).
    """
    kind, rid = library.parse_id(id)
    if kind != "track":
        return responses.error(responses.ERR_NOT_FOUND, "Not a track id", fmt=ctx.fmt, callback=ctx.callback)
    track = queries.get_track(rid)
    if track is None:
        return responses.error(responses.ERR_NOT_FOUND, "Track not found", fmt=ctx.fmt, callback=ctx.callback)

    return stream_track(
        request=request,
        track_path=track["path"],
        track_suffix=track["suffix"],
        track_content_type=track["content_type"],
        track_bitrate=track.get("bitrate"),
        requested_format=format,
        requested_bitrate=maxBitRate,
    )


# ---- download (alias for stream without transcoding) ----------------------

@_double_register("download")
def download(
    request: Request,
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    kind, rid = library.parse_id(id)
    if kind != "track":
        return responses.error(responses.ERR_NOT_FOUND, "Not a track id", fmt=ctx.fmt, callback=ctx.callback)
    track = queries.get_track(rid)
    if track is None:
        return responses.error(responses.ERR_NOT_FOUND, "Track not found", fmt=ctx.fmt, callback=ctx.callback)
    return stream_track(
        request=request,
        track_path=track["path"],
        track_suffix=track["suffix"],
        track_content_type=track["content_type"],
        track_bitrate=track.get("bitrate"),
        requested_format=None,  # never transcode on download
        requested_bitrate=None,
    )


# ---- getCoverArt ----------------------------------------------------------

@_double_register("getCoverArt")
def get_cover_art(
    request: Request,
    id: str = Query(...),
    size: Optional[int] = Query(default=None),  # noqa: ARG001 — accepted, not implemented
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
        return responses.error(responses.ERR_NOT_FOUND, "Cover art not found", fmt=ctx.fmt, callback=ctx.callback)

    etag = f'"{id}"'
    cache_headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    suffix = path.suffix.lstrip(".").lower()
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")
    return Response(content=path.read_bytes(), media_type=media, headers=cache_headers)


# ===========================================================================
# Phase 2: placeholder stubs
# ===========================================================================
# These return valid-but-empty Subsonic responses so clients don't error out
# when they probe these endpoints. Real implementations are TODOs.

@_double_register("getPlaylists")
def get_playlists(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: query playlists table, return owner's + public playlists.
    return responses.ok({"playlists": {"playlist": []}}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getPlaylist")
def get_playlist(id: str = Query(...), ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: load playlist + entries.
    return responses.error(responses.ERR_NOT_FOUND, "Playlists not yet implemented", fmt=ctx.fmt, callback=ctx.callback)


@_double_register("createPlaylist")
def create_playlist(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: insert playlist + tracks.
    return responses.ok({"playlist": {"id": "0", "name": "stub", "songCount": 0, "duration": 0, "owner": ctx.username, "public": False, "entry": []}},
                        fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getStarred")
@_double_register("getStarred2")
def get_starred(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: read starred table.
    return responses.ok({"starred": {"artist": [], "album": [], "song": []}}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("star")
def star(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: insert into starred.
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("unstar")
def unstar(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: delete from starred.
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("scrobble")
def scrobble(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: bump play_counts row.
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getNowPlaying")
def get_now_playing(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: track in-flight stream sessions.
    return responses.ok({"nowPlaying": {"entry": []}}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getOpenSubsonicExtensions")
def get_open_subsonic_extensions(request: Request) -> Response:
    """
    Publicly accessible (no auth required) endpoint that advertises which
    OpenSubsonic extensions this server supports.
    """
    extensions = [
        {"name": "httpFormPost", "versions": [1]},
    ]
    return responses.ok(
        {"openSubsonicExtensions": extensions},
        fmt="json",
    )


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
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Not authorized", fmt=ctx.fmt, callback=ctx.callback)
    user = queries.get_user_by_username(username)
    if user is None:
        return responses.error(responses.ERR_NOT_FOUND, "User not found", fmt=ctx.fmt, callback=ctx.callback)
    return responses.ok({"user": _user_row_to_subsonic(user)}, fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getUsers")
def get_users(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """Return all users. Admin only. (Subsonic 1.8.0)"""
    if not ctx.is_admin:
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Admin required", fmt=ctx.fmt, callback=ctx.callback)
    all_users = queries.list_users()
    return responses.ok(
        {"users": {"user": [_user_row_to_subsonic(u) for u in all_users]}},
        fmt=ctx.fmt, callback=ctx.callback,
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
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Create a new user. Admin only. (Subsonic 1.1.0)"""
    if not ctx.is_admin:
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Admin required", fmt=ctx.fmt, callback=ctx.callback)
    plaintext = _decode_subsonic_password(password)
    try:
        queries.create_user(
            username, hash_password(plaintext),
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
    except sqlite3.IntegrityError:
        return responses.error(
            responses.ERR_GENERIC,
            f"Username '{username}' already exists",
            fmt=ctx.fmt, callback=ctx.callback,
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
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Update an existing user. Admin only. (Subsonic 1.10.1)"""
    if not ctx.is_admin:
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Admin required", fmt=ctx.fmt, callback=ctx.callback)

    password_hash = hash_password(_decode_subsonic_password(password)) if password else None

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
    if not found:
        return responses.error(responses.ERR_NOT_FOUND, f"User '{username}' not found", fmt=ctx.fmt, callback=ctx.callback)
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("deleteUser")
def delete_user(
    username: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Delete a user. Admin only. (Subsonic 1.3.0)"""
    if not ctx.is_admin:
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Admin required", fmt=ctx.fmt, callback=ctx.callback)
    if username == ctx.username:
        return responses.error(
            responses.ERR_GENERIC,
            "Cannot delete your own account",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    deleted = queries.delete_user_by_username(username)
    if not deleted:
        return responses.error(responses.ERR_NOT_FOUND, f"User '{username}' not found", fmt=ctx.fmt, callback=ctx.callback)
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
        return responses.error(responses.ERR_NOT_AUTHORIZED, "Not authorized", fmt=ctx.fmt, callback=ctx.callback)
    user = queries.get_user_by_username(username)
    if user is None:
        return responses.error(responses.ERR_NOT_FOUND, "User not found", fmt=ctx.fmt, callback=ctx.callback)
    plaintext = _decode_subsonic_password(password)
    queries.update_user_password(user["id"], hash_password(plaintext))
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


# ---------------------------------------------------------------------------
# Centralised exception handler
# ---------------------------------------------------------------------------
# Registered on the FastAPI app in main.py — see add_exception_handler call.
async def subsonic_auth_exception_handler(request: Request, exc: SubsonicAuthError):
    return _handle_auth_error(exc)
