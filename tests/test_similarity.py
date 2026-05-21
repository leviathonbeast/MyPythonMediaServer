"""
Tests for the sonic-similarity math (backend/core/similarity.py) and the
analysis pass (backend/core/sonic_analysis.py).

The math is exercised against synthetic vectors — no audio files needed — with
negative controls (missing id, empty library, zero-variance dimension) so the
ranking is proven to depend on the data, not to always return the same thing.

The analysis pass is tested with `extract_features` monkeypatched to a fixed
vector, so we verify the orchestration (selection query, storage, incremental
vs force, progress/failure counting) without decoding real audio.
"""

from __future__ import annotations

import time

from backend.core import similarity as s
from backend.core import sonic_analysis
from backend.db import queries, transaction


# Synthetic library: two tight clusters in 3-space.
_DATA = [
    (10, [0.0, 0.0, 0.0]),
    (20, [0.1, 0.0, 0.0]),   # near 10
    (30, [9.0, 9.0, 9.0]),
    (40, [9.1, 9.0, 9.0]),   # near 30
]


# ---------------------------------------------------------------------------
# find_similar
# ---------------------------------------------------------------------------


class TestFindSimilar:
    def test_nearest_first_and_excludes_self(self):
        res = s.find_similar(_DATA, 10, 3)
        assert all(tid != 10 for tid, _ in res)
        assert res[0][0] == 20  # nearest neighbour ranked first
        # scores descending
        assert res == sorted(res, key=lambda x: x[1], reverse=True)

    def test_respects_count(self):
        assert len(s.find_similar(_DATA, 10, 1)) == 1

    def test_missing_id_returns_empty(self):
        assert s.find_similar(_DATA, 999, 3) == []

    def test_empty_library_returns_empty(self):
        assert s.find_similar([], 10, 3) == []

    def test_zero_count_returns_empty(self):
        assert s.find_similar(_DATA, 10, 0) == []

    def test_zero_variance_dim_no_nan(self):
        # A constant 3rd dim must not produce NaN scores (div-by-zero guard).
        data = [(1, [0.0, 1.0, 7.0]), (2, [2.0, 3.0, 7.0]), (3, [5.0, 1.0, 7.0])]
        res = s.find_similar(data, 1, 5)
        assert all(score == score for _, score in res)  # NaN != NaN


# ---------------------------------------------------------------------------
# find_path
# ---------------------------------------------------------------------------


class TestFindPath:
    def test_pins_endpoints_and_orders(self):
        p = s.find_path(_DATA, 10, 30, 4)
        assert p[0][0] == 10
        assert p[-1][0] == 30
        assert p[0][1] == 1.0  # start scored against itself

    def test_length_capped_by_count(self):
        assert len(s.find_path(_DATA, 10, 30, 3)) == 3

    def test_shorter_when_library_too_small(self):
        # Only 3 tracks but count=10 → path can't exceed start+middle+end.
        small = [(1, [0.0, 0.0]), (2, [1.0, 1.0]), (3, [2.0, 2.0])]
        p = s.find_path(small, 1, 3, 10)
        assert p[0][0] == 1 and p[-1][0] == 3
        assert len(p) == 3  # start, the one middle, end — no duplicates

    def test_same_start_and_end(self):
        assert s.find_path(_DATA, 10, 10, 5) == [(10, 1.0)]

    def test_missing_endpoint_returns_empty(self):
        assert s.find_path(_DATA, 10, 999, 4) == []

    def test_no_duplicate_tracks(self):
        ids = [tid for tid, _ in s.find_path(_DATA, 10, 30, 4)]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Analysis pass (extract_features monkeypatched)
# ---------------------------------------------------------------------------


def _seed_two_tracks():
    with transaction():
        folder = queries.add_music_folder(name="an", path="/an-folder")
        artist = queries.upsert_artist("An Artist")
        album = queries.upsert_album(artist_id=artist, name="An Album", year=2024)
        now = int(time.time())

        def row(path):
            return {
                "album_id": album, "artist_id": artist, "music_folder_id": folder,
                "path": path, "title": path, "track_number": 1, "disc_number": 1,
                "duration": 180, "bitrate": 320, "size": 1, "suffix": "mp3",
                "content_type": "audio/mpeg", "year": 2024, "genre": "x",
                "mtime": now, "content_hash": None, "last_scanned": now,
            }

        t1 = queries.upsert_track(row("/an-folder/a.mp3"))
        t2 = queries.upsert_track(row("/an-folder/b.mp3"))
    return t1, t2


class TestAnalysisPass:
    def test_populates_features(self, client, monkeypatch):
        t1, t2 = _seed_two_tracks()
        monkeypatch.setattr(
            sonic_analysis, "extract_features", lambda path: [1.0] * s.EXPECTED_DIMS
        )
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.analyzed == 2
        assert prog.failed == 0
        assert queries.get_track_features(t1) == [1.0] * s.EXPECTED_DIMS
        assert queries.get_track_features(t2) == [1.0] * s.EXPECTED_DIMS

    def test_incremental_skips_already_analyzed(self, client, monkeypatch):
        _seed_two_tracks()
        monkeypatch.setattr(
            sonic_analysis, "extract_features", lambda path: [1.0] * s.EXPECTED_DIMS
        )
        sonic_analysis.analyze_all_blocking(force=False)
        # Second incremental run: nothing left to do.
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.total == 0
        assert prog.analyzed == 0

    def test_force_reanalyzes_everything(self, client, monkeypatch):
        _seed_two_tracks()
        monkeypatch.setattr(
            sonic_analysis, "extract_features", lambda path: [1.0] * s.EXPECTED_DIMS
        )
        sonic_analysis.analyze_all_blocking(force=False)
        prog = sonic_analysis.analyze_all_blocking(force=True)
        assert prog.total == 2
        assert prog.analyzed == 2

    def test_failed_extraction_counted_not_stored(self, client, monkeypatch):
        t1, t2 = _seed_two_tracks()
        # First track extracts, second fails (None).
        monkeypatch.setattr(
            sonic_analysis, "extract_features",
            lambda path: [1.0] * s.EXPECTED_DIMS if path.endswith("a.mp3") else None,
        )
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.analyzed == 1
        assert prog.failed == 1
        assert queries.get_track_features(t1) is not None
        assert queries.get_track_features(t2) is None
