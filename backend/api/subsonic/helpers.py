from __future__ import annotations

import logging
import time as _t
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from backend.core.library import track_to_subsonic, _album_to_directory_child
from .. import responses
from ..deps import SubsonicAuthError, SubsonicContext, subsonic_context

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

    `folder` is a required list of accessible music-folder ids; we expose all
    folders for every user since per-user folder restrictions aren't modelled
    yet. An empty list would technically be valid but trips some clients that
    treat empty == no access.
    """
    from backend.db import queries
    folder_ids = [f["id"] for f in queries.list_music_folders()]
    return {
        "username": user["username"],
        "email": user.get("email") or "",
        "scrobblingEnabled": bool(user.get("scrobbling_enabled", False)),
        "maxBitRate": user.get("max_bit_rate", 0),
        "adminRole": bool(user.get("is_admin", False)),
        "settingsRole": bool(user.get("settings_role", True)),
        "downloadRole": bool(user.get("download_role", False)),
        "uploadRole": bool(user.get("upload_role", False)),
        "playlistRole": bool(user.get("playlist_role", True)),
        "coverArtRole": bool(user.get("cover_art_role", False)),
        "commentRole": bool(user.get("comment_role", False)),
        "podcastRole": bool(user.get("podcast_role", False)),
        "streamRole": bool(user.get("stream_role", True)),
        "jukeboxRole": bool(user.get("jukebox_role", False)),
        "shareRole": bool(user.get("share_role", False)),
        "videoConversionRole": bool(user.get("video_conversion_role", False)),
        "folder": folder_ids,
    }


def _playlist_row_to_subsonic(playlist: dict) -> dict:
    """Convert an internal playlist dict to the shape the Subsonic/OpenSubsonic spec requires."""

    created_at_iso = datetime.fromtimestamp(
        playlist["created_at"], tz=timezone.utc
    ).isoformat()
    updated_at_iso = datetime.fromtimestamp(
        playlist["updated_at"], tz=timezone.utc
    ).isoformat()
    return {
        "id": playlist["id"],
        "owner": playlist["owner"],
        "name": playlist["name"],
        "comment": playlist["comment"],
        "public": bool(playlist["is_public"]),
        "created": created_at_iso,
        "changed": updated_at_iso,
        "entry": [track_to_subsonic(t) for t in playlist.get("tracks", [])],
        "songCount": playlist["trackcount"],
        "duration": playlist["duration"] or 0,
    }


from ..deps import SubsonicAuthError, SubsonicContext, subsonic_context


def find_similar_deduped(seed_id: int, count: int) -> list[tuple[int, float]]:
    """similarity.find_similar for a seed, with library duplicates collapsed.

    A library commonly holds the same recording as several files (a single plus
    its album track, a remaster beside the original, plain duplicate rips).
    Their fingerprints are near-identical, so a raw nearest-neighbour query
    stacks every copy at the top and "radio" replays one song repeatedly. We
    give find_similar a key function that identifies a *logical song* — by
    recording MBID when the file is tagged, else case-insensitive artist+title —
    so only the first copy of each is kept (and copies of the seed are dropped).

    Track rows are cached per call so the key function doesn't re-query an id it
    has already looked up while walking the ranking.
    """
    from backend.db import queries
    from backend.core import library, similarity

    cache: dict[int, Optional[dict]] = {}

    def _key_for(track_id: int):
        if track_id not in cache:
            cache[track_id] = queries.get_track(track_id)
        return library.logical_song_key(cache[track_id])

    return similarity.find_similar(
        queries.get_all_track_features(), seed_id, count, key_for=_key_for
    )


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
        router.add_api_route(f"/{path}", fn, methods=["GET", "POST"])
        router.add_api_route(f"/{path}.view", fn, methods=["GET", "POST"])
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Centralised error handler. Each endpoint can raise SubsonicAuthError to bail.
# ---------------------------------------------------------------------------


def _handle_auth_error(e: SubsonicAuthError) -> Response:
    return responses.error(e.code, e.message, fmt=e.fmt, callback=e.callback)
