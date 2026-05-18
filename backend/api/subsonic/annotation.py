from __future__ import annotations

from fastapi import Depends, Query, Response

from typing import Optional

from backend.db import queries
from backend.db.connection import transaction
from backend.core import library
from datetime import datetime, timezone

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
    track_to_subsonic,
)


def _build_starred(user_id: int) -> dict:
    """
    One-query hydrate. The wide projection in get_starred_items joins
    starred against tracks/albums/artists (and their parent artist/album
    rows), so we never need a per-row fetch here — every column we use
    below comes from the original query.
    """
    rows = queries.get_starred_items(user_id)
    artists: list[dict] = []
    albums: list[dict] = []
    songs: list[dict] = []

    for item in rows:
        starred_at = datetime.fromtimestamp(
            item["starred_at"], tz=timezone.utc
        ).isoformat()
        target_type = item["target_type"]
        target_id = item["target_id"]

        if target_type == "artist":
            # LEFT JOIN means missing rows yield NULL — skip those.
            if item.get("artist_id") is None:
                continue
            artists.append(
                {
                    "id": library.make_artist_id(item["artist_id"]),
                    "name": item["artist_name"],
                    "coverArt": library.make_artist_id(item["artist_id"]),
                    "starred": starred_at,
                }
            )

        elif target_type == "album":
            if item.get("album_name") is None:
                continue
            albums.append(
                {
                    "id": library.make_album_id(target_id),
                    "parent": library.make_artist_id(item["album_artist_id"]),
                    "album": item["album_name"],
                    "title": item["album_name"],
                    "name": item["album_name"],
                    "isDir": True,
                    "coverArt": library.make_album_id(target_id),
                    "songCount": item["album_track_count"],
                    "created": datetime.fromtimestamp(
                        item["album_created_at"], tz=timezone.utc
                    ).isoformat(),
                    "duration": item["album_duration"],
                    "playCount": 0,  # NYI
                    "artistId": library.make_artist_id(item["album_artist_id"]),
                    "artist": item["album_artist_name"] or "Unknown Artist",
                    "year": item["album_year"],
                    "genre": item["album_genre"],
                    "starred": starred_at,
                }
            )

        elif target_type == "track":
            if item.get("track_title") is None:
                continue
            # Synthesise a row-shaped dict so track_to_subsonic() stays
            # the single source of truth for the song response shape. The
            # aliased columns map back to the names that helper expects.
            track_row = {
                "id": item["track_id"],
                "title": item["track_title"],
                "path": item["track_path"],
                "duration": item["track_duration"],
                "track_number": item["track_number"],
                "disc_number": item["track_disc"],
                "year": item["track_year"],
                "genre": item["track_genre"],
                "suffix": item["track_suffix"],
                "content_type": item["track_content_type"],
                "bitrate": item["track_bitrate"],
                "size": item["track_size"],
                "album_id": item["track_album_id"],
                "artist_id": item["track_artist_id"],
                "album_name": item["track_album_name"],
                "artist_name": item["track_artist_name"],
                "cover_art_id": item["track_cover_art_id"],
            }
            songs.append({**track_to_subsonic(track_row), "starred": starred_at})

    return {"artist": artists, "album": albums, "song": songs}


# Play count query
@_double_register("scrobble")
def scrobble(
    id: list[str] = Query(...),
    time: Optional[list[int]] = Query(default=None),  # noqa: ARG001 — accepted, unused
    submission: bool = Query(default=True),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Register a playback or "now playing" event. (Subsonic 1.5.0; multi-id 1.8.0)

    `id` may be repeated to scrobble several files at once. `time` (epoch ms)
    is accepted for protocol compatibility but currently ignored — we use
    server-side now() in queries.play_count.
    """
    # Resolve every id up front so a single bad value can be reported clearly.
    resolved: list[int] = []
    for raw in id:
        kind, rid = library.parse_id(raw)
        if kind != "track" or rid is None:
            return responses.error(
                responses.ERR_NOT_FOUND,
                "Not a track id",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )
        if queries.get_track(rid) is None:
            return responses.error(
                responses.ERR_NOT_FOUND,
                "Track not found",
                fmt=ctx.fmt,
                callback=ctx.callback,
            )
        resolved.append(rid)

    if submission:
        with transaction():
            for rid in resolved:
                queries.play_count(ctx.user_id, rid)
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getStarred")
def get_starred(
    musicFolderId: Optional[int] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return responses.ok(
        {"starred": _build_starred(ctx.user_id)},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# calls _build_starred at the top of this document
@_double_register("getStarred2")
def get_starred2(
    musicFolderId: Optional[int] = Query(default=None),  # noqa: ARG001 — accepted, scoping NYI
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    return responses.ok(
        {"starred2": _build_starred(ctx.user_id)},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("star")
def star(
    id: Optional[list[str]] = Query(default=[]),
    albumId: Optional[list[str]] = Query(default=[]),
    artistId: Optional[list[str]] = Query(default=[]),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    with transaction():
        for sid in id:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.star_item(ctx.user_id, "track", rid)

        for sid in albumId:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.star_item(ctx.user_id, "album", rid)

        for sid in artistId:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.star_item(ctx.user_id, "artist", rid)

    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("unstar")
def unstar(
    id: Optional[list[str]] = Query(default=[]),
    albumId: Optional[list[str]] = Query(default=[]),
    artistId: Optional[list[str]] = Query(default=[]),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:

    with transaction():
        for sid in id:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.unstar_item(ctx.user_id, "track", rid)

        for sid in albumId:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.unstar_item(ctx.user_id, "album", rid)

        for sid in artistId:
            _, rid = library.parse_id(sid)
            if rid is None:
                continue
            queries.unstar_item(ctx.user_id, "artist", rid)

    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("getNowPlaying")
def get_now_playing(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    # TODO: track in-flight stream sessions.
    return responses.ok(
        {"nowPlaying": {"entry": []}}, fmt=ctx.fmt, callback=ctx.callback
    )
