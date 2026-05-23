"""
ListenBrainz integration: scrobbling ("submitting listens") and importing
the recommendation playlists ListenBrainz generates for a user.

How this differs from the Last.fm module
-----------------------------------------
Last.fm uses an OAuth-like token dance plus a server-wide api_key/secret and
md5-signed requests (see backend/core/lastfm.py). ListenBrainz is much
simpler: every authenticated call is keyed by a single per-user **user
token** that the user copies from https://listenbrainz.org/settings/. There
is no server-side credential, no signing, and no redirect handshake — we
just store that token (in user_external_accounts, service="listenbrainz")
and send it as an `Authorization: Token <token>` header.

What lives here
---------------
  * validate_token()          — confirm a pasted token and learn the username
  * submit_listen()           — permanent scrobble (listen_type "single")
  * update_now_playing()      — transient "playing now" (no listened_at)
  * get_created_for_playlists() — the generated playlists (Weekly Jams, etc.)
  * fetch_playlist()          — a single playlist's JSPF body (with tracks)
  * parse_jspf_tracks()       — normalise JSPF tracks → ImportTrack records

Design notes (carried over from the Last.fm module's conventions):
  * Stdlib only. urllib, not requests/httpx — no new runtime dependency.
  * Submission failures are logged, never raised: the user's playback
    already happened, and a flaky external service must not break the
    Subsonic scrobble endpoint. Interactive calls (validate, playlist
    fetch) DO raise RuntimeError so the web layer can surface the error.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# Public ListenBrainz API root. The submission/playlist hosts are the same
# service; only the path differs.
LISTENBRAINZ_API_ROOT = "https://api.listenbrainz.org"

# Identifies us in submitted listens so they're attributable in the user's
# ListenBrainz history (and so they can filter by client if they want).
_SUBMISSION_CLIENT = "Muse"
_SUBMISSION_CLIENT_VERSION = "0.1.0"
_USER_AGENT = f"{_SUBMISSION_CLIENT}/{_SUBMISSION_CLIENT_VERSION} (+https://github.com/your-org/muse)"

# JSPF (the XSPF-derived JSON playlist format ListenBrainz speaks) namespaces
# its extension blobs under these keys. We only read from them defensively.
_JSPF_TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"

# A MusicBrainz recording identifier looks like
# "https://musicbrainz.org/recording/<uuid>"; we want the trailing uuid.
# ListenBrainz playlist identifiers look like
# "https://listenbrainz.org/playlist/<uuid>" (sometimes with a trailing
# slash). One UUID regex serves both.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class GeneratedPlaylist:
    """One entry from the user's "created for you" list.

    `mbid` is what fetch_playlist() needs; the rest is for display so the
    user can pick which playlist to import.
    """

    mbid: str
    title: str
    description: str  # ListenBrainz's annotation, plain text (may be "")
    track_count: Optional[int]  # often unknown until the playlist is fetched

    def as_dict(self) -> dict:
        return {
            "mbid": self.mbid,
            "title": self.title,
            "description": self.description,
            "track_count": self.track_count,
        }


@dataclass
class ImportTrack:
    """A single JSPF track normalised down to what we can match against the
    local library. Every field is best-effort: ListenBrainz playlists are
    rich, but a given track may carry only an MBID, only artist+title, or
    both."""

    recording_mbid: Optional[str]
    title: str
    artist: str
    album: Optional[str]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict] = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Make a JSON request to the ListenBrainz API and return the parsed body.

    Raises RuntimeError on transport failure, non-2xx status, or invalid
    JSON — callers that must not fail (scrobble paths) wrap this in their own
    try/except; interactive callers let it propagate to the web layer.
    """
    url = LISTENBRAINZ_API_ROOT + path
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if token:
        # ListenBrainz expects the literal scheme "Token", not "Bearer".
        headers["Authorization"] = f"Token {token}"

    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # ListenBrainz returns a JSON {"code":..,"error":..} body on errors;
        # surface its message when we can, so "invalid token" reads clearly.
        detail = exc.reason
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("error") or payload.get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"ListenBrainz API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to contact ListenBrainz: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from ListenBrainz: {exc}") from exc


