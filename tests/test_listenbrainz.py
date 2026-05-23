"""
Tests for the ListenBrainz integration.

Two layers are covered:

  1. Pure parsing/building helpers (no DB, no network): JSPF track parsing,
     MBID extraction, listen-payload construction, and response parsing for
     validate-token / created-for playlists. These pin the wire format we
     send to and read from ListenBrainz — get them wrong and scrobbles are
     silently rejected or playlists import as empty.

  2. The import endpoint end-to-end against a seeded library, with the
     network call stubbed. This pins the track-resolution behaviour (MBID
     first, then artist+title) and the honest matched/total/unmatched
     accounting the UI relies on.

The network is never touched: every test that would call ListenBrainz
monkeypatches `_request` (or the higher-level fetch) so the suite stays
hermetic and fast.
"""

from __future__ import annotations

import time

import pytest

from backend.core import listenbrainz as lb
from backend.db import queries, transaction


# ===========================================================================
# _extract_uuid — identifiers arrive as a string or a list of URLs
# ===========================================================================


class TestExtractUuid:
    _UUID = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"

    def test_recording_url_string(self):
        url = f"https://musicbrainz.org/recording/{self._UUID}"
        assert lb._extract_uuid(url) == self._UUID

    def test_identifier_as_list(self):
        """Newer ListenBrainz JSPF gives `identifier` as a list of URLs."""
        ids = [f"https://musicbrainz.org/recording/{self._UUID}"]
        assert lb._extract_uuid(ids) == self._UUID

    def test_playlist_url_with_trailing_slash(self):
        url = f"https://listenbrainz.org/playlist/{self._UUID}/"
        assert lb._extract_uuid(url) == self._UUID

    def test_uppercase_is_normalised_to_lowercase(self):
        url = f"https://musicbrainz.org/recording/{self._UUID.upper()}"
        assert lb._extract_uuid(url) == self._UUID  # MBIDs stored lowercase

    def test_none_and_garbage_return_none(self):
        assert lb._extract_uuid(None) is None
        assert lb._extract_uuid("no uuid here") is None
        assert lb._extract_uuid([]) is None


# ===========================================================================
# parse_jspf_tracks — normalise a JSPF playlist body to ImportTrack records
# ===========================================================================


class TestParseJspfTracks:
    def test_full_track(self):
        mbid = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
        playlist = {
            "track": [
                {
                    "identifier": [f"https://musicbrainz.org/recording/{mbid}"],
                    "title": "Svefn-g-englar",
                    "creator": "Sigur Rós",
                    "album": "Ágætis byrjun",
                }
            ]
        }
        tracks = lb.parse_jspf_tracks(playlist)
        assert len(tracks) == 1
        t = tracks[0]
        assert t.recording_mbid == mbid
        assert t.title == "Svefn-g-englar"
        assert t.artist == "Sigur Rós"
        assert t.album == "Ágætis byrjun"

    def test_track_without_mbid_keeps_artist_and_title(self):
        """A track with no identifier is still importable by artist+title."""
        playlist = {"track": [{"title": "Untagged", "creator": "Someone"}]}
        tracks = lb.parse_jspf_tracks(playlist)
        assert len(tracks) == 1
        assert tracks[0].recording_mbid is None
        assert tracks[0].album is None

    def test_track_with_neither_mbid_nor_title_dropped(self):
        """Nothing to match on → drop it rather than emit a useless row."""
        playlist = {"track": [{"creator": "Only an artist"}]}
        assert lb.parse_jspf_tracks(playlist) == []

    def test_empty_or_missing_track_list(self):
        assert lb.parse_jspf_tracks({}) == []
        assert lb.parse_jspf_tracks({"track": []}) == []


# ===========================================================================
# _track_metadata — the block every submitted listen carries
# ===========================================================================


class TestTrackMetadata:
    def test_required_fields_and_submission_client(self):
        md = lb._track_metadata(
            artist="Boards of Canada", title="Roygbiv", album=None, recording_mbid=None
        )
        assert md["artist_name"] == "Boards of Canada"
        assert md["track_name"] == "Roygbiv"
        # Always stamped so the listen is attributable to Muse.
        assert md["additional_info"]["submission_client"] == "Muse"
        # No album → no release_name key (rather than release_name=None).
        assert "release_name" not in md
        # No MBID → not present in additional_info.
        assert "recording_mbid" not in md["additional_info"]

    def test_album_and_mbid_included_when_present(self):
        mbid = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
        md = lb._track_metadata(
            artist="A", title="B", album="C", recording_mbid=mbid
        )
        assert md["release_name"] == "C"
        assert md["additional_info"]["recording_mbid"] == mbid


