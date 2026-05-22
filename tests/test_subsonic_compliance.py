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
# 7c. getSonicSimilarTracks + findSonicPath ("sonicSimilarity" extension)
# ===========================================================================


def _seed_tracks_with_features(n, vectors):
    """Seed n tracks (one folder/artist/album) and store the given feature
    vectors. Returns the list of track ids in order."""
    import time as _t
    ids = []
    with transaction():
        folder = queries.add_music_folder(name="sonic", path="/sonic-dir")
        artist = queries.upsert_artist("Sonic Artist")
        album = queries.upsert_album(artist_id=artist, name="Sonic Album", year=2024)
        now = int(_t.time())
        for i in range(n):
            ids.append(queries.upsert_track({
                "album_id": album, "artist_id": artist, "music_folder_id": folder,
                "path": f"/sonic-dir/{i}.mp3", "title": f"Song {i}", "track_number": i,
                "disc_number": 1, "duration": 180, "bitrate": 320, "size": 1,
                "suffix": "mp3", "content_type": "audio/mpeg", "year": 2024,
                "genre": "x", "mtime": now, "content_hash": None, "last_scanned": now,
            }))
        for tid, vec in zip(ids, vectors):
            queries.upsert_track_features(tid, vec, 1)
    return ids


class TestSonicSimilarity:
    def test_extension_advertised(self, client):
        names = {e["name"] for e in _ok(_sub(client, "getOpenSubsonicExtensions"))["openSubsonicExtensions"]}
        assert "sonicSimilarity" in names

    def test_similar_returns_nearest_excluding_self(self, client):
        # Track 0 sits next to track 1; track 2 is far away.
        ids = _seed_tracks_with_features(3, [[0.0, 0.0], [0.1, 0.0], [9.0, 9.0]])
        body = _ok(_sub(client, "getSonicSimilarTracks", id=f"tr-{ids[0]}", count=5))
        matches = body["sonicMatch"]
        returned = [m["entry"]["id"] for m in matches]
        assert f"tr-{ids[0]}" not in returned          # self excluded
        assert returned[0] == f"tr-{ids[1]}"            # nearest first
        assert all("similarity" in m for m in matches)  # flat {entry, similarity}

    def test_similar_empty_when_no_features(self, client, seeded_library):
        # Seeded track has no feature vector → empty sonicMatch, still ok envelope.
        body = _ok(_sub(client, "getSonicSimilarTracks", id=seeded_library["track_prefix"]))
        assert body["sonicMatch"] == []

    def test_similar_bad_id_errors(self, client):
        _err(_sub(client, "getSonicSimilarTracks", id="al-1"), code=70)

    def test_path_pins_endpoints(self, client):
        ids = _seed_tracks_with_features(
            4, [[0.0, 0.0], [3.0, 3.0], [6.0, 6.0], [9.0, 9.0]]
        )
        body = _ok(_sub(
            client, "findSonicPath",
            startSongId=f"tr-{ids[0]}", endSongId=f"tr-{ids[3]}", count=4,
        ))
        path = [m["entry"]["id"] for m in body["sonicMatch"]]
        assert path[0] == f"tr-{ids[0]}"
        assert path[-1] == f"tr-{ids[3]}"
        assert body["sonicMatch"][0]["similarity"] == 1.0  # start vs itself


# ===========================================================================
# 7d. getSimilarSongs + getSimilarSongs2 (core Subsonic "artist radio")
# ===========================================================================


class TestSimilarSongs:
    def test_song_seed_returns_seed_then_neighbours(self, client):
        # Track 0 sits next to track 1; track 2 is far away.
        ids = _seed_tracks_with_features(3, [[0.0, 0.0], [0.1, 0.0], [9.0, 9.0]])
        body = _ok(_sub(client, "getSimilarSongs", id=f"tr-{ids[0]}", count=5))
        songs = body["similarSongs"]["song"]
        assert songs, "expected a non-empty song list"
        assert songs[0]["id"] == f"tr-{ids[0]}"   # seed prepended first
        assert songs[1]["id"] == f"tr-{ids[1]}"   # nearest neighbour next
        assert len(songs) <= 5

    def test_artist_seed_id3_form(self, client):
        # getSimilarSongs2 takes an artist id and seeds from a random track of theirs.
        ids = _seed_tracks_with_features(3, [[0.0, 0.0], [0.1, 0.0], [9.0, 9.0]])
        artist_id = queries.get_track(ids[0])["artist_id"]
        body = _ok(_sub(client, "getSimilarSongs2", id=f"ar-{artist_id}", count=10))
        songs = body["similarSongs2"]["song"]
        assert songs, "analysed artist should yield a radio queue"
        assert all(s["id"].startswith("tr-") for s in songs)

    def test_empty_when_no_features(self, client, seeded_library):
        # Seeded track/artist have no feature vectors → empty list, ok envelope.
        body = _ok(_sub(client, "getSimilarSongs", id=seeded_library["track_prefix"]))
        assert body["similarSongs"]["song"] == []
        body2 = _ok(_sub(client, "getSimilarSongs2", id=seeded_library["artist_prefix"]))
        assert body2["similarSongs2"]["song"] == []

    def test_unknown_id_errors(self, client):
        _err(_sub(client, "getSimilarSongs", id="tr-999999"), code=70)
        _err(_sub(client, "getSimilarSongs2", id="ar-999999"), code=70)
        _err(_sub(client, "getSimilarSongs2", id="not-an-id"), code=70)

    def test_count_cap(self, client):
        ids = _seed_tracks_with_features(5, [[float(i), 0.0] for i in range(5)])
        body = _ok(_sub(client, "getSimilarSongs", id=f"tr-{ids[0]}", count=2))
        songs = body["similarSongs"]["song"]
        assert len(songs) <= 2
        assert songs[0]["id"] == f"tr-{ids[0]}"   # seed always first


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
# 10b. Lyrics, bookmarks, internet radio, ratings, tokenInfo
# ===========================================================================


