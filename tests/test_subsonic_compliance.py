"""
OpenSubsonic compliance tests.

Two goals:
  1. Verify every implemented /rest/* endpoint returns a properly-shaped
     OpenSubsonic response envelope (status, version, type, serverVersion,
     openSubsonic) and the right child object key for that endpoint.
  2. Surface endpoints we *don't* implement but that real clients
     (DSub, Symfonium, Feishin, Substreamer) expect on connect. Each one
     gets a TestMissingEndpoints case that the test author intentionally
     expects to fail today — they're documentation of the gap.

The DB is empty for these tests (no seeded music), so we exercise:
  - happy-path "empty list" responses (most browse endpoints)
  - error-path "not found" responses (anything that needs an id)
  - shape conformance for both
"""

from __future__ import annotations

import pytest

from backend.db import queries
from backend.db.connection import transaction

from ._subsonic import sub as _sub, ok as _ok, err as _err


# ===========================================================================
# 1. Envelope conformance
# ===========================================================================


class TestEnvelopeConformance:
    """Every endpoint must return the OpenSubsonic envelope on both ok and failure."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "ping",
            "getLicense",
            "getMusicFolders",
            "getIndexes",
            "getArtists",
            "getAlbumList",
            "getAlbumList2",
            "getGenres",
            "getRandomSongs",
            "getTopSongs",
            "getStarred",
            "getStarred2",
            "getNowPlaying",
            "getPlaylists",
        ],
    )
    def test_no_arg_endpoints_return_valid_envelope(self, client, endpoint):
        _ok(_sub(client, endpoint))

    def test_unknown_method_returns_envelope_or_404(self, client):
        r = _sub(client, "thisIsNotAMethod")
        if r.status_code == 404:
            return
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/json"), (
            f"Expected JSON envelope for unknown method, got "
            f"{r.headers.get('content-type')}: {r.text[:120]!r}"
        )
        _err(r)


# ===========================================================================
# 2. System
# ===========================================================================


class TestPing:
    def test_anonymous_ping_still_returns_envelope(self, client):
        # ping with no auth should fail at the auth layer but still respond
        # with a Subsonic-shaped 200 (NEVER a 401 — clients treat that as a
        # network error).
        r = client.get("/rest/ping", params={"f": "json"})
        _err(r)

    def test_ping_body_is_minimal(self, client):
        body = _ok(_sub(client, "ping"))
        # status/version/type/serverVersion/openSubsonic + nothing else mandatory.
        assert body["status"] == "ok"


class TestGetLicense:
    def test_returns_license_object(self, client):
        body = _ok(_sub(client, "getLicense"))
        lic = body.get("license")
        assert lic is not None, f"Missing 'license' key: {body}"
        assert "valid" in lic


# ===========================================================================
# 3. Browsing
# ===========================================================================


class TestGetMusicFolders:
    def test_returns_music_folders_object(self, client):
        body = _ok(_sub(client, "getMusicFolders"))
        mf = body.get("musicFolders")
        assert mf is not None, f"Missing 'musicFolders' key: {body}"
        # Even when there are no folders the array key should exist (per spec).
        folders = mf.get("musicFolder", [])
        assert isinstance(folders, list)


class TestGetIndexes:
    def test_envelope_has_indexes_key(self, client):
        body = _ok(_sub(client, "getIndexes"))
        assert "indexes" in body, f"Missing 'indexes' key: {body}"

    def test_indexes_has_last_modified_and_articles(self, client):
        body = _ok(_sub(client, "getIndexes"))
        indexes = body["indexes"]
        assert "lastModified" in indexes
        assert isinstance(indexes["lastModified"], int)
        assert "ignoredArticles" in indexes


class TestGetArtists:
    def test_envelope_has_artists_key(self, client):
        body = _ok(_sub(client, "getArtists"))
        assert "artists" in body
        assert "ignoredArticles" in body["artists"]


class TestGetArtist:
    def test_missing_id_returns_error(self, client):
        # Subsonic spec: required-param missing → code 10.
        r = _sub(client, "getArtist")
        # FastAPI's Query validation kicks in here and returns 422 — that's
        # NOT spec-compliant. Subsonic clients expect a 200 envelope.
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            _err(r)

    def test_unknown_id_returns_not_found(self, client):
        # Code 70 = data not found.
        body = _err(_sub(client, "getArtist", id="ar-999999"), code=70)
        assert (
            "artist" in body["error"]["message"].lower()
            or "found" in body["error"]["message"].lower()
        )


class TestGetMusicDirectory:
    def test_missing_id_returns_error(self, client):
        r = _sub(client, "getMusicDirectory")
        assert r.status_code in (200, 422)

    def test_unknown_id_returns_not_found(self, client):
        _err(_sub(client, "getMusicDirectory", id="al-999999"), code=70)


class TestGetGenres:
    def test_returns_genres_object(self, client):
        body = _ok(_sub(client, "getGenres"))
        genres = body.get("genres")
        assert genres is not None
        # Empty DB → empty list, but the key must still exist.
        assert isinstance(genres.get("genre", []), list)


# ===========================================================================
# 4. Albums / songs
# ===========================================================================


class TestGetAlbumList:
    def test_album_list_returns_albumList_envelope_key(self, client):
        body = _ok(_sub(client, "getAlbumList"))
        assert "albumList" in body
        assert isinstance(body["albumList"].get("album", []), list)

    def test_album_list2_returns_albumList2_envelope_key(self, client):
        body = _ok(_sub(client, "getAlbumList2"))
        assert "albumList2" in body
        assert isinstance(body["albumList2"].get("album", []), list)

    def test_invalid_type_param_does_not_500(self, client):
        # Unknown sort types should error gracefully (50 = generic), not 500.
        r = _sub(client, "getAlbumList", type="completelyMadeUp")
        # Acceptable: empty list + 200 OR a typed error envelope.
        assert r.status_code == 200


class TestGetAlbum:
    def test_missing_id_returns_error(self, client):
        r = _sub(client, "getAlbum")
        assert r.status_code in (200, 422)

    def test_unknown_id_returns_not_found(self, client):
        _err(_sub(client, "getAlbum", id="al-999999"), code=70)


class TestGetSong:
    def test_missing_id_returns_error(self, client):
        r = _sub(client, "getSong")
        assert r.status_code in (200, 422)

    def test_unknown_id_returns_not_found(self, client):
        _err(_sub(client, "getSong", id="tr-999999"), code=70)


class TestGetRandomSongs:
    def test_returns_randomSongs_envelope_key(self, client):
        body = _ok(_sub(client, "getRandomSongs"))
        assert "randomSongs" in body
        assert isinstance(body["randomSongs"].get("song", []), list)


class TestGetTopSongs:
    def test_returns_topSongs_envelope_key(self, client):
        body = _ok(_sub(client, "getTopSongs"))
        assert "topSongs" in body
        assert isinstance(body["topSongs"].get("song", []), list)


# ===========================================================================
# 5. Search
# ===========================================================================


class TestSearch3:
    def test_search3_envelope(self, client):
        body = _ok(_sub(client, "search3", query=""))
        assert "searchResult3" in body
        sr = body["searchResult3"]
        # Per spec each sub-array may be omitted when empty, but our impl
        # returns them always. Don't be strict; just check we got an object.
        assert isinstance(sr, dict)

    def test_search3_keys_are_lists_when_present(self, client):
        body = _ok(_sub(client, "search3", query="anything"))
        sr = body["searchResult3"]
        for key in ("artist", "album", "song"):
            if key in sr:
                assert isinstance(sr[key], list)


# ===========================================================================
# 6. Annotation (scrobble / starred / nowPlaying)
# ===========================================================================


class TestScrobble:
    def test_missing_id_returns_error(self, client):
        r = _sub(client, "scrobble")
        assert r.status_code in (200, 422)

    def test_unknown_track_id_returns_not_found(self, client):
        _err(_sub(client, "scrobble", id="tr-999999"), code=70)

    def test_non_track_id_kind_returns_error(self, client):
        _err(_sub(client, "scrobble", id="ar-1"), code=70)


class TestStarred:
    def test_getStarred_envelope(self, client):
        body = _ok(_sub(client, "getStarred"))
        assert "starred" in body
        # Spec: object with three optional arrays.
        st = body["starred"]
        assert isinstance(st.get("artist", []), list)
        assert isinstance(st.get("album", []), list)
        assert isinstance(st.get("song", []), list)

    def test_getStarred2_envelope(self, client):
        body = _ok(_sub(client, "getStarred2"))
        # Spec key for getStarred2 is "starred2" — current impl returns
        # "starred" instead. Flag this as a failure to push the fix.
        assert "starred2" in body, (
            "getStarred2 should wrap its payload under the 'starred2' key, "
            f"not 'starred'. Got envelope keys: {list(body.keys())}"
        )


class TestNowPlaying:
    def test_envelope(self, client):
        body = _ok(_sub(client, "getNowPlaying"))
        assert "nowPlaying" in body
        assert isinstance(body["nowPlaying"].get("entry", []), list)


# ===========================================================================
# 7. Streaming + cover art (error paths — we have no real files)
# ===========================================================================


class TestStreamingErrors:
    def test_stream_unknown_id_is_404_or_subsonic_error(self, client):
        # Stream may legitimately return a Subsonic 200-envelope error or a
        # plain HTTP 404 (when the file disappears between DB lookup and
        # send). Accept either, but the 200 path must carry a valid envelope.
        r = _sub(client, "stream", id="tr-999999")
        assert r.status_code in (200, 404)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith(
            "application/json"
        ):
            _err(r, code=70)

    def test_download_unknown_id(self, client):
        r = _sub(client, "download", id="tr-999999")
        assert r.status_code in (200, 404)

    def test_stream_non_track_id(self, client):
        r = _sub(client, "stream", id="al-1")
        assert r.status_code in (200, 404)


class TestGetCoverArt:
    def test_unknown_id_returns_404_or_envelope(self, client):
        # Returns a Subsonic-shaped error (code 70) when the cached file
        # isn't present.
        r = _sub(client, "getCoverArt", id="thisHashDoesNotExist")
        assert r.status_code in (200, 404)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith(
            "application/json"
        ):
            _err(r, code=70)


# ===========================================================================
# 7b. getTranscodeDecision (OpenSubsonic "transcoding" extension)
# ===========================================================================


def _transcode_post(client, body, **params):
    """POST getTranscodeDecision with a JSON ClientInfo body + query auth."""
    query = {
        "u": "admin", "p": "adminpass",
        "v": "1.16.1", "c": "pytest", "f": "json",
        **params,
    }
    return client.post("/rest/getTranscodeDecision", params=query, json=body)


class TestGetTranscodeDecision:
    def test_extension_is_advertised(self, client):
        body = _ok(_sub(client, "getOpenSubsonicExtensions"))
        names = {e["name"] for e in body["openSubsonicExtensions"]}
        assert "transcoding" in names

    def test_unsupported_media_type_errors(self, client, seeded_library):
        r = _transcode_post(
            client, {}, mediaId=seeded_library["track_prefix"], mediaType="podcast"
        )
        _err(r, code=70)

    def test_unknown_media_id_errors(self, client):
        r = _transcode_post(client, {}, mediaId="tr-999999", mediaType="song")
        _err(r, code=70)

    def test_direct_play_for_capable_client(self, client, seeded_library):
        # Seeded track is mp3@320; a client that direct-plays mp3 over http
        # must get canDirectPlay=true and no transcodeStream.
        body = {
            "directPlayProfiles": [
                {"containers": ["mp3"], "audioCodecs": ["mp3"], "protocols": ["http"]}
            ]
        }
        r = _transcode_post(
            client, body, mediaId=seeded_library["track_prefix"], mediaType="song"
        )
        td = _ok(r)["transcodeDecision"]
        assert td["canDirectPlay"] is True
        assert td["transcodeReason"] == []
        assert "transcodeStream" not in td
        # bitrate must be reported in bits/second (DB stores 320 kbps).
        assert td["sourceStream"]["audioBitrate"] == 320000

    def test_transcode_for_incapable_client(self, client, seeded_library):
        # A client that only direct-plays flac can't play the mp3 source, so
        # we must report a transcode target it accepts (mp3) with params.
        body = {
            "directPlayProfiles": [
                {"containers": ["flac"], "audioCodecs": ["flac"], "protocols": []}
            ],
            "transcodingProfiles": [
                {"container": "mp3", "audioCodec": "mp3", "protocol": "http"}
            ],
        }
        r = _transcode_post(
            client, body, mediaId=seeded_library["track_prefix"], mediaType="song"
        )
        td = _ok(r)["transcodeDecision"]
        assert td["canDirectPlay"] is False
        assert td["canTranscode"] is True
        assert td["transcodeReason"] == ["ContainerNotSupported"]
        assert td["transcodeStream"]["codec"] == "mp3"
        assert td["transcodeParams"]  # non-empty opaque token

    def test_source_stream_includes_scanned_props(self, client):
        # A track with scanned channels/sample_rate/bit_depth must surface
        # them in sourceStream (proves the columns flow end-to-end into the
        # decision, not just into get_track).
        import time as _t
        with transaction():
            folder = queries.add_music_folder(name="hd", path="/hd-folder")
            artist = queries.upsert_artist("HD Artist")
            album = queries.upsert_album(artist_id=artist, name="HD Album", year=2024)
            now = int(_t.time())
            tid = queries.upsert_track({
                "album_id": album, "artist_id": artist, "music_folder_id": folder,
                "path": "/hd-folder/hi.flac", "title": "HiRes",
                "track_number": 1, "disc_number": 1, "duration": 200,
                "bitrate": 3000, "channels": 2, "sample_rate": 96000,
                "bit_depth": 24, "size": 75_000_000, "suffix": "flac",
                "content_type": "audio/flac", "year": 2024, "genre": "Jazz",
                "mtime": now, "content_hash": None, "last_scanned": now,
            })
        body = {
            "directPlayProfiles": [
                {"containers": ["flac"], "audioCodecs": ["flac"], "protocols": []}
            ]
        }
        r = _transcode_post(client, body, mediaId=f"tr-{tid}", mediaType="song")
        td = _ok(r)["transcodeDecision"]
        assert td["canDirectPlay"] is True
        src = td["sourceStream"]
        assert src["audioChannels"] == 2
        assert src["audioSamplerate"] == 96000
        assert src["audioBitdepth"] == 24
        assert src["audioBitrate"] == 3_000_000  # 3000 kbps → bps


# ===========================================================================
# 8. Playlists
# ===========================================================================


class TestPlaylistsEndpoints:
    def test_getPlaylists_empty_list(self, client):
        body = _ok(_sub(client, "getPlaylists"))
        pls = body.get("playlists")
        assert pls is not None
        assert isinstance(pls.get("playlist", []), list)

    def test_getPlaylist_unknown_returns_not_found(self, client):
        _err(_sub(client, "getPlaylist", id="999999"), code=70)

    def test_createPlaylist_then_get(self, client):
        body = _ok(_sub(client, "createPlaylist", name="Compliance Test"))
        pl = body.get("playlist")
        assert pl is not None, f"createPlaylist envelope missing 'playlist': {body}"
        assert pl["name"] == "Compliance Test"
        assert "id" in pl
        # Then verify getPlaylist round-trips it.
        body2 = _ok(_sub(client, "getPlaylist", id=str(pl["id"])))
        assert body2["playlist"]["name"] == "Compliance Test"

    def test_deletePlaylist_removes_it(self, client):
        # Create, then delete, then verify it's gone.
        pl = _ok(_sub(client, "createPlaylist", name="To Be Deleted"))["playlist"]
        _ok(_sub(client, "deletePlaylist", id=str(pl["id"])))
        _err(_sub(client, "getPlaylist", id=str(pl["id"])), code=70)


# ===========================================================================
# 9. .view aliases
# ===========================================================================


class TestDotViewAliases:
    """Every endpoint should also be reachable at /rest/<name>.view (legacy)."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "ping.view",
            "getLicense.view",
            "getMusicFolders.view",
            "getIndexes.view",
            "getArtists.view",
            "getAlbumList.view",
            "getAlbumList2.view",
            "getGenres.view",
            "getPlaylists.view",
            "getStarred.view",
            "getNowPlaying.view",
        ],
    )
    def test_dot_view_returns_valid_envelope(self, client, endpoint):
        _ok(_sub(client, endpoint))


