"""
Library service.

Translates between internal integer ids and Subsonic-style string ids
("ar-123", "al-456", "tr-789") and assembles the data shapes the API layer
serialises.

WHY string-prefixed ids:
    Subsonic clients treat ids as opaque strings. Prefixing lets us tell at
    a glance whether an id refers to an artist/album/track without an extra
    DB lookup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.db import queries


# ---------------------------------------------------------------------------
# Subsonic id helpers
# ---------------------------------------------------------------------------

ARTIST_PREFIX = "ar-"
ALBUM_PREFIX  = "al-"
TRACK_PREFIX  = "tr-"


def make_artist_id(rid: int) -> str: return f"{ARTIST_PREFIX}{rid}"
def make_album_id(rid: int)  -> str: return f"{ALBUM_PREFIX}{rid}"
def make_track_id(rid: int)  -> str: return f"{TRACK_PREFIX}{rid}"


def parse_id(s: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parse 'ar-123' -> ('artist', 123). Returns (None, None) for garbage.
    """
    if not s:
        return None, None
    for prefix, kind in ((ARTIST_PREFIX, "artist"), (ALBUM_PREFIX, "album"), (TRACK_PREFIX, "track")):
        if s.startswith(prefix):
            try:
                return kind, int(s[len(prefix):])
            except ValueError:
                return None, None
    # Tolerate bare integers for backwards compatibility / test convenience.
    try:
        return "track", int(s)
    except ValueError:
        return None, None


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------

def get_indexes() -> Dict[str, Any]:
    """
    Build the response for getIndexes.

    Subsonic shape:
      indexes:
        index[]:
          name: 'A'
          artist[]: { id, name, albumCount }
    """
    grouped = queries.list_artists_indexed()
    indexes = []
    for letter in sorted(grouped.keys()):
        indexes.append({
            "name": letter,
            "artist": [
                {
                    "id":         make_artist_id(a["id"]),
                    "name":       a["name"],
                    "albumCount": a["albumCount"],
                    "coverArt":   a.get("coverArtId"),
                }
                for a in grouped[letter]
            ],
        })
    return {"index": indexes}


def get_music_directory(directory_id: str) -> Optional[Dict[str, Any]]:
    """
    Subsonic 'directory' is overloaded: it can be an artist (children = albums)
    or an album (children = tracks). We dispatch on the id prefix.

    Returning None -> 404 in the API layer.
    """
    kind, rid = parse_id(directory_id)
    if kind == "artist":
        artist = queries.get_artist(rid)
        if not artist:
            return None
        albums = queries.list_artist_albums(rid)
        return {
            "id":   make_artist_id(rid),
            "name": artist["name"],
            "child": [_album_to_directory_child(a, artist["name"]) for a in albums],
        }
    if kind == "album":
        album = queries.get_album(rid)
        if not album:
            return None
        tracks = queries.list_album_tracks(rid)
        return {
            "id":     make_album_id(rid),
            "name":   album["name"],
            "parent": make_artist_id(album["artist_id"]),
            "child":  [track_to_subsonic(t) for t in tracks],
        }
    return None


def _album_to_directory_child(album: Dict[str, Any], artist_name: str) -> Dict[str, Any]:
    """Render an album as a directory child (the Subsonic dirty-trick view)."""
    return {
        "id":         make_album_id(album["id"]),
        "parent":     make_artist_id(album.get("artist_id", 0)) if album.get("artist_id") else None,
        "title":      album["name"],
        "album":      album["name"],
        "artist":     artist_name,
        "isDir":      True,
        "coverArt":   album.get("cover_art_id"),
        "year":       album.get("year"),
        "genre":      album.get("genre"),
        "songCount":  album.get("track_count"),
        "duration":   album.get("duration"),
        "created":    _epoch_to_iso(album.get("created_at")),
    }


