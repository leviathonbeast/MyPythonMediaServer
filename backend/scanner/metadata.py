"""
Metadata extraction.

Layered fallback strategy:
    1. mutagen — fast, format-aware tag reader (ID3, Vorbis comments, MP4, etc).
    2. ffprobe — JSON output, much slower but handles weirder formats.
    3. Filename heuristics — last resort. "Artist - Album - 03 - Title.mp3"
       gives us something usable when there are no tags at all.

WHY layered:
    Real-world libraries have files with no tags, broken tags, or formats
    mutagen doesn't fully understand. Fall through gracefully — a track with
    a guessed artist beats no track at all.

Returned shape is the same regardless of source so the scanner doesn't care.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from backend.config import get_settings


@dataclass
class TrackMetadata:
    """Normalised metadata extracted from an audio file."""
    title: str = "Unknown"
    artist: str = "Unknown Artist"
    album_artist: Optional[str] = None
    album: str = "Unknown Album"
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    duration: Optional[int] = None     # seconds
    bitrate: Optional[int] = None       # kbps
    # Picard / MusicBrainz primary release type — "album", "ep", "single",
    # "compilation", "live", "remix", "soundtrack", etc. We normalise to
    # lowercase and don't validate further; the artist page maps known
    # values into display groups and falls back to "album" for the rest.
    release_type: Optional[str] = None
    has_embedded_art: bool = False
    art_data: Optional[bytes] = field(default=None, repr=False)


# Map file extensions to MIME types. Subsonic clients need contentType.
SUFFIX_TO_MIME = {
    "mp3":  "audio/mpeg",
    "flac": "audio/flac",
    "ogg":  "audio/ogg",
    "opus": "audio/opus",
    "m4a":  "audio/mp4",
    "wav":  "audio/wav",
    "aac":  "audio/aac",
    "wma":  "audio/x-ms-wma",
}


def extract(path: str) -> TrackMetadata:
    """
    Extract metadata for a single file. Always returns a TrackMetadata; never raises.

    Order of attempts: mutagen → ffprobe → filename. Each fills missing fields
    rather than overwriting good data from a higher-priority source.
    """
    meta = TrackMetadata()
    _try_mutagen(path, meta)
    if meta.duration is None or meta.title == "Unknown":
        _try_ffprobe(path, meta)
    if meta.title == "Unknown":
        _try_filename(path, meta)
    return meta


# ---------------------------------------------------------------------------
# Mutagen
# ---------------------------------------------------------------------------

def _try_mutagen(path: str, meta: TrackMetadata) -> None:
    """
    Pull tags via mutagen. Different formats expose tags through different
    APIs; we use the high-level easy=True interface where possible and fall
    through for art/duration which need format-specific handling.
    """
    try:
        # easy=True gives us a uniform dict of common tags.
        mf = MutagenFile(path, easy=True)
    except (ID3NoHeaderError, Exception):
        return
    if mf is None:
        return

    def first(key: str) -> Optional[str]:
        v = mf.get(key)
        if not v:
            return None
        if isinstance(v, list):
            return str(v[0]) if v else None
        return str(v)

    title = first("title")
    artist = first("artist")
    album_artist = first("albumartist")
    album = first("album")
    genre = first("genre")
    track_no = first("tracknumber")
    disc_no = first("discnumber")
    date = first("date") or first("year")
    # FLAC/Vorbis exposes this as 'releasetype' through the easy interface.
    # ID3 and MP4 don't, so we also probe the raw tags below.
    release_type = first("releasetype") or first("musicbrainz_albumtype")

    if title:        meta.title = title
    if artist:       meta.artist = artist
    if album_artist: meta.album_artist = album_artist
    if album:        meta.album = album
    if genre:        meta.genre = genre
    if track_no:     meta.track_number = _parse_int_pair(track_no)
    if disc_no:      meta.disc_number = _parse_int_pair(disc_no)
    if date:         meta.year = _parse_year(date)
    if release_type: meta.release_type = _normalise_release_type(release_type)

    # Reload without easy=True to get streaminfo + embedded art.
    try:
        mf_full = MutagenFile(path)
        if mf_full is not None:
            info = getattr(mf_full, "info", None)
            if info is not None:
                if hasattr(info, "length") and info.length:
                    meta.duration = int(info.length)
                if hasattr(info, "bitrate") and info.bitrate:
                    meta.bitrate = int(info.bitrate / 1000)

            # Embedded art lives in different places per format. Cover the
            # common ones; bail on anything weird.
            art = _extract_embedded_art(mf_full)
            if art is not None:
                meta.has_embedded_art = True
                meta.art_data = art

            # Release type via raw tags — covers ID3 TXXX and MP4 ----.
            if meta.release_type is None:
                meta.release_type = _extract_release_type(mf_full)
    except Exception:
        pass


def _normalise_release_type(value: str) -> Optional[str]:
    """Picard sometimes writes multi-valued types like "Album; Live"; we keep
    the first token and lowercase it. Returns None for empty input.
    """
    if not value:
        return None
    head = value.split(";")[0].split("/")[0].strip().lower()
    return head or None


def _extract_release_type(mf) -> Optional[str]:
    """Read the MusicBrainz album type from format-native tag locations.

    Mutagen's `easy=True` interface only exposes a small set of keys.
    The release type lives in different places depending on container:
      - FLAC/Vorbis: a regular 'releasetype' comment (already handled above).
      - ID3 (MP3):   a TXXX user-defined frame, typically described as
                     "MusicBrainz Album Type" or "RELEASETYPE".
      - MP4 (m4a):   a freeform "----:com.apple.iTunes:MusicBrainz Album Type"
                     atom, holding bytes that need decoding.
    """
    tags = getattr(mf, "tags", None)
    if tags is None:
        return None

    # ID3: walk every TXXX frame, match by description (case-insensitive).
    if hasattr(tags, "getall"):
        for frame in tags.getall("TXXX") or []:
            desc = (getattr(frame, "desc", "") or "").lower()
            if "musicbrainz album type" in desc or desc == "releasetype":
                vals = list(getattr(frame, "text", []) or [])
                if vals:
                    return _normalise_release_type(str(vals[0]))

    # MP4 freeform atoms — keys look like "----:com.apple.iTunes:<NAME>".
    if hasattr(tags, "items"):
        for key, val in tags.items():
            if not isinstance(key, str):
                continue
            if key.startswith("----:") and "MusicBrainz Album Type" in key:
                try:
                    raw = val[0]
                    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                    return _normalise_release_type(text)
                except Exception:
                    continue
    return None


def _extract_embedded_art(mf) -> Optional[bytes]:
    """Return embedded artwork bytes, or None."""
    # ID3 (MP3): APIC frames
    tags = getattr(mf, "tags", None)
    if tags is not None:
        # MP3
        if hasattr(tags, "getall"):
            for frame in tags.getall("APIC") or []:
                if getattr(frame, "data", None):
                    return frame.data
        # MP4 (m4a): 'covr' atom
        covr = tags.get("covr") if hasattr(tags, "get") else None
        if covr:
            try:
                return bytes(covr[0])
            except Exception:
                pass
    # FLAC: pictures attribute
    pictures = getattr(mf, "pictures", None)
    if pictures:
        try:
            return pictures[0].data
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# ffprobe fallback
# ---------------------------------------------------------------------------

def _try_ffprobe(path: str, meta: TrackMetadata) -> None:
    """
    Use ffprobe to fill missing fields.

    Slow (subprocess per file) so we only call it when mutagen failed. ffprobe
    handles obscure formats (WavPack, MPC, dsf, ...) that mutagen may not.
    """
    settings = get_settings()
    try:
        out = subprocess.run(
            [
                settings.ffprobe_binary,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return
        data = json.loads(out.stdout or b"{}")
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return

    fmt = data.get("format", {}) or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    if meta.title == "Unknown" and tags.get("title"):
        meta.title = tags["title"]
    if meta.artist == "Unknown Artist" and tags.get("artist"):
        meta.artist = tags["artist"]
    if meta.album == "Unknown Album" and tags.get("album"):
        meta.album = tags["album"]
    if meta.genre is None and tags.get("genre"):
        meta.genre = tags["genre"]
    if meta.year is None and (tags.get("date") or tags.get("year")):
        meta.year = _parse_year(tags.get("date") or tags.get("year"))

    if meta.duration is None:
        try:
            meta.duration = int(float(fmt.get("duration") or 0)) or None
        except (TypeError, ValueError):
            pass
    if meta.bitrate is None:
        try:
            meta.bitrate = int(int(fmt.get("bit_rate") or 0) / 1000) or None
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Filename heuristic
# ---------------------------------------------------------------------------

# Match patterns like "01 - Title", "01. Title", "1-01 - Title".
_FILENAME_RE = re.compile(
    r"^(?:(?P<disc>\d+)[-_])?(?P<track>\d{1,3})\s*[-_.\s]+\s*(?P<title>.+)$"
)


def _try_filename(path: str, meta: TrackMetadata) -> None:
    """
    Last-ditch parsing of the path. Layout we look for:
        .../Artist Name/Album Name/01 - Track Title.mp3
        .../Artist - Album - 01 - Track Title.mp3
    """
    p = Path(path)
    stem = p.stem

    m = _FILENAME_RE.match(stem)
    if m:
        if meta.title == "Unknown":
            meta.title = m.group("title").strip()
        if meta.track_number is None:
            try:
                meta.track_number = int(m.group("track"))
            except (TypeError, ValueError):
                pass
        if meta.disc_number is None and m.group("disc"):
            try:
                meta.disc_number = int(m.group("disc"))
            except (TypeError, ValueError):
                pass
    else:
        if meta.title == "Unknown":
            meta.title = stem

    # Artist/Album from parent directories: .../Artist/Album/track.ext
    parents = list(p.parents)
    if len(parents) >= 2:
        if meta.album == "Unknown Album":
            meta.album = parents[0].name or "Unknown Album"
        if meta.artist == "Unknown Artist":
            meta.artist = parents[1].name or "Unknown Artist"


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _parse_int_pair(value: str) -> Optional[int]:
    """'3/12' -> 3. '3' -> 3. Garbage -> None."""
    if value is None:
        return None
    head = value.split("/")[0].strip()
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


_YEAR_RE = re.compile(r"(\d{4})")


def _parse_year(value: str) -> Optional[int]:
    """Pull the first 4-digit number out of a date-ish string."""
    if value is None:
        return None
    m = _YEAR_RE.search(str(value))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def get_content_type(suffix: str) -> str:
    """Return MIME type for a suffix (without dot). Defaults to octet-stream."""
    return SUFFIX_TO_MIME.get(suffix.lower().lstrip("."), "application/octet-stream")
