"""
HTTP audio streamer (async).

Three response shapes come out of this module:

    * RAW, full file        — 200, with Content-Length.
    * RAW, byte range       — 206, with Content-Range / Content-Length.
    * TRANSCODED, with seek — 206, with a *synthesized* Content-Range/Length
                              (byte positions are derived from target bitrate
                              × source duration; accurate enough for browser
                              seek bars, but not byte-exact).
    * TRANSCODED, no seek   — 200, chunked, no length.

Seek-on-transcode
-----------------
Two ways for the client to ask for a seek on a transcoded stream:

    1. `?timeOffset=<seconds>` — Subsonic-style explicit seek.
    2. `Range: bytes=N-`       — translated to a time offset via the target
                                  bitrate (`seek = N / bytes_per_second`).

Either way we pass `-ss <seconds>` to ffmpeg and start decoding from there.

Policy
------
* `transcoding_enabled=False`  → raw, every time.
* `format=raw` (or unset and `default_transcode_format=raw`) → raw.
  Raw streams are NEVER bitrate-capped — the user opted in to the full file.
* Anything else → transcoded, clamped to
  `min(requested_bitrate, max_streaming_bitrate, MAX_TRANSCODE_BITRATE=320)`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, Response

from backend.config import get_settings
from .presets import MAX_TRANSCODE_BITRATE, TranscodePreset, resolve_preset
from .transcoder import TranscodeStream

log = logging.getLogger(__name__)

# Range: bytes=START-END  (END optional; we ignore multipart ranges — almost
# no audio client uses those).
_RANGE_RE = re.compile(r"bytes=(?P<start>\d+)-(?P<end>\d*)")


# ---------------------------------------------------------------------------
# Range parsing
# ---------------------------------------------------------------------------


def _parse_range(
    header: Optional[str], total_size: Optional[int]
) -> Optional[Tuple[int, Optional[int]]]:
    """
    Parse a Range header. Returns `(start, end_inclusive_or_None)` or None.

    For raw streaming `total_size` is the file's exact size; the returned
    `end` is always a real byte index. For transcoded streaming we may pass
    an *estimated* size (or None) — in that case `end` is whatever the client
    asked for (possibly None for "to the end").
    """
    if not header:
        return None
    m = _RANGE_RE.search(header)
    if not m:
        return None
    start = int(m.group("start"))
    end_str = m.group("end")
    if total_size is not None:
        end = int(end_str) if end_str else total_size - 1
        if start >= total_size or end < start:
            return None
        return start, min(end, total_size - 1)
    return start, (int(end_str) if end_str else None)


# ---------------------------------------------------------------------------
# Async file iteration (raw path)
# ---------------------------------------------------------------------------


async def _async_file_chunks(
    path: str, start: int, end: int, chunk_size: int
) -> AsyncIterator[bytes]:
    """
    Yield bytes `[start, end]` inclusive from `path` without blocking the loop.

    We use plain blocking file IO inside `asyncio.to_thread`. For NAS-backed
    paths a syscall can stall for tens of ms; running it on a worker keeps the
    event loop responsive for other requests.
    """
    remaining = end - start + 1
    f = await asyncio.to_thread(open, path, "rb")
    try:
        await asyncio.to_thread(f.seek, start)
        while remaining > 0:
            data = await asyncio.to_thread(f.read, min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        await asyncio.to_thread(f.close)


# ---------------------------------------------------------------------------
# Server-side policy
# ---------------------------------------------------------------------------


def _apply_policy(
    requested_format: Optional[str],
    requested_bitrate: Optional[int],
) -> Tuple[Optional[str], Optional[int]]:
    """
    Decide what (format, bitrate) we'll actually serve given the client ask.

    Returns `(None, None)` to mean "stream the original file" — raw is the
    only thing that ever bypasses the bitrate ceiling.
    """
    settings = get_settings()

    if not settings.transcoding_enabled:
        return None, None

    fmt = (requested_format or settings.default_transcode_format or "raw").lower()
    if fmt == "raw":
        return None, None

    cap = MAX_TRANSCODE_BITRATE
    if settings.max_streaming_bitrate is not None:
        cap = min(cap, settings.max_streaming_bitrate)

    if requested_bitrate is None:
        bitrate = min(settings.default_transcode_bitrate, cap)
    else:
        bitrate = min(requested_bitrate, cap)

    return fmt, bitrate


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def stream_track(
    request: Request,
    track_path: str,
    track_suffix: str,
    track_content_type: str,
    track_bitrate: Optional[int],
    track_duration: Optional[float] = None,
    requested_format: Optional[str] = None,
    requested_bitrate: Optional[int] = None,
    time_offset: Optional[float] = None,
) -> Response:
    """
    Build the HTTP response for a /stream (or /download) request.

    `track_duration` is the source length in seconds; we use it to synthesize
    valid Content-Range / Content-Length headers on seek-on-transcode requests.
    Without it, transcoded seek still works but the response is 200 chunked.
    """
    settings = get_settings()

    if not Path(track_path).is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    fmt, bitrate = _apply_policy(requested_format, requested_bitrate)
    preset = resolve_preset(
        requested_format=fmt,
        requested_bitrate=bitrate,
        source_format=track_suffix,
        source_bitrate=track_bitrate,
        default_bitrate=settings.default_transcode_bitrate,
    )

    if preset is not None:
        return await _build_transcoded_response(
            request=request,
            preset=preset,
            track_path=track_path,
            track_duration=track_duration,
            time_offset=time_offset,
            chunk_size=settings.stream_chunk_size,
        )

    return await _build_raw_response(
        request=request,
        track_path=track_path,
        track_content_type=track_content_type,
        chunk_size=settings.stream_chunk_size,
    )


# ---------------------------------------------------------------------------
# Raw path
# ---------------------------------------------------------------------------


async def _build_raw_response(
    request: Request,
    track_path: str,
    track_content_type: str,
    chunk_size: int,
) -> Response:
    file_size = await asyncio.to_thread(os.path.getsize, track_path)
    rng = _parse_range(request.headers.get("range"), file_size)

    common = {
        "Accept-Ranges": "bytes",
        "Content-Type": track_content_type,
        "Cache-Control": "no-cache",
    }

    if rng is None:
        return StreamingResponse(
            _async_file_chunks(track_path, 0, file_size - 1, chunk_size),
            status_code=200,
            media_type=track_content_type,
            headers={**common, "Content-Length": str(file_size)},
        )

    start, end = rng  # end is non-None here because we passed a real size
    assert end is not None
    return StreamingResponse(
        _async_file_chunks(track_path, start, end, chunk_size),
        status_code=206,
        media_type=track_content_type,
        headers={
            **common,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
        },
    )


# ---------------------------------------------------------------------------
# Transcoded path
# ---------------------------------------------------------------------------


async def _build_transcoded_response(
    request: Request,
    preset: TranscodePreset,
    track_path: str,
    track_duration: Optional[float],
    time_offset: Optional[float],
    chunk_size: int,
) -> Response:
    """
    Spawn ffmpeg, stream its stdout. Honour `time_offset` and HTTP Range as seek.
    """
    bytes_per_second = preset.bitrate * 1000 // 8  # kbps → bytes/s
    estimated_total: Optional[int] = (
        int(track_duration * bytes_per_second) if track_duration else None
    )

    rng = _parse_range(request.headers.get("range"), estimated_total)
    rng_start = rng[0] if rng else None
    rng_end = rng[1] if rng else None

    # Explicit timeOffset wins. Otherwise derive seek from the Range start byte.
    if time_offset is not None and time_offset > 0:
        seek_seconds = float(time_offset)
    elif rng_start:
        seek_seconds = rng_start / bytes_per_second
    else:
        seek_seconds = 0.0

    ts = TranscodeStream(
        track_path,
        preset,
        chunk_size=chunk_size,
        start_seconds=seek_seconds,
    )
    # Enter the context here so subprocess-spawn errors surface as HTTP errors
    # rather than mid-stream interruptions. The generator below handles exit.
    await ts.__aenter__()

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in ts.iter_chunks():
                yield chunk
        finally:
            await ts.__aexit__(None, None, None)

    common = {
        "Content-Type": preset.content_type,
        "Cache-Control": "no-cache",
    }

    # Synthesized 206 when the client asked for a seek and we know the duration.
    if rng_start is not None and estimated_total is not None:
        end = rng_end if rng_end is not None else estimated_total - 1
        length = max(0, end - rng_start + 1)
        return StreamingResponse(
            gen(),
            status_code=206,
            media_type=preset.content_type,
            headers={
                **common,
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {rng_start}-{end}/{estimated_total}",
                "Content-Length": str(length),
            },
        )

    # No seek, or no duration to synthesize ranges from → chunked.
    return StreamingResponse(
        gen(),
        status_code=200,
        media_type=preset.content_type,
        headers={
            **common,
            "Accept-Ranges": "bytes" if estimated_total else "none",
        },
    )
