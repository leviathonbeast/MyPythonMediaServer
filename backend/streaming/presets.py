"""
Transcoding presets.

Three target codecs — MP3, AAC and Opus — at three bitrates each
(128 / 192 / 320 kbps). 320 kbps is the hard ceiling for transcoded streams:
above that the perceptual quality gain is negligible but the CPU cost isn't.
Clients that want lossless or hi-bit-depth audio should ask for `raw`, which
bypasses bitrate policy entirely.

WHY presets vs free-form ffmpeg args:
    Free-form args from a client = arbitrary code execution. Presets bound the
    surface area so clients can only ask for combinations we explicitly support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Hard ceiling on transcoded bitrate. Raw streams bypass this — see streamer.py.
MAX_TRANSCODE_BITRATE = 320

# Standard bitrate rungs we expose. Adding a new value here also makes the UI
# pick it up via /api/transcoding/policy.
_BITRATES = (128, 192, 256, 320)


@dataclass(frozen=True)
class TranscodePreset:
    """A single transcode target. Args go straight to ffmpeg after `-i`."""

    format: str  # "mp3", "aac", "opus"
    bitrate: int  # kbps
    content_type: str  # MIME for the response
    suffix: str  # for Content-Disposition
    ffmpeg_args: List[str]  # appended after `-i <path>` and before `pipe:1`


def _mp3(br: int) -> TranscodePreset:
    return TranscodePreset(
        format="mp3",
        bitrate=br,
        content_type="audio/mpeg",
        suffix="mp3",
        ffmpeg_args=["-vn", "-c:a", "libmp3lame", "-b:a", f"{br}k", "-f", "mp3"],
    )


def _aac(br: int) -> TranscodePreset:
    # ADTS container so the output stream needs no remuxing on the client.
    # We use the built-in `aac` encoder; libfdk_aac would be higher quality but
    # is non-free and not shipped with most ffmpeg builds.
    return TranscodePreset(
        format="aac",
        bitrate=br,
        content_type="audio/aac",
        suffix="aac",
        ffmpeg_args=["-vn", "-c:a", "aac", "-b:a", f"{br}k", "-f", "adts"],
    )


def _opus(br: int) -> TranscodePreset:
    return TranscodePreset(
        format="opus",
        bitrate=br,
        content_type="audio/ogg",
        suffix="opus",
        ffmpeg_args=["-vn", "-c:a", "libopus", "-b:a", f"{br}k", "-f", "opus"],
    )


PRESETS: Dict[Tuple[str, int], TranscodePreset] = {
    **{("mp3", br): _mp3(br) for br in _BITRATES},
    **{("aac", br): _aac(br) for br in _BITRATES},
    **{("opus", br): _opus(br) for br in _BITRATES},
}


def list_presets() -> List[Dict[str, object]]:
    """All configured presets — used by /api/transcoding/policy to populate the UI."""
    return [
        {"format": fmt, "bitrate": br, "content_type": p.content_type}
        for (fmt, br), p in PRESETS.items()
    ]


def resolve_preset(
    requested_format: Optional[str],
    requested_bitrate: Optional[int],
    source_format: str,
    source_bitrate: Optional[int],
    default_bitrate: int = 192,
) -> Optional[TranscodePreset]:
    """
    Pick a preset, or None for "stream original file".

    Skip re-encoding when:
        * No format requested, or `raw`.
        * Requested format equals source format AND the requested bitrate would
          not actually shrink the source — re-encoding to a worse copy is waste.
    """
    if not requested_format or requested_format == "raw":
        return None
    requested_format = requested_format.lower()

    if requested_format == source_format.lower():
        if requested_bitrate is None or (
            source_bitrate and requested_bitrate >= source_bitrate
        ):
            return None

    bitrate = min(requested_bitrate or default_bitrate, MAX_TRANSCODE_BITRATE)

    key = (requested_format, bitrate)
    if key in PRESETS:
        return PRESETS[key]

    # Snap to the closest rung for this format.
    matching = [(b, p) for (f, b), p in PRESETS.items() if f == requested_format]
    if not matching:
        return None
    matching.sort(key=lambda x: abs(x[0] - bitrate))
    return matching[0][1]