def _seed_track_with_lyrics(lyrics: str | None):
    """Seed one artist/album/track, optionally storing lyrics. Returns the
    Subsonic ids the endpoints take."""
    with transaction():
        folder = queries.add_music_folder(name="lyr", path="/lyr-dir")
        artist = queries.upsert_artist("Lyric Artist")
        album = queries.upsert_album(artist_id=artist, name="Lyric Album", year=2024)
        now = 1_700_000_000
        tid = queries.upsert_track({
            "album_id": album, "artist_id": artist, "music_folder_id": folder,
            "path": "/lyr-dir/song.mp3", "title": "Lyric Song", "track_number": 1,
            "disc_number": 1, "duration": 200, "bitrate": 320, "size": 1,
            "suffix": "mp3", "content_type": "audio/mpeg", "year": 2024,
            "genre": "x", "mtime": now, "content_hash": None, "last_scanned": now,
            "lyrics": lyrics,
        })
    return {
        "track": f"tr-{tid}", "album": f"al-{album}", "artist": f"ar-{artist}",
    }


class TestLyrics:
    def test_lyrics_by_song_id_returns_structured_lines(self, client):
        ids = _seed_track_with_lyrics("line one\nline two\nline three")
        body = _ok(_sub(client, "getLyricsBySongId", id=ids["track"]))
        blocks = body["lyricsList"]["structuredLyrics"]
        assert len(blocks) == 1
        block = blocks[0]
        assert block["synced"] is False
        assert [ln["value"] for ln in block["line"]] == ["line one", "line two", "line three"]
        assert block["displayTitle"] == "Lyric Song"

    def test_lyrics_by_song_id_empty_when_none(self, client):
        ids = _seed_track_with_lyrics(None)
        body = _ok(_sub(client, "getLyricsBySongId", id=ids["track"]))
        assert body["lyricsList"]["structuredLyrics"] == []

    def test_lyrics_by_song_id_synced_lrc(self, client):
        # An LRC blob (embedded or from a .lrc sidecar) comes back synced, with
        # per-line millisecond start times.
        ids = _seed_track_with_lyrics("[00:01.00]first\n[00:03.50]second")
        body = _ok(_sub(client, "getLyricsBySongId", id=ids["track"]))
        block = body["lyricsList"]["structuredLyrics"][0]
        assert block["synced"] is True
        assert block["line"][0] == {"start": 1000, "value": "first"}
        assert block["line"][1] == {"start": 3500, "value": "second"}

    def test_get_lyrics_strips_timestamps(self, client):
        # The legacy getLyrics is plain-text only — LRC tags must be gone.
        _seed_track_with_lyrics("[00:01.00]alpha\n[00:02.00]beta")
        body = _ok(_sub(client, "getLyrics", artist="Lyric Artist", title="Lyric Song"))
        assert body["lyrics"]["value"] == "alpha\nbeta"

    def test_lyrics_by_song_id_bad_id_errors(self, client):
        _err(_sub(client, "getLyricsBySongId", id="al-1"), code=70)

    def test_get_lyrics_by_name(self, client):
        _seed_track_with_lyrics("hello world")
        body = _ok(_sub(client, "getLyrics", artist="Lyric Artist", title="Lyric Song"))
        assert body["lyrics"]["value"] == "hello world"
        assert body["lyrics"]["artist"] == "Lyric Artist"

    def test_get_lyrics_miss_is_empty_not_error(self, client):
        body = _ok(_sub(client, "getLyrics", artist="Nobody", title="Nothing"))
        assert body["lyrics"]["value"] == ""

    def test_songLyrics_extension_advertised(self, client):
        names = {e["name"] for e in _ok(_sub(client, "getOpenSubsonicExtensions"))["openSubsonicExtensions"]}
        assert "songLyrics" in names