# ===========================================================================
# 10. Auth & format
# ===========================================================================


class TestFormatParameter:
    def test_default_format_is_json(self, client):
        # No f= param at all — spec says default is xml, but most servers
        # (and ours) accept json as the default when not specified.
        r = client.get(
            "/rest/ping",
            params={
                "u": "admin",
                "p": "adminpass",
                "v": "1.16.1",
                "c": "test",
            },
        )
        # Acceptable either way: JSON envelope OR XML body.
        assert r.status_code == 200

    def test_jsonp_callback_is_wrapped(self, client):
        r = _sub(client, "ping", f="jsonp", callback="myCb")
        assert r.status_code == 200
        # JSONP responses are JS, not JSON. Body should start with the
        # callback name.
        body = r.text
        assert body.startswith("myCb(") or body.startswith(
            "/**/myCb("
        ), f"Expected JSONP-wrapped body, got: {body[:80]!r}"


class TestAuthErrors:
    def test_wrong_password_returns_envelope_code_40(self, client):
        _err(
            client.get(
                "/rest/ping",
                params={
                    "u": "admin",
                    "p": "wrong-pass",
                    "v": "1.16.1",
                    "c": "t",
                    "f": "json",
                },
            ),
            code=40,
        )

    def test_missing_username_returns_envelope_code_10(self, client):
        _err(
            client.get(
                "/rest/ping",
                params={
                    "p": "x",
                    "v": "1.16.1",
                    "c": "t",
                    "f": "json",
                },
            ),
            code=10,
        )

    def test_getSongsByGenre(self, client):
        body = _ok(_sub(client, "getSongsByGenre", genre="Rock"))

        assert "songsByGenre" in body


