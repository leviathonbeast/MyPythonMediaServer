"""
Tests for star / unstar / getStarred / getStarred2.

Covers:
  - Round-trip: star a track/album/artist → it appears in getStarred.
  - Envelope keys: getStarred returns 'starred', getStarred2 returns 'starred2'.
  - Hydration: the wide-projection query in get_starred_items returns
    every column _build_starred reads, so the song/album/artist responses
    are fully populated without per-row fetches.
  - LEFT JOIN safety: a starred row whose target was deleted (orphan) is
    silently skipped instead of crashing the endpoint.
  - Per-user isolation: stars are scoped to user_id; user A doesn't see
    user B's stars.

WHY worth testing carefully:
    getStarred / getStarred2 are called by clients on connect to render
    the "Favourites" tab. A null-deref or wrong envelope key here breaks
    every Subsonic client at the same time, and the bug is invisible to
    the dev because the test DB is usually empty during exploratory
    pokes. These tests seed real rows so the joined columns actually
    matter.
"""

from __future__ import annotations

import pytest

from backend.db import queries, transaction

from ._subsonic import sub as _sub, ok as _ok


# ===========================================================================
# Envelope keys
# ===========================================================================


class TestEnvelopeKeys:
    def test_getStarred_uses_starred_key(self, client):
        body = _ok(_sub(client, "getStarred"))
        assert "starred" in body
        assert "starred2" not in body

    def test_getStarred2_uses_starred2_key(self, client):
        """Regression — the old impl returned 'starred' for both endpoints,
        which broke clients that switch on the envelope key."""
        body = _ok(_sub(client, "getStarred2"))
        assert "starred2" in body
        assert "starred" not in body


# ===========================================================================
# Empty state
# ===========================================================================


class TestEmptyState:
    def test_no_stars_returns_three_empty_arrays(self, client):
        body = _ok(_sub(client, "getStarred"))
        st = body["starred"]
        assert st["artist"] == []
        assert st["album"] == []
        assert st["song"] == []


# ===========================================================================
# Round-trip star → getStarred
# ===========================================================================


class TestStarTrack:
    def test_starred_track_appears_in_song_list(self, client, seeded_library):
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        songs = body["starred"]["song"]
        assert len(songs) == 1
        s = songs[0]
        assert s["id"] == seeded_library["track_prefix"]
        assert s["title"] == "Test Song"

    def test_starred_track_has_hydrated_album_and_artist(self, client, seeded_library):
        """Validates the wide-projection JOIN actually populates the response.
        If the joined columns weren't being read, these fields would be empty."""
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        s = body["starred"]["song"][0]
        assert s["album"] == "Test Album"
        assert s["artist"] == "Test Artist"
        assert s["albumId"] == seeded_library["album_prefix"]
        assert s["artistId"] == seeded_library["artist_prefix"]

    def test_starred_track_has_iso_starred_timestamp(self, client, seeded_library):
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        s = body["starred"]["song"][0]
        # ISO 8601 with timezone offset (e.g. "2026-05-14T20:31:00+00:00").
        assert "T" in s["starred"]
        # Tolerate either +00:00 or Z but require some TZ indicator.
        assert "+" in s["starred"] or s["starred"].endswith("Z")

    def test_starred_track_has_audio_metadata(self, client, seeded_library):
        """A track-shaped row needs duration / bitrate / suffix populated so
        the client can render a player chip without an extra getSong call."""
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        s = body["starred"]["song"][0]
        assert s["duration"] == 180
        assert s["bitRate"] == 320
        assert s["suffix"] == "mp3"


