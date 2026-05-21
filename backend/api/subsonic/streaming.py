from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Depends, Query, Response, Request

from backend.config import get_settings
from backend.db import queries
from backend.core import library
from backend.streaming import stream_track
from backend.streaming import decision as transcode_decision

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
    one `if err is not None: return err` line instead of duplicating branches.
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
async def stream(
    request: Request,
    id: str = Query(...),
    maxBitRate: Optional[int] = Query(default=None, ge=0, le=2000),
    format: Optional[str] = Query(default=None),
    timeOffset: Optional[float] = Query(default=None, ge=0),
    size: Optional[str] = Query(default=None),  # noqa: ARG001 — video-only, accepted
    estimateContentLength: bool = Query(default=False),  # noqa: ARG001 — accepted, not used
    converted: bool = Query(default=False),  # noqa: ARG001 — video-only, accepted
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Audio streaming with optional on-the-fly transcoding.

    Supports HTTP Range for raw streaming and seek-on-transcode via either
    `?timeOffset=<seconds>` or a `Range: bytes=N-` header (the latter is
    translated to a time offset using the target bitrate).
    """
    track, err = _resolve_track(id, ctx)
    if err is not None:
        return err

    return await stream_track(
        request=request,
        track_path=track["path"],
        track_suffix=track["suffix"],
        track_content_type=track["content_type"],
        track_bitrate=track.get("bitrate"),
        track_duration=track.get("duration"),
        requested_format=format,
        requested_bitrate=maxBitRate,
        time_offset=timeOffset,
    )


# ---- download (alias for stream without transcoding) ----------------------


@_double_register("download")
async def download(
    request: Request,
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    track, err = _resolve_track(id, ctx)
    if err is not None:
        return err

    resp = await stream_track(
        request=request,
        track_path=track["path"],
        track_suffix=track["suffix"],
        track_content_type=track["content_type"],
        track_bitrate=track.get("bitrate"),
        track_duration=track.get("duration"),
        requested_format=None,  # download is always raw
        requested_bitrate=None,
    )

    filename = f"{track['title']}.{track['suffix']}"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ---- getTranscodeDecision (OpenSubsonic "transcoding" extension) -----------


@_double_register("getTranscodeDecision")
async def get_transcode_decision(
    request: Request,
    mediaId: str = Query(...),
    mediaType: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """
    Tell a client, before it streams, whether we'd serve a track untouched
    or transcode it — and to what. Part of the OpenSubsonic `transcoding`
    extension (advertised in getOpenSubsonicExtensions).

    Wire shape
    ----------
    POST, with auth + `mediaId` + `mediaType` in the query string and a JSON
    `ClientInfo` body describing the caller's playback capabilities. (We also
    accept GET — the body is then empty, which yields a conservative
    "must transcode" answer.) `mediaType` is `song` or `podcast`; we only
    serve songs, so `podcast` is rejected.

    The actual matching lives in backend/streaming/decision.py; this handler
    just adapts a track row + the request body into that pure function and
    serialises the result into the Subsonic envelope.
    """
    # We have no podcast support, so only `song` is meaningful here.
    if mediaType.lower() != "song":
        return responses.error(
            responses.ERR_NOT_FOUND,
            "Only 'song' media is supported",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    track, err = _resolve_track(mediaId, ctx)
    if err is not None:
        return err

    # Parse the ClientInfo body. A missing/empty/invalid body is not fatal —
    # we treat it as "client declared nothing", which decide() reads as
    # "can't direct-play" and falls back to a server-default transcode.
    try:
        client_info = await request.json()
    except Exception:
        client_info = {}
    if not isinstance(client_info, dict):
        client_info = {}

    settings = get_settings()
    suffix = track["suffix"]
    # DB stores bitrate in kbps; the spec's StreamDetails wants bits/second.
    src_bitrate = track.get("bitrate")
    src_bitrate_bps = int(src_bitrate) * 1000 if src_bitrate else None

    result = transcode_decision.decide(
        source_container=suffix,
        source_codec=transcode_decision.codec_for_suffix(suffix),
        source_bitrate_bps=src_bitrate_bps,
        # Extended stream props (populated by the scanner; NULL on tracks not
        # yet rescanned, and bit depth is always NULL for lossy). decide()
        # treats any None as "can't evaluate" rather than guessing.
        source_channels=track.get("channels"),
        source_samplerate=track.get("sample_rate"),
        source_bitdepth=track.get("bit_depth"),
        client=client_info,
        transcoding_enabled=settings.transcoding_enabled,
        default_transcode_format=settings.default_transcode_format,
    )

    return responses.ok(
        {"transcodeDecision": result.to_dict()},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
