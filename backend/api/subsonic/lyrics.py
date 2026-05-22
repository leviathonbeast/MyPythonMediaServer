"""
Lyrics endpoints — getLyrics (legacy) and getLyricsBySongId (OpenSubsonic).

Both serve the lyrics the scanner stored on the track row (tracks.lyrics,
migration 8 — read from a sidecar .lrc when present, else embedded USLT / ©lyr /
Vorbis tags). The stored blob may be plain text OR LRC-formatted with
[mm:ss.xx] timestamps; backend.core.lyrics is the single place that understands
LRC, so these handlers just call it.

  - getLyrics (Subsonic 1.2.0): addresses a song by artist + title (it predates
    ID3 ids) and has no notion of timing — it returns plain text only, so we
    strip any LRC tags. Response: {"lyrics": {"artist", "title", "value"}}.
    A miss (song not found / no lyrics) still returns a well-formed empty
    lyrics object — getLyrics never errors on a miss, which is what clients
    expect.

  - getLyricsBySongId (OpenSubsonic "songLyrics" extension): addresses a song by
    id and returns the richer structuredLyrics shape. We emit a single block
    that is `synced: true` with per-line `start` offsets when the stored blob is
    LRC, otherwise `synced: false` plain lines. Empty structuredLyrics list when
    the track has no lyrics.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.core import library
from backend.core import lyrics as lyrics_core

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


@_double_register("getLyrics")
def get_lyrics(
    artist: Optional[str] = Query(default=None),
    title: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """Plain lyrics for the song matching `artist` + `title`. Always an ok
    response: a miss yields an empty lyrics object, not an error."""
    value = ""
    out_artist = artist or ""
    out_title = title or ""
    if artist and title:
        row = queries.find_track_lyrics_by_name(artist, title)
        if row is not None:
            out_artist = row.get("artist_name") or out_artist
            out_title = row.get("title") or out_title
            # Plain text only — strip LRC timestamps if the blob is synced.
            value = lyrics_core.to_plain_text(row.get("lyrics"))

    # `value` is the element's text content in XML / the "value" key in JSON —
    # our responses serialiser maps a "value" key to both.
    return responses.ok(
        {"lyrics": {"artist": out_artist, "title": out_title, "value": value}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("getLyricsBySongId")
def get_lyrics_by_song_id(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    """OpenSubsonic structured lyrics for a track id. Empty structuredLyrics
    list when the track has no stored lyrics."""
    kind, rid = library.parse_id(id)
    if kind != "track" or rid is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Not a track id",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    track = queries.get_track(rid)
    if track is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Track not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )

    structured: list[dict] = []
    parsed = lyrics_core.parse(track.get("lyrics"))
    if parsed.lines:
        block = {
            "displayArtist": track.get("artist_name") or "",
            "displayTitle": track.get("title") or "",
            # We can't detect the language of stored lyrics; "xxx" is the
            # ISO 639-2 code for "no linguistic content / not applicable",
            # which is the honest value and what other servers emit.
            "lang": "xxx",
            # synced=true with per-line `start` (ms) for LRC; false plain lines
            # otherwise. The line dicts come straight from the parser.
            "synced": parsed.synced,
            "line": parsed.lines,
        }
        # The LRC [offset:] tag is reported separately; clients shift playback
        # by it rather than us baking it into every start.
        if parsed.synced and parsed.offset:
            block["offset"] = parsed.offset
        structured.append(block)

    return responses.ok(
        {"lyricsList": {"structuredLyrics": structured}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
