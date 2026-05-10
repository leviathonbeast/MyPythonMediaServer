"""
HTTP range-aware audio streamer.

Browsers (and many native clients) seek by sending `Range: bytes=N-` and
expect HTTP 206 Partial Content. Without this, dragging the seek bar in
the audio player won't work; the browser will be forced to re-download
from the start.

For transcoded streams we deliberately don't support Range — we'd have to
re-run ffmpeg from a seek offset which is doable but adds complexity. Most
clients fall back to streaming-from-start when Range isn't honoured.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, Response

from backend.config import get_settings
from .presets import resolve_preset, TranscodePreset
from .transcoder import TranscodeStream

# Range: bytes=START-END  (END optional, START required for our purposes)
_RANGE_RE = re.compile(r"bytes=(?P<start>\d+)-(?P<end>\d*)")


def _parse_range(header: Optional[str], file_size: int) -> Optional[Tuple[int, int]]:
    """
    Parse a Range header. Returns (start, end_inclusive) or None if absent/malformed.

    We handle the common cases and ignore multipart ranges (almost no client
    uses those for audio).
    """
    if not header:
        return None
    m = _RANGE_RE.search(header)
    if not m:
        return None
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else file_size - 1
    if start >= file_size or end < start:
        return None
    end = min(end, file_size - 1)
    return start, end


def _file_chunks(path: str, start: int, end: int, chunk_size: int) -> Iterator[bytes]:
    """
    Yield bytes [start, end] (inclusive) from a file in chunk_size pieces.

    We use plain file IO. For NAS-backed paths this is async-friendly because
    FastAPI runs streaming responses in a thread pool by default — the read()
    blocks the worker, not the event loop.
    """
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def stream_track(
    request: Request,
    track_path: str,
    track_suffix: str,
    track_content_type: str,
    track_bitrate: Optional[int],
    requested_format: Optional[str] = None,
    requested_bitrate: Optional[int] = None,
) -> Response:
    """
    Build the HTTP response for a /stream request.

    Returns a 206 with byte ranges for raw streaming, or a 200 chunked
    response when transcoding (no length, no seek — a deliberate trade-off).
    """
    settings = get_settings()

    if not Path(track_path).is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # ---- Server-side policy -------------------------------------------
    # Three knobs the operator controls (config.yaml or env vars):
    #   1. transcoding_enabled  — global kill-switch
    #   2. default_transcode_format — what to send if the client didn't ask
    #   3. max_streaming_bitrate — hard cap on the bitrate we'll serve
    #
    # The order matters. We apply defaults first, then enforce the cap.
    if not settings.transcoding_enabled:
        # Strict raw mode. The client's requests are politely ignored.
        requested_format = None
        requested_bitrate = None
    else:
        # Fill in the default if the client didn't ask. "raw" is the
        # special value resolve_preset() interprets as "no transcoding".
        if not requested_format:
            requested_format = settings.default_transcode_format

        # Cap the requested bitrate. If the client asked for none, the
        # cap still pulls them down to it when the source exceeds it
        # (handled below by injecting it as the request).
        cap = settings.max_streaming_bitrate
        if cap is not None:
            if requested_bitrate is None or requested_bitrate > cap:
                requested_bitrate = cap
            # If the source itself is over the cap and the client wanted
            # raw, we can't give them raw — force a re-encode to mp3@cap.
            if (
                (requested_format in (None, "raw"))
                and track_bitrate
                and track_bitrate > cap
            ):
                requested_format = "mp3"

    preset: Optional[TranscodePreset] = resolve_preset(
        requested_format=requested_format,
        requested_bitrate=requested_bitrate,
        source_format=track_suffix,
        source_bitrate=track_bitrate,
        default_bitrate=settings.default_transcode_bitrate,
    )

    # ---- Transcoding path ------------------------------------------------
    if preset is not None:
        # No length, no range — chunked. Most clients accept this.
        ts = TranscodeStream(track_path, preset, chunk_size=settings.stream_chunk_size)
        # We open the context here and close it inside the generator. FastAPI
        # consumes the generator; if the client disconnects, StarletteResponse
        # raises GeneratorExit which our `finally` in TranscodeStream catches.
        ts.__enter__()

        def gen():
            try:
                yield from ts.iter_chunks()
            finally:
                ts.__exit__(None, None, None)

        return StreamingResponse(
            gen(),
            media_type=preset.content_type,
            headers={"Accept-Ranges": "none", "Cache-Control": "no-cache"},
        )

    # ---- Raw / range path ------------------------------------------------
    file_size = os.path.getsize(track_path)
    rng = _parse_range(request.headers.get("range"), file_size)

    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type":  track_content_type,
        "Cache-Control": "no-cache",
    }

    if rng is None:
        # Full file. Still chunked, but with content-length so progress bars
        # and "download" actions in browsers know the size.
        return StreamingResponse(
            _file_chunks(track_path, 0, file_size - 1, settings.stream_chunk_size),
            status_code=200,
            media_type=track_content_type,
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        _file_chunks(track_path, start, end, settings.stream_chunk_size),
        status_code=206,
        media_type=track_content_type,
        headers={
            **common_headers,
            "Content-Length": str(length),
            "Content-Range":  f"bytes {start}-{end}/{file_size}",
        },
    )