class TestStarAlbum:
    def test_starred_album_appears_in_album_list(self, client, seeded_library):
        _ok(_sub(client, "star", albumId=seeded_library["album_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        albums = body["starred"]["album"]
        assert len(albums) == 1
        a = albums[0]
        assert a["id"] == seeded_library["album_prefix"]
        assert a["name"] == "Test Album"

    def test_starred_album_has_hydrated_artist(self, client, seeded_library):
        """Catches the original null-deref bug — if the album_artist_name
        JOIN drops out, this assertion fails with a missing/None value."""
        _ok(_sub(client, "star", albumId=seeded_library["album_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        a = body["starred"]["album"][0]
        assert a["artist"] == "Test Artist"
        assert a["artistId"] == seeded_library["artist_prefix"]
        assert a["year"] == 2024
        assert a["genre"] == "Indie"


class TestStarArtist:
    def test_starred_artist_appears_in_artist_list(self, client, seeded_library):
        _ok(_sub(client, "star", artistId=seeded_library["artist_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        artists = body["starred"]["artist"]
        assert len(artists) == 1
        assert artists[0]["id"] == seeded_library["artist_prefix"]
        assert artists[0]["name"] == "Test Artist"


# ===========================================================================
# Star multiple
# ===========================================================================


class TestStarMultiple:
    def test_star_all_three_types_in_one_call(self, client, seeded_library):
        _ok(_sub(client, "star",
                 id=seeded_library["track_prefix"],
                 albumId=seeded_library["album_prefix"],
                 artistId=seeded_library["artist_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        assert len(body["starred"]["song"]) == 1
        assert len(body["starred"]["album"]) == 1
        assert len(body["starred"]["artist"]) == 1

    def test_getStarred2_returns_same_payload_under_different_key(self, client, seeded_library):
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body1 = _ok(_sub(client, "getStarred"))
        body2 = _ok(_sub(client, "getStarred2"))
        # Apart from the envelope key, the payload shape is identical.
        assert body1["starred"] == body2["starred2"]


# ===========================================================================
# Unstar
# ===========================================================================


class TestUnstar:
    def test_unstar_track_removes_it_from_song_list(self, client, seeded_library):
        _ok(_sub(client, "star",   id=seeded_library["track_prefix"]))
        _ok(_sub(client, "unstar", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        assert body["starred"]["song"] == []

    def test_unstar_unknown_id_is_noop(self, client, seeded_library):
        """Unstarring something that was never starred should silently succeed."""
        _ok(_sub(client, "unstar", id=seeded_library["track_prefix"]))

    def test_double_star_is_idempotent(self, client, seeded_library):
        """star_item uses INSERT OR IGNORE, so the same row is only counted once."""
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        _ok(_sub(client, "star", id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred"))
        assert len(body["starred"]["song"]) == 1


# ===========================================================================
# Per-user isolation
# ===========================================================================


class TestPerUserScope:
    def test_admin_star_not_visible_to_regular(self, client, seeded_library):
        _ok(_sub(client, "star", admin=True, id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred", admin=False))
        assert body["starred"]["song"] == []

    def test_regular_star_not_visible_to_admin(self, client, seeded_library):
        _ok(_sub(client, "star", admin=False, id=seeded_library["track_prefix"]))
        body = _ok(_sub(client, "getStarred", admin=True))
        assert body["starred"]["song"] == []


# ===========================================================================
# LEFT JOIN safety
# ===========================================================================


class TestOrphanedStars:
    def test_star_pointing_at_missing_track_is_skipped(self, client):
        """The cascade FK on starred(track_id) should remove the row when
        the track is deleted, but defensive code in _build_starred also
        guards against it via the LEFT JOIN NULL check. We exercise that
        path by inserting a star directly for a track that never existed.
        """
        # Bypass the cascade by inserting a raw star row.
        admin = queries.get_user_by_username("admin")
        with transaction():
            queries.star_item(admin["id"], "track", 999_999)
        body = _ok(_sub(client, "getStarred"))
        assert body["starred"]["song"] == []
        # Endpoint did NOT crash — that's the real assertion.

    def test_star_pointing_at_missing_album_is_skipped(self, client):
        admin = queries.get_user_by_username("admin")
        with transaction():
            queries.star_item(admin["id"], "album", 999_999)
        body = _ok(_sub(client, "getStarred"))
        assert body["starred"]["album"] == []

    def test_star_pointing_at_missing_artist_is_skipped(self, client):
        admin = queries.get_user_by_username("admin")
        with transaction():
            queries.star_item(admin["id"], "artist", 999_999)
        body = _ok(_sub(client, "getStarred"))
        assert body["starred"]["artist"] == []