# ---------------------------------------------------------------------------
# Auth — token validation
# ---------------------------------------------------------------------------


def validate_token(token: str) -> str:
    """Confirm a user token is valid and return the ListenBrainz username.

    Used at link time so we can store the username alongside the token and
    show "Linked as <name>" — and so a typo'd token is rejected immediately
    rather than silently dropping every future scrobble.

    Raises RuntimeError if the token is empty, rejected, or the service is
    unreachable.
    """
    token = (token or "").strip()
    if not token:
        raise RuntimeError("A ListenBrainz user token is required")

    data = _request("GET", "/1/validate-token", token=token)
    # Response shape: {"code":200,"message":"Token valid.","valid":true,
    #                  "user_name":"<name>"}
    if not data.get("valid"):
        raise RuntimeError(data.get("message") or "Token rejected by ListenBrainz")
    username = data.get("user_name")
    if not username:
        # valid==true with no name shouldn't happen, but never store a token
        # we can't attribute to a user.
        raise RuntimeError("ListenBrainz accepted the token but returned no username")
    return username


# ---------------------------------------------------------------------------
# Scrobbling — submit listens
# ---------------------------------------------------------------------------


def _track_metadata(
    *,
    artist: str,
    title: str,
    album: Optional[str],
    recording_mbid: Optional[str],
) -> dict[str, Any]:
    """Build the `track_metadata` block shared by single + playing-now listens.

    ListenBrainz requires artist_name and track_name; everything else is
    optional `additional_info`. We always stamp the submission client so the
    listen is attributable, and include the recording MBID when we have one
    (it lets ListenBrainz skip its fuzzy metadata→MBID lookup).
    """
    additional: dict[str, Any] = {
        "submission_client": _SUBMISSION_CLIENT,
        "submission_client_version": _SUBMISSION_CLIENT_VERSION,
    }
    if recording_mbid:
        additional["recording_mbid"] = recording_mbid

    metadata: dict[str, Any] = {
        "artist_name": artist,
        "track_name": title,
        "additional_info": additional,
    }
    if album:
        metadata["release_name"] = album
    return metadata


def submit_listen(
    token: str,
    *,
    artist: str,
    title: str,
    album: Optional[str] = None,
    recording_mbid: Optional[str] = None,
    listened_at: int,
) -> None:
    """Submit a permanent listen (listen_type "single").

    Mirrors lastfm.scrobble_track: errors are logged, never raised, because
    the playback already happened and there's nothing actionable to do on a
    failed submission.
    """
    if not artist or not title:
        return  # ListenBrainz rejects listens without artist/track names
    payload = {
        "listen_type": "single",
        "payload": [
            {
                "listened_at": listened_at,
                "track_metadata": _track_metadata(
                    artist=artist,
                    title=title,
                    album=album,
                    recording_mbid=recording_mbid,
                ),
            }
        ],
    }
    try:
        _request("POST", "/1/submit-listens", token=token, body=payload, timeout=10)
    except Exception:
        log.warning(
            "listenbrainz submit_listen failed: %s — %s", artist, title, exc_info=True
        )


def update_now_playing(
    token: str,
    *,
    artist: str,
    title: str,
    album: Optional[str] = None,
    recording_mbid: Optional[str] = None,
) -> None:
    """Tell ListenBrainz what's playing right now (listen_type "playing_now").

    Transient — ListenBrainz shows it as the current track but doesn't record
    it permanently. A "playing_now" payload must NOT carry `listened_at`
    (the API rejects it otherwise). Errors logged, never raised.
    """
    if not artist or not title:
        return
    payload = {
        "listen_type": "playing_now",
        "payload": [
            {
                "track_metadata": _track_metadata(
                    artist=artist,
                    title=title,
                    album=album,
                    recording_mbid=recording_mbid,
                ),
            }
        ],
    }
    try:
        _request("POST", "/1/submit-listens", token=token, body=payload, timeout=10)
    except Exception:
        log.warning(
            "listenbrainz now-playing failed: %s — %s", artist, title, exc_info=True
        )


