"""
Last.fm artist bio fetcher.

A small wrapper around Last.fm's `artist.getInfo` REST endpoint, used by
the artist page to enrich the view with a short biography.

Design notes:

  * Optional. If `lastfm_api_key` isn't configured, every call returns
    None — the artist page handles a missing bio gracefully.
  * Plain text only. Last.fm returns HTML in the `bio.summary` /
    `bio.content` fields, including `<a>` tags that link onward.
    Rendering arbitrary third-party HTML inside our app is a security
    smell (XSS, tracking pixels, mixed content); we strip to plain text
    server-side and let the frontend decide how to format it.
  * In-memory TTL cache. Last.fm rate-limits to ~5 req/s per key and
    bios change rarely, so caching artist→bio for a day is a sensible
    default. The cache lives in-process; restarts re-fetch.
  * Stdlib only. We use urllib instead of requests/httpx so this module
    doesn't add a runtime dependency on top of what FastAPI already pulls.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from backend.config import get_settings

log = logging.getLogger(__name__)

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_USER_AGENT = "Muse/0.1 (+https://github.com/your-org/muse)"

LASTFM_API_ROOT = "https://ws.audioscrobbler.com/2.0/"
LASTFM_AUTH_URL = "https://www.last.fm/api/auth/"

# How long a successful bio fetch is reused without re-querying Last.fm.
# 24h is a deliberate trade-off: bios are mostly static, and we'd rather
# under-fetch than risk hitting rate limits during a busy library browse.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Negative cache: shorter TTL, so a transient API blip doesn't blank the
# bio for a full day.
_NEGATIVE_TTL_SECONDS = 30 * 60


@dataclass
class ArtistBio:
    summary: str  # short paragraph, plain text
    content: str  # longer body, plain text (may equal summary)
    url: Optional[str]  # link back to Last.fm
    image_url: Optional[str]  # large artist image on Last.fm
    tags: list[str]  # genre/mood tags from Last.fm

    def as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "content": self.content,
            "url": self.url,
            "image_url": self.image_url,
            "tags": self.tags,
        }


# (artist_name_lower) -> (expires_at, ArtistBio | None)
_cache: dict[str, tuple[float, Optional[ArtistBio]]] = {}


def get_artist_bio(name: str) -> Optional[ArtistBio]:
    """Fetch (or return from cache) the Last.fm bio for an artist.

    Returns None when:
      - no Last.fm API key is configured,
      - the artist isn't found on Last.fm,
      - the API call fails for any reason (network, parse, rate limit).

    Never raises — a flaky external service should not break the artist page.
    """
    settings = get_settings()
    api_key = settings.lastfm_api_key
    if not api_key:
        return None

    key = (name or "").strip().lower()
    if not key:
        return None

    now = time.time()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    bio = _fetch(api_key, name)

    ttl = _CACHE_TTL_SECONDS if bio is not None else _NEGATIVE_TTL_SECONDS
    _cache[key] = (now + ttl, bio)
    return bio


def _fetch(api_key: str, name: str) -> Optional[ArtistBio]:
    params = {
        "method": "artist.getinfo",
        "artist": name,
        "api_key": api_key,
        "format": "json",
        "autocorrect": "1",
    }
    url = f"{_LASTFM_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
    except Exception as e:
        log.debug("lastfm: fetch failed for %r: %s", name, e)
        return None

    if not isinstance(payload, dict) or "artist" not in payload:
        return None

    artist = payload["artist"] or {}
    bio_blob = artist.get("bio") or {}
    summary_html = bio_blob.get("summary") or ""
    content_html = bio_blob.get("content") or summary_html

    summary = _html_to_text(summary_html)
    content = _html_to_text(content_html)

    if not summary and not content:
        return None

    # Last.fm's image array is mostly empty/identical placeholder URLs
    # these days. We pick the largest size with a non-empty `#text` and
    # filter out their well-known placeholder.
    image_url: Optional[str] = None
    for image in artist.get("image") or []:
        candidate = (image or {}).get("#text") or ""
        if candidate and "2a96cbd8b46e442fc41c2b86b821562f" not in candidate:
            image_url = candidate

    tags: list[str] = []
    for tag in (artist.get("tags") or {}).get("tag") or []:
        tag_name = (tag or {}).get("name")
        if tag_name:
            tags.append(tag_name)

    return ArtistBio(
        summary=summary,
        content=content,
        url=artist.get("url") or None,
        image_url=image_url,
        tags=tags[:6],
    )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(s: str) -> str:
    """Strip HTML tags and collapse whitespace.

    Last.fm bios are short paragraphs of HTML, often ending with a
    'Read more on Last.fm' anchor we deliberately discard. This is
    not a general-purpose HTML-to-text — it's tuned for that input.
    """
    if not s:
        return ""
    # Drop the "Read more on Last.fm" trailing link Last.fm always appends.
    s = re.sub(
        r"<a [^>]*last\.fm[^>]*>.*?</a>\.?\s*$", "", s, flags=re.IGNORECASE | re.DOTALL
    )
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# LAST FM SIGNING HELPER
def _sign(params: dict[str, str], secret: str) -> str:
    """compute the last.fm api_sig: md5 of (sorted key+value pairs concat, then secret).
    Per https://www.last.fm/api/authspec - exclude 'format' and 'callback' from the signature, but include every other param exactly as sent.
    """
    parts = [
        f"{k}{v}" for k, v in sorted(params.items()) if k not in ("format", "callback")
    ]

    return hashlib.md5(("".join(parts) + secret).encode("utf-8")).hexdigest()


# LAST FM REQUEST TEMPORARY TOKEN CONSUMED BY getSession


def get_auth_token() -> str:
    """Request a one of token"""
    settings = get_settings()
    if not settings.lastfm_api_key or not settings.lastfm_api_secret:
        raise RuntimeError(
            "last.fm api_key + api_secret must be configured in settings"
        )
    params = {
        "method": "auth.getToken",
        "api_key": settings.lastfm_api_key,
    }
    params["api_sig"] = _sign(params, settings.lastfm_api_secret)
    params["format"] = "json"
    # Use whatever HTTP client the rest of this file already uses.
    # Return data["token"] — raise on missing.
    # https://ws.audioscrobbler.com/2.0/method=auth.getToken&api_key=...
    url = LASTFM_API_ROOT + "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to contact Last.fm API: {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response from Last.fm: {body}") from exc

    if "error" in data:
        raise RuntimeError(
            f"Last.fm API error {data.get('error')}: {data.get('message')}"
        )

    token = data.get("token")
    if not token:
        raise RuntimeError("Missing token in Last.fm response")
    return token


def build_auth_url(token: str) -> str:
    settings = get_settings()
    return f"{LASTFM_AUTH_URL}?api_key={settings.lastfm_api_key}&token={token}"


def get_session(token: str) -> dict:
    """Exchange the approved token for a permanent session_key + username.

    Returns the `session` sub-object from Last.fm's response:
        {"name": "<lastfm-username>", "key": "<permanent-session-key>", "subscriber": 0}
    """
    settings = get_settings()

    if not settings.lastfm_api_key or not settings.lastfm_api_secret:
        raise RuntimeError("Last.fm api_key + api_secret must both be configured")

    params = {
        "method": "auth.getSession",
        "api_key": settings.lastfm_api_key,
        "token": token,
    }

    params["api_sig"] = _sign(params, settings.lastfm_api_secret)
    params["format"] = "json"

    url = "https://ws.audioscrobbler.com/2.0/"

    data_bytes = urllib.parse.urlencode(params).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data_bytes,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "your-app-name/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to contact Last.fm API: {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response from Last.fm: {body}") from exc

    if "error" in data:
        raise RuntimeError(
            f"Last.fm API error {data.get('error')}: {data.get('message')}"
        )

    session = data.get("session")
    if not session:
        raise RuntimeError("Missing session in Last.fm response")

    return session
