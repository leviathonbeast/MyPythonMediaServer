"""
deezer artist bio fetcher.

A small wrapper around Last.fm's `artist.getInfo` REST endpoint, used by
the artist page to enrich the view with a short biography.

Design notes:


"""

from __future__ import annotations


import json
import logging

import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from backend.config import get_settings

log = logging.getLogger(__name__)

_DEEZER_BASE = "https://api.deezer.com/search/artist"
_DEEZER_ALBUM_BASE = "https://api.deezer.com/search/album"
_USER_AGENT = "Muse/0.1 (+https://github.com/your-org/muse)"

# How long a successful bio fetch is reused without re-querying Last.fm.
# 24h is a deliberate trade-off: bios are mostly static, and we'd rather
# under-fetch than risk hitting rate limits during a busy library browse.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Negative cache: shorter TTL, so a transient API blip doesn't blank the
# bio for a full day.
_NEGATIVE_TTL_SECONDS = 30 * 60

# key -> (expires_at, value)
_cache: dict[str, tuple[float, Optional[dict]]] = {}

_album_cache: dict[str, tuple[float, Optional[dict]]] = {}


def get_album_images(name: str, album: str) -> Optional[dict]:
    key = f"{name.strip().lower()}|{album.strip().lower()}"
    q = f'artist:"{name}" album:"{album}"'
    url = f"{_DEEZER_ALBUM_BASE}?q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    # cache hit
    now = time.time()
    cached = _album_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
    except Exception as e:
        log.debug("deezer: fetch failed for %r: %s", name, e)
        # negative cache
        _album_cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    if not isinstance(payload, dict) or "data" not in payload:
        _album_cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    data = payload["data"]
    if not data:
        _album_cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    first = payload["data"][0]
    images = {
        "cover_small": first.get("cover_small"),
        "cover_medium": first.get("cover_medium"),
        "cover_big": first.get("cover_big"),
        "cover_xl": first.get("cover_xl"),
    }
    _album_cache[key] = (now + _CACHE_TTL_SECONDS, images)
    return images


def get_artist_images(name: str) -> Optional[dict]:
    key = name.strip().lower()
    url = f"{_DEEZER_BASE}?q={urllib.parse.quote(name)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    # cache hit
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
    except Exception as e:
        log.debug("deezer: fetch failed for %r: %s", name, e)
        # negative cache
        _cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    if not isinstance(payload, dict) or "data" not in payload:
        _cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    data = payload["data"]
    if not data:
        _cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    # Pick artist with highest fan count
    artist = max(data, key=lambda a: a.get("nb_fan", 0))

    images = {
        "id": artist.get("id"),
        "name": artist.get("name"),
        "picture": artist.get("picture"),
        "picture_small": artist.get("picture_small"),
        "picture_medium": artist.get("picture_medium"),
        "picture_big": artist.get("picture_big"),
        "picture_xl": artist.get("picture_xl"),
    }

    # positive cache
    _cache[key] = (now + _CACHE_TTL_SECONDS, images)

    return images


# ---------------------------------------------------------------------------
# Album cover lookup (used by artwork recovery as the third-tier fallback
# behind embedded art and folder.jpg).
# ---------------------------------------------------------------------------

# Separate cache namespace so positive/negative entries for artist queries
# don't collide with album queries.


def get_album_cover_url(artist_name: str, album_name: str) -> Optional[str]:
    """
    Return the Deezer URL of an album cover (xl size, 1000×1000), or None.

    Query format `artist:"X" album:"Y"` narrows the search a lot more
    reliably than a free-text "X Y" query — Deezer's search treats the
    quoted fields as exact, so re-releases / EPs with overlapping names
    don't poison the top hit. We still take the first result rather than
    scoring, because Deezer already orders by relevance.
    """
    key = f"{artist_name.strip().lower()}|{album_name.strip().lower()}"
    now = time.time()
    cached = _album_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    q = f'artist:"{artist_name}" album:"{album_name}"'
    url = f"{_DEEZER_ALBUM_BASE}?q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
    except Exception as e:
        log.debug(
            "deezer: album fetch failed for %r / %r: %s", artist_name, album_name, e
        )
        _album_cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    if not isinstance(payload, dict) or "data" not in payload or not payload["data"]:
        _album_cache[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        return None

    first = payload["data"][0]
    cover_url = first.get("cover_xl") or first.get("cover_big") or first.get("cover")
    _album_cache[key] = (
        now + (_CACHE_TTL_SECONDS if cover_url else _NEGATIVE_TTL_SECONDS),
        cover_url,
    )
    return cover_url


def fetch_artist_image_bytes(name: str) -> Optional[bytes]:
    """Resolve the Deezer artist photo URL (xl) and download the image bytes.

    Returns None when the lookup fails or the download errors out. Caller
    feeds the bytes into `artwork.store_artwork()` so the photo lives in
    the same on-disk cache as album covers — one storage, one cache header
    policy, one GC sweep handles both.
    """
    if not name:
        return None
    images = get_artist_images(name)
    if not images:
        return None
    cover_url = (
        images.get("picture_xl")
        or images.get("picture_big")
        or images.get("picture_medium")
    )
    if not cover_url:
        return None
    req = urllib.request.Request(cover_url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except Exception as e:
        log.debug("deezer: artist image download failed for %r: %s", name, e)
        return None
    return data or None


def fetch_album_cover_bytes(artist_name: str, album_name: str) -> Optional[bytes]:
    """Resolve the Deezer album cover URL and download the image bytes.

    Returns None when the lookup fails or the download errors out. Caller
    feeds the bytes into artwork.store_artwork() exactly like embedded or
    folder art.
    """
    if not artist_name or not album_name:
        return None
    cover_url = get_album_cover_url(artist_name, album_name)
    if not cover_url:
        return None
    req = urllib.request.Request(cover_url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except Exception as e:
        log.debug(
            "deezer: cover download failed for %r/%r: %s", artist_name, album_name, e
        )
        return None
    # Defensive: empty/HTML body would still produce a valid response.
    return data or None
