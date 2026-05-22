"""
Lyrics parsing — turn a stored lyrics blob into the shapes the API needs.

The scanner stores whatever lyrics it found (embedded tag or sidecar .lrc) as a
single text blob in tracks.lyrics. That blob is either:
  - plain text (one lyric per line), or
  - LRC-formatted, with [mm:ss.xx] timestamps per line.

This module is the single place that understands LRC, so the endpoints stay
dumb: getLyricsBySongId calls parse() and forwards the result; getLyrics calls
to_plain_text(). Keeping it pure (str in, dataclass/str out) means it's unit
-testable with no DB or HTTP.

Supported LRC features:
  - line timestamps [mm:ss], [mm:ss.xx] (centiseconds), [mm:ss.xxx] (ms)
  - several timestamps on one line (repeated lines, e.g. a chorus)
  - the [offset:±ms] tag — returned separately so the client can apply it
    (per the OpenSubsonic structuredLyrics `offset` field), NOT baked into
    each line's start
  - other ID tags [ar:], [ti:], [al:], [by:], [length:], … are stripped

Not supported (deliberately): enhanced word-level <mm:ss.xx> timing. It's rare;
those inline markers are simply left in the line text rather than mis-parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A line timestamp: minutes:seconds with an optional fractional part. The key is
# numeric, which is what tells it apart from a metadata tag.
_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# An LRC metadata/ID tag: [word:value]. Key starts non-numeric, so it can never
# collide with a timestamp — the two regexes match disjoint sets.
_META = re.compile(r"\[([a-zA-Z#][a-zA-Z0-9_#]*):([^\]]*)\]")


@dataclass
class ParsedLyrics:
    """Result of parse(). `lines` items are {"value": str} when unsynced and
    {"start": int_ms, "value": str} when synced."""
    synced: bool
    lines: list[dict] = field(default_factory=list)
    offset: int = 0  # global ms offset from an [offset:] tag; 0 when absent


def _frac_to_ms(frac: str | None) -> int:
    """LRC fractional seconds → milliseconds. '34'→340, '5'→500, '345'→345."""
    if not frac:
        return 0
    return int((frac + "000")[:3])


def _ts_to_ms(minutes: str, seconds: str, frac: str | None) -> int:
    return (int(minutes) * 60 + int(seconds)) * 1000 + _frac_to_ms(frac)


def _strip_tags(line: str) -> str:
    """Remove every [..] timestamp and metadata tag, leaving the lyric text.
    Order is irrelevant since the two patterns match disjoint tags."""
    return _TIMESTAMP.sub("", _META.sub("", line)).strip()


def parse(text: str | None) -> ParsedLyrics:
    """Parse a stored lyrics blob into (synced?, lines, offset).

    If *any* line carries a timestamp the blob is treated as synced: only the
    timestamped lines are emitted, sorted by start time (so repeated-timestamp
    choruses and out-of-order files come out in playback order). Otherwise it's
    unsynced and every line is emitted in document order.
    """
    if not text:
        return ParsedLyrics(synced=False, lines=[])

    offset = 0
    synced_lines: list[dict] = []
    plain_lines: list[str] = []
    has_timestamps = False

    for raw in text.splitlines():
        meta_matches = _META.findall(raw)
        for key, val in meta_matches:
            if key.lower() == "offset":
                try:
                    offset = int(val.strip())
                except ValueError:
                    pass

        timestamps = list(_TIMESTAMP.finditer(raw))
        text_part = _strip_tags(raw)

        if timestamps:
            has_timestamps = True
            for m in timestamps:
                start = _ts_to_ms(m.group(1), m.group(2), m.group(3))
                synced_lines.append({"start": start, "value": text_part})
        elif meta_matches and not text_part:
            # Pure metadata line ([ar:..], [offset:..], …) — not a lyric.
            continue
        else:
            plain_lines.append(text_part)

    if has_timestamps:
        synced_lines.sort(key=lambda ln: ln["start"])
        return ParsedLyrics(synced=True, lines=synced_lines, offset=offset)
    return ParsedLyrics(
        synced=False, lines=[{"value": v} for v in plain_lines], offset=0
    )


def to_plain_text(text: str | None) -> str:
    """Lyrics as plain text with all LRC tags removed, in playback order.

    Used by the legacy getLyrics endpoint, which has no notion of timing.
    """
    if not text:
        return ""
    return "\n".join(ln["value"] for ln in parse(text).lines)
