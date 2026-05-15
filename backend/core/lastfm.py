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
from typing import Dict, Optional

from backend.config import get_settings


# ---------------------------------------------------------------------------
# TODO: scrobble-through to users' Last.fm accounts
# ---------------------------------------------------------------------------
# The signing helper below (_sign_params) is the one bit of glue every
# Last.fm write method needs. To finish the feature you'll need:
#
#   1. Schema migration adding `lastfm_session_key TEXT` to the users table.
#      One key per user — Last.fm session keys don't expire, so this is a
#      one-time authorize step per user.
#
#   2. An auth flow to obtain that session key. Two options:
#        a) Web auth: redirect user to last.fm/api/auth?api_key=...&cb=...
#           then exchange the returned token via auth.getSession.
#        b) Mobile auth: ask user for their Last.fm username + password,
#           POST to auth.getMobileSession (signed). Simpler UX, but stores
#           credentials in transit even if briefly.
#
#   3. Endpoints/UI on the profile page: "Link Last.fm account" button +
#       "Unlink" + status indicator.
#
#   4. Wire scrobble() into /rest/scrobble:
#        - On submission=true (track finished): call track.scrobble.
#        - On submission=false (track started): call track.updateNowPlaying.
#      Per spec, scrobble only on tracks > 30s and after >50% played or 4min.
#
#   5. (Optional) Wire love()/unlove() into /rest/star + /rest/unstar.
#
# All write methods are POSTs to the same _LASTFM_BASE URL with form-encoded
# params PLUS an `api_sig` field containing the MD5 of the sorted params.
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_USER_AGENT = "Muse/0.1 (+https://github.com/your-org/muse)"

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


# ---------------------------------------------------------------------------
# Signing helper for write methods (scrobble, love, getSession, ...).
# ---------------------------------------------------------------------------

def _sign_params(params: Dict[str, str], shared_secret: str) -> str:
    """
    Return the api_sig value for a Last.fm write request.

    Algorithm (per https://www.last.fm/api/desktopauth#_6-sign-your-call):
      1. Take every parameter except `format` and `callback`.
      2. Sort by key (lexicographic).
      3. Concatenate as "key1value1key2value2...".
      4. Append the shared secret.
      5. MD5 of the resulting bytes (UTF-8). Hex digest, lowercase.

    Returns the hex digest string. Caller is responsible for adding it as
    `api_sig` to the outgoing form body and sending the request as a POST.
    """
    parts: list[str] = []
    for k in sorted(params):
        if k in ("format", "callback"):
            continue
        parts.append(k)
        parts.append(params[k])
    parts.append(shared_secret)
    return hashlib.md5("".join(parts).encode("utf-8")).hexdigest()