class TestBookmarks:
    def test_create_list_delete_roundtrip(self, client, seeded_library):
        tid = seeded_library["track_prefix"]
        _ok(_sub(client, "createBookmark", id=tid, position=42000, comment="here"))

        body = _ok(_sub(client, "getBookmarks"))
        marks = body["bookmarks"]["bookmark"]
        assert len(marks) == 1
        assert marks[0]["position"] == 42000
        assert marks[0]["comment"] == "here"
        assert marks[0]["entry"]["id"] == tid
        assert marks[0]["username"] == "admin"

        _ok(_sub(client, "deleteBookmark", id=tid))
        assert _ok(_sub(client, "getBookmarks"))["bookmarks"]["bookmark"] == []

    def test_create_moves_existing(self, client, seeded_library):
        tid = seeded_library["track_prefix"]
        _ok(_sub(client, "createBookmark", id=tid, position=1000))
        _ok(_sub(client, "createBookmark", id=tid, position=9000))
        marks = _ok(_sub(client, "getBookmarks"))["bookmarks"]["bookmark"]
        assert len(marks) == 1            # upsert, not duplicate
        assert marks[0]["position"] == 9000

    def test_create_bad_id_errors(self, client):
        _err(_sub(client, "createBookmark", id="tr-999999", position=0), code=70)

    def test_bookmarks_are_per_user(self, client, seeded_library):
        # admin's bookmark must not show up for the regular user.
        _ok(_sub(client, "createBookmark", id=seeded_library["track_prefix"], position=5))
        body = _ok(_sub(client, "getBookmarks", admin=False))
        assert body["bookmarks"]["bookmark"] == []


class TestInternetRadio:
    def test_create_list_update_delete(self, client):
        _ok(_sub(client, "createInternetRadioStation",
                 streamUrl="http://stream/1", name="Radio One",
                 homepageUrl="http://home/1"))
        stations = _ok(_sub(client, "getInternetRadioStations"))["internetRadioStations"]["internetRadioStation"]
        assert len(stations) == 1
        sid = stations[0]["id"]
        assert stations[0]["name"] == "Radio One"
        assert stations[0]["streamUrl"] == "http://stream/1"
        assert stations[0]["homePageUrl"] == "http://home/1"

        _ok(_sub(client, "updateInternetRadioStation",
                 id=sid, streamUrl="http://stream/2", name="Radio Two"))
        again = _ok(_sub(client, "getInternetRadioStations"))["internetRadioStations"]["internetRadioStation"]
        assert again[0]["name"] == "Radio Two"
        assert again[0]["streamUrl"] == "http://stream/2"

        _ok(_sub(client, "deleteInternetRadioStation", id=sid))
        empty = _ok(_sub(client, "getInternetRadioStations"))["internetRadioStations"]["internetRadioStation"]
        assert empty == []

    def test_update_unknown_id_errors(self, client):
        _err(_sub(client, "updateInternetRadioStation",
                  id="999999", streamUrl="x", name="y"), code=70)

    def test_delete_unknown_id_errors(self, client):
        _err(_sub(client, "deleteInternetRadioStation", id="999999"), code=70)


class TestSetRating:
    def test_rate_track_surfaces_on_getSong(self, client, seeded_library):
        tid = seeded_library["track_prefix"]
        _ok(_sub(client, "setRating", id=tid, rating=4))
        song = _ok(_sub(client, "getSong", id=tid))["song"]
        assert song["userRating"] == 4
        assert song["averageRating"] == 4.0

    def test_rate_album_and_artist(self, client, seeded_library):
        _ok(_sub(client, "setRating", id=seeded_library["album_prefix"], rating=5))
        _ok(_sub(client, "setRating", id=seeded_library["artist_prefix"], rating=3))
        album = _ok(_sub(client, "getAlbum", id=seeded_library["album_prefix"]))["album"]
        artist = _ok(_sub(client, "getArtist", id=seeded_library["artist_prefix"]))["artist"]
        assert album["userRating"] == 5
        assert artist["userRating"] == 3

    def test_rating_zero_removes(self, client, seeded_library):
        tid = seeded_library["track_prefix"]
        _ok(_sub(client, "setRating", id=tid, rating=2))
        _ok(_sub(client, "setRating", id=tid, rating=0))
        song = _ok(_sub(client, "getSong", id=tid))["song"]
        assert "userRating" not in song      # cleared

    def test_rating_out_of_range_errors(self, client, seeded_library):
        _err(_sub(client, "setRating", id=seeded_library["track_prefix"], rating=9), code=10)

    def test_rating_bad_id_errors(self, client):
        _err(_sub(client, "setRating", id="tr-999999", rating=3), code=70)


class TestTokenInfo:
    def test_returns_username(self, client):
        body = _ok(_sub(client, "tokenInfo"))
        assert body["tokenInfo"]["username"] == "admin"


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

    # getBookmarks / getLyrics / getInternetRadioStations are now implemented —
    # they return a well-formed (empty) envelope even on an empty library.
    # Behavioural coverage lives in TestLyrics / TestBookmarks / TestInternetRadio.
    def test_getBookmarks(self, client):
        body = _ok(_sub(client, "getBookmarks"))
        assert "bookmarks" in body

    def test_getLyrics(self, client):
        body = _ok(_sub(client, "getLyrics", artist="Whoever", title="Whatever"))
        assert "lyrics" in body

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
