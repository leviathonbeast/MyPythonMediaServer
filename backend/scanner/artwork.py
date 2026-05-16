"""
Album art extraction and on-disk cache.

The flow:
    Scanner reads embedded art via mutagen → calls store_artwork(bytes) →
    we hash the bytes, write to artwork_cache/<hash>.<ext>, return the hash.
    The album row stores that hash in cover_art_id.

WHY hash-based filenames:
    * Identical artwork across albums (compilations, re-releases) deduplicates
      automatically.
    * Hash is a stable id we can use in the Subsonic getCoverArt URL.
    * Easy to garbage collect later — files unreferenced by any album row are
      removable.

Resized variants are written next to the source as `<hash>_<size>.jpg` and
share the same lifecycle: the GC sweep in db/maintenance.py strips a trailing
"_<digits>" before comparing against `albums.cover_art_id`, so resized
thumbnails are removed in lock-step with their source.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

from backend.config import get_settings

log = logging.getLogger(__name__)


def _detect_image_format(data: bytes) -> Tuple[str, str]:
    """
    Return (extension, mime_type) by sniffing magic bytes.

    Just JPEG / PNG / WebP — covers ~all embedded artwork in the wild.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    # Default to JPEG — probably wrong but Subsonic clients are lenient.
    return "jpg", "image/jpeg"


def store_artwork(data: bytes) -> Optional[str]:
    """
    Persist artwork bytes. Returns the cover_art_id (hash), or None if data is empty.
    Idempotent — calling twice with the same bytes is a no-op.
    """
    if not data:
        return None
    settings = get_settings()
    h = hashlib.sha1(data).hexdigest()[:16]
    ext, _mime = _detect_image_format(data)
    cache_dir = Path(settings.artwork_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{h}.{ext}"
    if not out.exists():
        out.write_bytes(data)
    return h


_ARTWORK_ID_RE = re.compile(r"[0-9a-f]{16}")


def find_artwork_path(cover_art_id: str) -> Optional[Path]:
    """Locate a stored artwork file by its hash. Returns the path or None."""
    # Reject anything that isn't a 16-char hex hash. Without this an attacker
    # could pass id="../../etc/passwd" and read any file with a .jpg/.png/.webp
    # extension that resolves outside the artwork cache dir.
    if not cover_art_id or not _ARTWORK_ID_RE.fullmatch(cover_art_id):
        return None
    cache_dir = Path(get_settings().artwork_cache_dir)
    # Try common extensions in order of likelihood.
    for ext in ("jpg", "png", "webp"):
        p = cache_dir / f"{cover_art_id}.{ext}"
        if p.exists():
            return p
    return None


# Subsonic clients in the wild request sizes like 32/64/128/300/600. Cap at
# 1024 so a malicious or buggy client passing size=10000 doesn't burn CPU
# re-encoding at full resolution for no visual benefit.
_RESIZE_MAX_DIMENSION = 1024

# Pillow is imported lazily inside resize_cached so importing the artwork
# module doesn't require Pillow at the scanner layer (where it isn't used).


def resize_cached(cover_art_id: str, size: int) -> Optional[Path]:
    """Return a path to a `size`-pixel variant, creating it on first request.

    `size` is interpreted Subsonic-style: the longest side of the output is
    at most `size` pixels; aspect ratio is preserved. Output is always JPEG
    because the endpoint always serves image/jpeg for resized variants and
    JPEG is a smaller-on-the-wire choice for thumbnails than re-encoded PNG.

    Returns:
        * Path to a cached resized file, OR
        * The source path unchanged when `size` would upscale (no point
          re-encoding to make it look the same or worse), OR
        * None when the source isn't in the cache or the request is invalid.

    Concurrency: two requests for the same (id, size) racing through this
    function both end up renaming the same content into place. The second
    rename clobbers the first harmlessly because the resized output is
    deterministic from (source bytes, size).
    """
    if size <= 0:
        return None
    size = min(size, _RESIZE_MAX_DIMENSION)

    src = find_artwork_path(cover_art_id)
    if src is None:
        return None

    cache_dir = Path(get_settings().artwork_cache_dir)
    out = cache_dir / f"{cover_art_id}_{size}.jpg"
    if out.exists():
        return out

    # Imported here so module import stays cheap and a missing Pillow only
    # bites at the moment a resize is actually attempted.
    from PIL import Image, ImageOps

    try:
        with Image.open(src) as im:
            # Don't upscale — Subsonic clients ask for "at most N pixels", and
            # blowing a 200px source up to 600px just wastes bytes.
            if max(im.size) <= size:
                return src
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            # Write to a temp file then rename so a crash mid-encode never
            # leaves a half-written variant in the cache.
            tmp = out.with_suffix(".tmp")
            im.save(tmp, format="JPEG", quality=85, optimize=True)
            tmp.replace(out)
    except Exception as e:
        log.warning("resize_cached(%s, %d) failed: %s", cover_art_id, size, e)
        return src

    return out


def find_folder_artwork(track_path: str) -> Optional[bytes]:
    """
    Look for cover.jpg / folder.jpg / front.jpg next to the audio file.

    Lots of libraries store artwork as a separate file rather than embedded.
    The scanner falls back to this when the audio file has no embedded art.
    """
    folder = Path(track_path).parent
    candidates = [
        "cover.jpg", "cover.jpeg", "cover.png",
        "folder.jpg", "folder.jpeg", "folder.png",
        "front.jpg", "front.jpeg", "front.png",
        "album.jpg", "albumart.jpg",
    ]
    try:
        # Read directory once; build a lowercase-name → entry map so candidate
        # lookups are O(1) instead of re-scanning the directory for each name.
        entries = {e.name.lower(): e for e in folder.iterdir() if e.is_file()}
    except OSError:
        return None
    for name in candidates:
        entry = entries.get(name)
        if entry is not None:
            try:
                return entry.read_bytes()
            except OSError:
                return None
    return None