# ===========================================================================
# submit_listen / update_now_playing — payload shape + no-op guards
# ===========================================================================


class TestSubmitListen:
    def _capture(self, monkeypatch):
        """Replace _request with a recorder; return the list it appends to."""
        calls: list[dict] = []

        def fake_request(method, path, *, token=None, body=None, timeout=15):
            calls.append({"method": method, "path": path, "token": token, "body": body})
            return {"status": "ok"}

        monkeypatch.setattr(lb, "_request", fake_request)
        return calls

    def test_single_listen_includes_listened_at(self, monkeypatch):
        calls = self._capture(monkeypatch)
        lb.submit_listen(
            "tok", artist="A", title="B", album="C",
            recording_mbid=None, listened_at=1700000000,
        )
        assert len(calls) == 1
        body = calls[0]["body"]
        assert calls[0]["token"] == "tok"
        assert body["listen_type"] == "single"
        item = body["payload"][0]
        assert item["listened_at"] == 1700000000
        assert item["track_metadata"]["track_name"] == "B"

    def test_now_playing_omits_listened_at(self, monkeypatch):
        """A playing_now payload must NOT carry listened_at — the API rejects
        it otherwise."""
        calls = self._capture(monkeypatch)
        lb.update_now_playing("tok", artist="A", title="B")
        assert len(calls) == 1
        body = calls[0]["body"]
        assert body["listen_type"] == "playing_now"
        assert "listened_at" not in body["payload"][0]

    def test_missing_artist_or_title_is_a_noop(self, monkeypatch):
        """Don't even hit the network for a listen ListenBrainz would reject."""
        calls = self._capture(monkeypatch)
        lb.submit_listen("tok", artist="", title="B", listened_at=1)
        lb.submit_listen("tok", artist="A", title="", listened_at=1)
        lb.update_now_playing("tok", artist="", title="")
        assert calls == []

    def test_transport_failure_is_swallowed(self, monkeypatch):
        """A submission failure must never propagate — playback already
        happened, there's nothing actionable."""
        def boom(*a, **k):
            raise RuntimeError("listenbrainz down")

        monkeypatch.setattr(lb, "_request", boom)
        # Should not raise.
        lb.submit_listen("tok", artist="A", title="B", listened_at=1)
        lb.update_now_playing("tok", artist="A", title="B")


# ===========================================================================
# validate_token / get_created_for_playlists — response parsing
# ===========================================================================