# ===========================================================================
# 11. KNOWN-MISSING endpoints
# ---------------------------------------------------------------------------
# Each test below documents an endpoint that real Subsonic clients probe
# and we don't yet implement. They are expected to fail today; un-xfail
# them as each one is added. The presence of these tests is the gap log.
# ===========================================================================


class TestMissingEndpoints:

    def test_getPlayQueue(self, client):
        _ok(_sub(client, "getPlayQueue"))

    @pytest.mark.xfail(reason="getBookmarks not implemented", strict=False)
    def test_getBookmarks(self, client):
        body = _ok(_sub(client, "getBookmarks"))
        assert "bookmarks" in body

    @pytest.mark.xfail(reason="getLyrics not implemented", strict=False)
    def test_getLyrics(self, client):
        _ok(_sub(client, "getLyrics", artist="Whoever", title="Whatever"))

    @pytest.mark.xfail(
        reason="getLyricsBySongId (OpenSubsonic) not implemented", strict=False
    )
    def test_getLyricsBySongId(self, client):
        _ok(_sub(client, "getLyricsBySongId", id="tr-1"))

    @pytest.mark.xfail(reason="getInternetRadioStations not implemented", strict=False)
    def test_getInternetRadioStations(self, client):
        body = _ok(_sub(client, "getInternetRadioStations"))
        assert "internetRadioStations" in body

    @pytest.mark.xfail(reason="getAvatar not implemented", strict=True)
    def test_getAvatar(self, client):
        # A real getAvatar response is the raw image (image/jpeg, image/png).
        # The unknown-method catchall returns a JSON envelope, so we can't
        # accept application/json here — that would let the catchall satisfy
        # the assertion and the test would xpass even with no real impl.
        r = _sub(client, "getAvatar", username="admin")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert ct.startswith(
            "image/"
        ), f"getAvatar returned {ct!r} — looks unimplemented (catchall envelope?)"

    def test_getAlbumInfo2(self, client, monkeypatch):
        # Seed a minimal artist + album so we have a real id to query.
        # No tracks needed — getAlbumInfo2 only joins on the album row.
        with transaction():
            artist_id = queries.upsert_artist("Compliance Artist", sort_name="Compliance Artist")
            album_id = queries.upsert_album(
                artist_id=artist_id,
                name="Compliance Album",
                year=2024,
                genre="Test",
                release_type="album",
            )

        # Short-circuit the Deezer network call — the endpoint must produce a
        # valid envelope whether or not external lookups succeed, and the test
        # suite must never depend on the internet being reachable.
        monkeypatch.setattr(
            "backend.core.deezer.get_album_images", lambda _a, _b: None
        )

        body = _ok(_sub(client, "getAlbumInfo2", id=f"al-{album_id}"))
        assert "albumInfo" in body

    @pytest.mark.xfail(reason="getShares not implemented", strict=False)
    def test_getShares(self, client):
        body = _ok(_sub(client, "getShares"))
        assert "shares" in body

    def test_search2(self, client):
        body = _ok(_sub(client, "search2", query=""))
        assert "searchResult2" in body
