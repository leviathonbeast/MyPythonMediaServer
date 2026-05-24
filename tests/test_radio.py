"""
Tests for the endless-queue continuation (autoplay radio).

backend.core.radio.continue_from turns "what was played recently" into fresh
tracks to append, reusing the sonic-similarity engine and the logical-song
de-duplication. These tests pin the behaviour that matters for the feature:

  * the seed tracks and anything already queued are never returned,
  * duplicate recordings (same artist+title, or same MBID) collapse to one,
  * an un-analysed library yields nothing rather than erroring,
  * the HTTP endpoint requires auth and returns song objects the player can
    enqueue directly.

Vectors are filled per-track so each track has a distinct fingerprint; the
exact nearest ordering isn't asserted (these are membership/dedup tests), so
the assertions don't depend on the standardisation geometry.
"""

from __future__ import annotations

import time

from backend.core import radio
from backend.core import similarity as s
from backend.core.library import make_track_id
from backend.db import queries, transaction


def _seed_library():
    """Seed a folder of analysed tracks, including duplicate recordings.

    Returns a dict of name -> track_id:
        seed              the track we seed the radio from
        near, near_dup    same artist+title (a duplicate recording)
        other             a distinct song
        far               another distinct song
        mb, mb_dup        two files sharing one recording MBID (titles differ)
    """
    D = s.EXPECTED_DIMS
    with transaction():
        folder = queries.add_music_folder(name="r", path="/r")
        now = int(time.time())

        def mk(path, artist_name, title, fill, mbid=None):
            artist = queries.upsert_artist(artist_name)
            tid = queries.upsert_track({
                "album_id": None, "artist_id": artist, "music_folder_id": folder,
                "path": path, "title": title, "track_number": 1, "disc_number": 1,
                "duration": 180, "bitrate": 320, "size": 1, "suffix": "flac",
                "content_type": "audio/flac", "year": 2024, "genre": "x",
                "mtime": now, "content_hash": None, "last_scanned": now,
                "musicbrainz_id": mbid,
            })
            # Distinct fingerprint per track: a ramp offset by `fill`.
            queries.upsert_track_features(
                tid, [fill + i * 0.001 for i in range(D)], s.FEATURE_VERSION
            )
            return tid

        return {
            "seed":     mk("/r/seed.flac",  "A1", "Seed",  0.00),
            "near":     mk("/r/near.flac",  "A2", "Near",  0.10),
            "near_dup": mk("/r/near2.flac", "A2", "Near",  0.11),  # dup of near
            "other":    mk("/r/other.flac", "A3", "Other", 0.20),
            "far":      mk("/r/far.flac",   "A4", "Far",   9.00),
            "mb":       mk("/r/mb.flac",    "A5", "MB A",  0.30, mbid="rec-1"),
            "mb_dup":   mk("/r/mb2.flac",   "A5", "MB B",  0.31, mbid="rec-1"),
        }


class TestContinueFrom:
    def test_excludes_seed_and_collapses_duplicates(self, client):
        ids = _seed_library()
        rows = radio.continue_from([ids["seed"]], [ids["seed"]], 10)
        got = [r["id"] for r in rows]

        assert ids["seed"] not in got                 # the seed is never returned
        assert len(got) == len(set(got))              # no id repeated
        assert (ids["near"] in got) ^ (ids["near_dup"] in got)   # one copy only
        assert (ids["mb"] in got) ^ (ids["mb_dup"] in got)       # MBID dup collapsed
        assert ids["other"] in got and ids["far"] in got

    def test_excludes_already_queued(self, client):
        ids = _seed_library()
        exclude = [ids["seed"], ids["other"], ids["far"]]
        got = [r["id"] for r in radio.continue_from([ids["seed"]], exclude, 10)]
        assert ids["other"] not in got
        assert ids["far"] not in got

    def test_excludes_a_duplicate_of_an_already_queued_song(self, client):
        # 'near' is in the queue; its duplicate 'near_dup' must not be offered
        # as a "new" track.
        ids = _seed_library()
        exclude = [ids["seed"], ids["near"]]
        got = [r["id"] for r in radio.continue_from([ids["seed"]], exclude, 10)]
        assert ids["near"] not in got
        assert ids["near_dup"] not in got

    def test_respects_count(self, client):
        ids = _seed_library()
        got = radio.continue_from([ids["seed"]], [ids["seed"]], 2)
        assert len(got) == 2

    def test_empty_when_library_not_analysed(self, client):
        # Tracks exist but have no feature vectors → nothing to suggest.
        with transaction():
            folder = queries.add_music_folder(name="u", path="/u")
            artist = queries.upsert_artist("Una")
            now = int(time.time())
            tid = queries.upsert_track({
                "album_id": None, "artist_id": artist, "music_folder_id": folder,
                "path": "/u/x.flac", "title": "X", "track_number": 1,
                "disc_number": 1, "duration": 180, "bitrate": 320, "size": 1,
                "suffix": "flac", "content_type": "audio/flac", "year": 2024,
                "genre": "x", "mtime": now, "content_hash": None,
                "last_scanned": now,
            })
        assert radio.continue_from([tid], [tid], 10) == []

    def test_no_seeds_returns_empty(self, client):
        _seed_library()
        assert radio.continue_from([], [], 10) == []


class TestRadioContinueEndpoint:
    def test_returns_songs_excluding_seed(self, client, user_headers):
        ids = _seed_library()
        seed = make_track_id(ids["seed"])
        resp = client.post(
            "/api/radio/continue",
            json={"seed_ids": [seed], "exclude_ids": [seed], "count": 10},
            headers=user_headers,
        )
        assert resp.status_code == 200, resp.text
        songs = resp.json()["songs"]
        returned = {song["id"] for song in songs}
        assert seed not in returned
        # Song objects are the enqueue-ready Subsonic shape.
        assert all("id" in song and "title" in song for song in songs)
        # Duplicate recordings still collapsed end-to-end.
        assert not ({make_track_id(ids["near"]), make_track_id(ids["near_dup"])} <= returned)

    def test_requires_auth(self, client):
        resp = client.post(
            "/api/radio/continue",
            json={"seed_ids": [], "exclude_ids": [], "count": 5},
        )
        assert resp.status_code in (401, 403)