# ---------------------------------------------------------------------------
# Playlist import — the "created for you" recommendation playlists
# ---------------------------------------------------------------------------


def _extract_uuid(value: Any) -> Optional[str]:
    """Pull a UUID out of a JSPF identifier, which may be a string or a list.

    Newer ListenBrainz responses give `identifier` as a list of URLs; older
    ones as a single string. We scan whichever we get for the first UUID and
    return it lowercased (MBIDs are conventionally lowercase)."""
    candidates: list[str]
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [v for v in value if isinstance(v, str)]
    else:
        return None
    for candidate in candidates:
        m = _UUID_RE.search(candidate)
        if m:
            return m.group(0).lower()
    return None


def get_created_for_playlists(
    token: str, username: str, *, count: int = 25
) -> list[GeneratedPlaylist]:
    """List the recommendation playlists ListenBrainz has generated for a user.

    These are the "created for you" playlists — Weekly Jams, Weekly
    Exploration, Daily Jams and friends — produced by ListenBrainz's troi
    recommendation engine. The list view returns metadata only (no tracks);
    fetch_playlist() pulls a chosen one's tracks.

    Raises RuntimeError on failure so the web layer can report it.
    """
    path = f"/1/user/{urllib.parse.quote(username)}/playlists/createdfor?count={int(count)}"
    data = _request("GET", path, token=token)

    out: list[GeneratedPlaylist] = []
    for entry in data.get("playlists") or []:
        # Each entry wraps the JSPF object under a "playlist" key.
        pl = (entry or {}).get("playlist") or {}
        mbid = _extract_uuid(pl.get("identifier"))
        if not mbid:
            continue  # can't import what we can't address
        # `track` is usually empty in the list view; only trust it if present.
        tracks = pl.get("track")
        track_count = len(tracks) if isinstance(tracks, list) and tracks else None
        out.append(
            GeneratedPlaylist(
                mbid=mbid,
                title=pl.get("title") or "Untitled playlist",
                description=_strip_html(pl.get("annotation") or ""),
                track_count=track_count,
            )
        )
    return out


def fetch_playlist(token: str, playlist_mbid: str) -> dict[str, Any]:
    """Fetch one playlist's full JSPF body (including its tracks).

    Sends the token even for public playlists — created-for playlists can be
    private to the user, and an authenticated request works for both.

    Raises RuntimeError on failure. Returns the inner `playlist` object.
    """
    mbid = (playlist_mbid or "").strip().lower()
    if not _UUID_RE.fullmatch(mbid):
        raise RuntimeError("Invalid playlist id")
    data = _request("GET", f"/1/playlist/{mbid}", token=token)
    playlist = data.get("playlist")
    if not isinstance(playlist, dict):
        raise RuntimeError("ListenBrainz returned no playlist body")
    return playlist


def parse_jspf_tracks(playlist: dict[str, Any]) -> list[ImportTrack]:
    """Normalise a JSPF playlist's tracks into ImportTrack records.

    JSPF track fields we read:
      * identifier — MusicBrainz recording URL(s); we extract the MBID
      * title      — track name
      * creator    — artist name
      * album      — release name (optional)

    Tracks missing both an MBID and a title are dropped — there's nothing to
    match them on.
    """
    out: list[ImportTrack] = []
    for raw in playlist.get("track") or []:
        if not isinstance(raw, dict):
            continue
        recording_mbid = _extract_uuid(raw.get("identifier"))
        title = (raw.get("title") or "").strip()
        artist = (raw.get("creator") or "").strip()
        album = (raw.get("album") or "").strip() or None
        if not recording_mbid and not title:
            continue
        out.append(
            ImportTrack(
                recording_mbid=recording_mbid,
                title=title,
                artist=artist,
                album=album,
            )
        )
    return out


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """ListenBrainz annotations are short HTML snippets; we want plain text.

    Same rationale as the Last.fm bio stripping: rendering third-party HTML
    in our UI is an XSS smell, and the description is decorative anyway.
    """
    if not s:
        return ""
    s = _TAG_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s
