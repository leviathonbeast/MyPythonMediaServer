"""
Transcoding quality presets.

Subsonic clients send `?format=...&maxBitRate=...`. We map that pair to one
of these presets. Adding a new format is just a tuple in PRESETS.

WHY presets vs free-form ffmpeg args:
    Free-form args from a client = arbitrary code execution, basically.
    Presets bound the surface area: clients can only ask for combinations we
    explicitly support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class TranscodePreset:
    """A single transcode target. Args go straight to ffmpeg's stdout output."""
    format: str          # "mp3", "opus", "ogg"
    bitrate: int         # kbps
    content_type: str    # MIME for the response
    suffix: str          # for Content-Disposition
    ffmpeg_args: List[str]  # appended after `-i pipe:0` and before `pipe:1`


# Each entry is (format, bitrate) -> preset.
PRESETS: Dict[Tuple[str, int], TranscodePreset] = {
    ("mp3", 320): TranscodePreset(
        format="mp3", bitrate=320, content_type="audio/mpeg", suffix="mp3",
        ffmpeg_args=["-vn", "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"],
    ),
    ("mp3", 192): TranscodePreset(
        format="mp3", bitrate=192, content_type="audio/mpeg", suffix="mp3",
        ffmpeg_args=["-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
    ),
    ("mp3", 128): TranscodePreset(
        format="mp3", bitrate=128, content_type="audio/mpeg", suffix="mp3",
        ffmpeg_args=["-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
    ),
    ("opus", 128): TranscodePreset(
        format="opus", bitrate=128, content_type="audio/ogg", suffix="opus",
        ffmpeg_args=["-vn", "-c:a", "libopus", "-b:a", "128k", "-f", "ogg"],
    ),
    ("opus", 96): TranscodePreset(
        format="opus", bitrate=96, content_type="audio/ogg", suffix="opus",
        ffmpeg_args=["-vn", "-c:a", "libopus", "-b:a", "96k", "-f", "ogg"],
    ),
    ("ogg", 192): TranscodePreset(
        format="ogg", bitrate=192, content_type="audio/ogg", suffix="ogg",
        ffmpeg_args=["-vn", "-c:a", "libvorbis", "-b:a", "192k", "-f", "ogg"],
    ),
}


def list_presets() -> List[Dict[str, object]]:
    """Return all configured presets as plain dicts.

    Used by /api/transcoding/policy so the frontend can populate the
    format/bitrate dropdowns from a single source of truth — adding a
    preset to PRESETS automatically makes it available in the UI.
    """
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

    Logic:
        * If client asked for `raw` or didn't ask, return None.
        * If client format equals source format AND requested bitrate >= source,
          return None — re-encoding to a worse codec is wasteful.
        * Else find the closest preset matching the requested format/bitrate.
    """
    if not requested_format or requested_format == "raw":
        return None
    requested_format = requested_format.lower()

    # No re-encode if formats already match and bitrate doesn't go down.
    if requested_format == source_format.lower():
        if requested_bitrate is None or (source_bitrate and requested_bitrate >= source_bitrate):
            return None

    bitrate = requested_bitrate or default_bitrate

    # Exact match on (format, bitrate)?
    key = (requested_format, bitrate)
    if key in PRESETS:
        return PRESETS[key]

    # Closest available bitrate for that format.
    matching = [(b, p) for (f, b), p in PRESETS.items() if f == requested_format]
    if not matching:
        return None
    matching.sort(key=lambda x: abs(x[0] - bitrate))
    return matching[0][1]
