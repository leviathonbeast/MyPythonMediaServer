from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import Depends, Query, Response, Request

from backend.db import queries
from backend.core import library
from backend.streaming import stream_track

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


def _resolve_track(
    id: str, ctx: SubsonicContext
) -> Tuple[Optional[dict], Optional[Response]]:
    """
    Shared id-lookup for stream and download.

    Both endpoints need the same prelude: parse the Subsonic id, reject it if
    it isn't a track id, then fetch the track row. Returning (track, None) on
    success and (None, error_response) on failure lets each caller bail with
    one `if err is not None: return err` line instead of duplicating the two
    error branches.
    """
    kind, rid = library.parse_id(id)
    if kind != "track":
        return None, responses.error(
            responses.ERR_NOT_FOUND,
            "Not a track id",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    track = queries.get_track(rid)
    if track is None:
        return None, responses.error(
            responses.ERR_NOT_FOUND,
            "Track not found",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )
    return track, None


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
    track, err = _resolve_track(id, ctx)
    if err is not None:
        return err

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
    track, err = _resolve_track(id, ctx)
    if err is not None:
        return err
    t = stream_track(
        request=request,
        track_path=track["path"],
        track_suffix=track["suffix"],
        track_content_type=track["content_type"],
        track_bitrate=track.get("bitrate"),
        requested_format=None,  # never transcode on download
        requested_bitrate=None,
    )

    filename = f"{track['title']}.{track['suffix']}"
    t.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return t