def list_albums(
    list_type: str,
    size: int,
    offset: int,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    genre: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """getAlbumList. Returns rendered Subsonic album dicts."""
    rows = queries.list_albums(
        list_type=list_type, size=size, offset=offset,
        from_year=from_year, to_year=to_year, genre=genre,
    )
    return [_album_to_directory_child(r, r["artist_name"]) for r in rows]


def get_album_with_tracks(album_id: str) -> Optional[Dict[str, Any]]:
    kind, rid = parse_id(album_id)
    if kind != "album":
        return None
    album = queries.get_album(rid)
    if not album:
        return None
    tracks = queries.list_album_tracks(rid)
    return {
        **_album_to_directory_child(album, album["artist_name"]),
        # AlbumID3 fields expected by ID3-mode clients (name + artistId).
        "name":     album["name"],
        "artistId": make_artist_id(album["artist_id"]) if album.get("artist_id") else None,
        "song":     [track_to_subsonic(t) for t in tracks],
    }

def get_song(song_id: str) -> Optional[Dict[str, any]]:
    kind, rid = parse_id(song_id)
    if kind != "track" or rid is None:
        return None
    # Load the row
    row = queries.get_track(rid)
    if row is None:
        return None
    return track_to_subsonic(row)

# ---------------------------------------------------------------------------
# Track serialisation
# ---------------------------------------------------------------------------

def track_to_subsonic(t: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render a track row as an OpenSubsonic song object.

    Includes all Subsonic 1.16.1 required fields plus the OpenSubsonic
    extensions (mediaType, genres, artists, albumArtists, displayArtist,
    displayAlbumArtist) where we have the data. Fields the scanner doesn't
    yet populate (bitDepth, samplingRate, channelCount, replayGain, bpm,
    comment, musicBrainzId, contributors, moods) are omitted rather than
    sent as null, keeping the payload compact and clients happy.
    """
    artist_id_str  = make_artist_id(t["artist_id"]) if t.get("artist_id") else None
    album_id_str   = make_album_id(t["album_id"])   if t.get("album_id")  else None
    artist_name    = t.get("artist_name")
    genre          = t.get("genre")

    out: Dict[str, Any] = {
        "id":          make_track_id(t["id"]),
        "parent":      album_id_str,
        "isDir":       False,
        "title":       t["title"],
        "album":       t.get("album_name"),
        "artist":      artist_name,
        "track":       t.get("track_number"),
        "discNumber":  t.get("disc_number"),
        "year":        t.get("year"),
        "genre":       genre,
        "coverArt":    t.get("cover_art_id"),
        "size":        t.get("size"),
        "contentType": t.get("content_type"),
        "suffix":      t.get("suffix"),
        "duration":    t.get("duration"),
        "bitRate":     t.get("bitrate"),
        "path":        _relative_path(t.get("path")),
        "isVideo":     False,
        "albumId":     album_id_str,
        "artistId":    artist_id_str,
        "type":        "music",
        # OpenSubsonic extensions
        "mediaType":   "song",
    }

    # Multi-value genre array (OpenSubsonic)
    if genre:
        out["genres"] = [{"name": genre}]

    # Multi-artist arrays (OpenSubsonic) — we have a single artist per track
    if artist_id_str and artist_name:
        out["artists"]       = [{"id": artist_id_str, "name": artist_name}]
        out["displayArtist"] = artist_name

    # Album artist (may differ from track artist)
    album_artist_id   = t.get("album_artist_id")
    album_artist_name = t.get("album_artist_name") or artist_name
    if album_artist_id and album_artist_name:
        aa_id_str = make_artist_id(album_artist_id)
        out["albumArtists"]       = [{"id": aa_id_str, "name": album_artist_name}]
        out["displayAlbumArtist"] = album_artist_name
    elif artist_id_str and album_artist_name:
        out["albumArtists"]       = [{"id": artist_id_str, "name": album_artist_name}]
        out["displayAlbumArtist"] = album_artist_name

    return out


def _relative_path(absolute: Optional[str]) -> Optional[str]:
    """
    Return a path relative to its music folder root, for the Subsonic 'path' field.

    Clients sometimes display this. Stripping the absolute root keeps it tidy
    and avoids leaking server-side directory structure.
    """
    if not absolute:
        return None
    # Best-effort: just return the file name + parent. We don't need to be
    # exact here; Subsonic clients use the id, not the path, for actions.
    p = Path(absolute)
    parts = p.parts
    if len(parts) > 3:
        return str(Path(*parts[-3:]))
    return p.name


def _epoch_to_iso(epoch: Optional[int]) -> Optional[str]:
    """Convert epoch seconds to Subsonic-style ISO 8601 (UTC)."""
    if epoch is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
