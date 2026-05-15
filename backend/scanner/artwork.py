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
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional, Tuple

from backend.config import get_settings


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