class TestValidateToken:
    def test_returns_username_when_valid(self, monkeypatch):
        monkeypatch.setattr(
            lb, "_request",
            lambda *a, **k: {"valid": True, "user_name": "alice"},
        )
        assert lb.validate_token("tok") == "alice"

    def test_empty_token_rejected_without_network(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("should not call the network for an empty token")

        monkeypatch.setattr(lb, "_request", boom)
        with pytest.raises(RuntimeError):
            lb.validate_token("   ")

    def test_invalid_token_raises(self, monkeypatch):
        monkeypatch.setattr(
            lb, "_request",
            lambda *a, **k: {"valid": False, "message": "Token invalid."},
        )
        with pytest.raises(RuntimeError):
            lb.validate_token("bad")


class TestGetCreatedForPlaylists:
    def test_parses_and_skips_entries_without_mbid(self, monkeypatch):
        mbid = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
        monkeypatch.setattr(
            lb, "_request",
            lambda *a, **k: {
                "playlists": [
                    {
                        "playlist": {
                            "identifier": f"https://listenbrainz.org/playlist/{mbid}",
                            "title": "Weekly Jams",
                            "annotation": "<p>Your <b>weekly</b> jams</p>",
                        }
                    },
                    # No identifier → unaddressable → skipped.
                    {"playlist": {"title": "Broken"}},
                ]
            },
        )
        out = lb.get_created_for_playlists("tok", "alice")
        assert len(out) == 1
        assert out[0].mbid == mbid
        assert out[0].title == "Weekly Jams"
        # Annotation HTML is stripped to plain text.
        assert out[0].description == "Your weekly jams"


# ===========================================================================
# Track resolution queries (DB-backed)
# ===========================================================================


class TestTrackResolutionQueries:
    def test_match_by_artist_and_title_is_case_insensitive(self, seeded_library):
        tid = queries.find_track_id_by_artist_and_title("test artist", "TEST SONG")
        assert tid == seeded_library["track_id"]

    def test_no_match_returns_none(self, seeded_library):
        assert queries.find_track_id_by_artist_and_title("Nobody", "Nothing") is None
        assert queries.find_track_id_by_artist_and_title("", "Test Song") is None

    def test_match_by_musicbrainz_id(self, seeded_library):
        mbid = "8f3471b5-7e6a-48da-86a9-c1c07a0f47ae"
        now = int(time.time())
        with transaction():
            tagged_id = queries.upsert_track({
                "album_id":        seeded_library["album_id"],
                "artist_id":       seeded_library["artist_id"],
                "music_folder_id": seeded_library["folder_id"],
                "path":            "/test/fixtures/music/tagged.mp3",
                "title":           "Tagged Song",
                "track_number":    2,
                "disc_number":     1,
                "duration":        200,
                "bitrate":         320,
                "size":            8_000_000,
                "suffix":          "mp3",
                "content_type":    "audio/mpeg",
                "year":            2024,
                "genre":           "Indie",
                "mtime":           now,
                "content_hash":    None,
                "last_scanned":    now,
                "musicbrainz_id":  mbid,
            })
        assert queries.find_track_id_by_musicbrainz_id(mbid) == tagged_id
        assert queries.find_track_id_by_musicbrainz_id("nope") is None


# ===========================================================================
# Import endpoint — end-to-end with the network stubbed
# ===========================================================================


def _link_listenbrainz(username: str) -> None:
    """Link the 'regular' test user to a ListenBrainz account.

    Committed via transaction() so the request handler thread (separate
    SQLite connection) can see it — same pattern conftest uses for users.
    """
    user = queries.get_user_by_username("regular")
    assert user is not None
    with transaction():
        queries.set_external_account(
            user["id"], queries.SERVICE_LISTENBRAINZ,
            auth_token="user-token", username=username,
        )


class TestImportEndpoint:
    def test_import_creates_playlist_and_reports_coverage(
        self, client, user_headers, seeded_library, monkeypatch
    ):
        _link_listenbrainz("alice")

        playlist_mbid = "11111111-1111-1111-1111-111111111111"
        # One track matches the seeded library (by artist+title); one doesn't.
        fake_jspf = {
            "title": "Weekly Jams for alice",
            "track": [
                {"title": "Test Song", "creator": "Test Artist", "album": "Test Album"},
                {"title": "Not In Library", "creator": "Ghost"},
            ],
        }
        monkeypatch.setattr(
            lb, "fetch_playlist", lambda token, mbid: fake_jspf
        )

        resp = client.post(
            "/api/me/listenbrainz/playlists/import",
            json={"playlist_mbid": playlist_mbid},
            headers=user_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["matched"] == 1
        assert data["total"] == 2
        assert data["unmatched"] == ["Ghost — Not In Library"]
        assert data["name"] == "Weekly Jams for alice"

        # The playlist really exists and holds the one matched track.
        created = queries.get_playlist(data["playlist_id"])
        assert created is not None
        assert created["owner_id"] == queries.get_user_by_username("regular")["id"]
        assert len(created["tracks"]) == 1
        assert created["tracks"][0]["id"] == seeded_library["track_id"]

    def test_import_with_no_matches_is_422_and_creates_nothing(
        self, client, user_headers, seeded_library, monkeypatch
    ):
        _link_listenbrainz("alice")
        monkeypatch.setattr(
            lb, "fetch_playlist",
            lambda token, mbid: {"title": "Misses", "track": [
                {"title": "Nope", "creator": "Nobody"},
            ]},
        )
        resp = client.post(
            "/api/me/listenbrainz/playlists/import",
            json={"playlist_mbid": "22222222-2222-2222-2222-222222222222"},
            headers=user_headers,
        )
        assert resp.status_code == 422

    def test_import_requires_linked_account(self, client, user_headers, seeded_library):
        # 'regular' is not linked in this test.
        resp = client.post(
            "/api/me/listenbrainz/playlists/import",
            json={"playlist_mbid": "33333333-3333-3333-3333-333333333333"},
            headers=user_headers,
        )
        assert resp.status_code == 400
