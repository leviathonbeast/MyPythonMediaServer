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
import types

import numpy as np
import pytest

from backend.api.subsonic.helpers import find_similar_deduped
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
# Excerpt loading: libsndfile fast path vs ffmpeg fallback.
#
# These are hermetic — soundfile.SoundFile and subprocess.run are mocked, so
# the tests assert the *routing* and command construction without needing real
# audio files or ffmpeg installed.
# ---------------------------------------------------------------------------


class _FakeSoundFile:
    """Minimal stand-in for soundfile.SoundFile usable as a context manager.

    `read_error` makes .read() raise — the real-world case where libsndfile
    opens the header fine but can't decode the frames.
    """

    def __init__(self, frames, samplerate, data=None, read_error=None):
        self.frames = frames
        self.samplerate = samplerate
        self._data = data
        self._read_error = read_error
        self.seeked_to = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def seek(self, n):
        self.seeked_to = n

    def read(self, frames, dtype, always_2d):
        if self._read_error is not None:
            raise self._read_error
        return self._data


class TestExcerptLoading:
    def test_soundfile_readable_decodes_without_ffmpeg(self, monkeypatch):
        """When libsndfile can decode the file we use the soundfile path and
        never touch ffmpeg. Using sr == analysis SR keeps the returned samples
        equal to what was read (no resample), so we can assert on them."""
        data = np.linspace(-0.5, 0.5, 4410, dtype=np.float32)  # mono, 1-D
        fake = _FakeSoundFile(frames=4410, samplerate=s._ANALYSIS_SR, data=data)
        monkeypatch.setattr(s.sf, "SoundFile", lambda p: fake)
        monkeypatch.setattr(
            s, "_ffmpeg_load_excerpt",
            lambda *a, **k: pytest.fail("ffmpeg used for a decodable file"),
        )

        y, sr = s._load_excerpt("song.flac", 60.0)
        assert sr == s._ANALYSIS_SR
        assert np.array_equal(y, data)

    def test_soundfile_open_failure_falls_back_to_ffmpeg(self, monkeypatch):
        """libsndfile can't even open AAC/M4A/WMA → route to ffmpeg, not
        librosa's deprecated audioread path."""
        def boom(_p):
            raise RuntimeError("libsndfile cannot open")

        monkeypatch.setattr(s.sf, "SoundFile", boom)
        seen = {}
        monkeypatch.setattr(
            s, "_ffmpeg_load_excerpt",
            lambda path, sec: (
                seen.update(path=path, sec=sec),
                (np.zeros(3, np.float32), s._ANALYSIS_SR),
            )[1],
        )
        y, sr = s._load_excerpt("song.m4a", 45.0)
        assert seen == {"path": "song.m4a", "sec": 45.0}
        assert sr == s._ANALYSIS_SR

    def test_soundfile_decode_failure_falls_back_to_ffmpeg(self, monkeypatch):
        """Header opens but frames won't decode (partial/odd FLAC) — the exact
        case that used to slip through to audioread. Must reach ffmpeg."""
        fake = _FakeSoundFile(
            frames=44100, samplerate=44100, read_error=RuntimeError("frame decode failed")
        )
        monkeypatch.setattr(s.sf, "SoundFile", lambda p: fake)
        hit = {}
        monkeypatch.setattr(
            s, "_ffmpeg_load_excerpt",
            lambda path, sec: (hit.setdefault("yes", True), (np.zeros(2, np.float32), s._ANALYSIS_SR))[1],
        )
        y, sr = s._load_excerpt("partial.flac", 60.0)
        assert hit.get("yes") is True
        assert sr == s._ANALYSIS_SR

    def test_ffmpeg_decode_parses_f32le_and_builds_command(self, monkeypatch):
        """The ffmpeg fallback streams raw float32 PCM; we read it straight into
        a numpy array and the command requests mono f32le at the analysis SR."""
        samples = np.array([0.1, -0.2, 0.3, 0.0], dtype=np.float32)
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout=samples.tobytes(), stderr=b"")

        monkeypatch.setattr(s.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "backend.config.get_settings",
            lambda: types.SimpleNamespace(ffmpeg_binary="ffmpeg"),
        )

        y, sr = s._ffmpeg_load_excerpt("song.m4a", 30.0)
        assert sr == s._ANALYSIS_SR
        assert np.array_equal(y, samples)
        assert y.flags.writeable  # copied out of the read-only stdout buffer
        cmd = captured["cmd"]
        assert cmd[0] == "ffmpeg"
        assert "song.m4a" in cmd
        assert cmd[cmd.index("-ar") + 1] == str(s._ANALYSIS_SR)
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-f") + 1] == "f32le"

    def test_ffmpeg_nonzero_exit_raises(self, monkeypatch):
        """A failed decode raises (so extract_features counts it as failed and
        logs the path) rather than returning silent garbage."""
        monkeypatch.setattr(
            s.subprocess, "run",
            lambda cmd, **kw: types.SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"moov atom not found\n"
            ),
        )
        monkeypatch.setattr(
            "backend.config.get_settings",
            lambda: types.SimpleNamespace(ffmpeg_binary="ffmpeg"),
        )
        with pytest.raises(RuntimeError):
            s._ffmpeg_load_excerpt("broken.m4a", 30.0)


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
# find_similar de-duplication (key_for) — keep duplicate recordings out of radio
# ---------------------------------------------------------------------------


class TestFindSimilarDedup:
    # Five tracks; 1 is the query. 5 is a duplicate of the query's song (key A),
    # 2 and 3 are duplicates of each other (key B), 4 is its own song (key C).
    _DUP = [
        (1, [0.0, 0.0]),
        (5, [0.02, 0.0]),   # duplicate of the query song
        (2, [1.0, 0.0]),
        (3, [1.02, 0.0]),   # duplicate of 2
        (4, [5.0, 5.0]),
    ]
    _KEYS = {1: "A", 5: "A", 2: "B", 3: "B", 4: "C"}

    def _key_for(self, tid):
        return self._KEYS.get(tid)

    def test_collapses_duplicates_and_excludes_query_song(self):
        res = s.find_similar(self._DUP, 1, 10, key_for=self._key_for)
        ids = [tid for tid, _ in res]
        keys = [self._KEYS[t] for t in ids]
        assert len(keys) == len(set(keys))   # no logical song repeated
        assert 5 not in ids                  # the query's own duplicate is dropped
        assert "A" not in keys               # query's key never reappears
        assert set(keys) == {"B", "C"}

    def test_keeps_the_higher_ranked_of_a_duplicate_group(self):
        full = [tid for tid, _ in s.find_similar(self._DUP, 1, 10)]
        deduped = [tid for tid, _ in s.find_similar(self._DUP, 1, 10, key_for=self._key_for)]
        first_b = next(t for t in full if self._KEYS[t] == "B")
        other_b = ({2, 3} - {first_b}).pop()
        assert first_b in deduped
        assert other_b not in deduped

    def test_none_key_tracks_are_never_treated_as_duplicates(self):
        data = [(1, [0.0, 0.0]), (2, [1.0, 0.0]), (3, [1.01, 0.0])]
        keys = {1: "A", 2: None, 3: None}
        res = s.find_similar(data, 1, 10, key_for=lambda t: keys[t])
        assert {tid for tid, _ in res} == {2, 3}

    def test_without_key_for_duplicates_are_kept(self):
        # Back-compat: no key_for → original behaviour, duplicates included.
        ids = {tid for tid, _ in s.find_similar(self._DUP, 1, 10)}
        assert {2, 3, 5} <= ids


# ---------------------------------------------------------------------------
# find_similar_deduped — the API helper that keys real track rows by MBID /
# artist+title (DB-backed).
# ---------------------------------------------------------------------------


class TestFindSimilarDedupedHelper:
    def _seed(self):
        """Seed a folder of tracks including duplicate recordings:
          - the seed song, plus a second file of it (same artist+title)
          - 'One', plus a second file of it (same artist+title, no MBID)
          - an MBID-tagged track, plus another file sharing that MBID
        """
        D = s.EXPECTED_DIMS
        with transaction():
            folder = queries.add_music_folder(name="d", path="/d")
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
                queries.upsert_track_features(tid, [fill] * D, s.FEATURE_VERSION)
                return tid

            return {
                "seed":    mk("/d/seed.flac",  "X", "Seed", 0.00),
                "seeddup": mk("/d/seed2.flac", "X", "Seed", 0.01),  # dup of seed
                "one":     mk("/d/one.flac",   "Y", "One",  0.10),
                "onedup":  mk("/d/one2.flac",  "Y", "One",  0.11),  # dup of one
                "mb":      mk("/d/mb.flac",    "Z", "MB A",  0.20, mbid="rec-1"),
                "mbdup":   mk("/d/mb2.flac",   "Z", "MB B",  0.21, mbid="rec-1"),  # dup by MBID
            }

    def test_radio_contains_no_duplicate_songs(self, client):
        ids = self._seed()
        got = [tid for tid, _ in find_similar_deduped(ids["seed"], 50)]

        # The seed and its duplicate file never appear in its own radio.
        assert ids["seed"] not in got
        assert ids["seeddup"] not in got
        # Exactly one of each duplicate pair survives — by artist+title…
        assert (ids["one"] in got) ^ (ids["onedup"] in got)
        # …and by shared recording MBID (even though their titles differ).
        assert (ids["mb"] in got) ^ (ids["mbdup"] in got)


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
            sonic_analysis, "extract_features",
            # The pass calls extract_features(path, excerpt_seconds); accept
            # and ignore the second arg.
            lambda path, excerpt_seconds=None: [1.0] * s.EXPECTED_DIMS,
        )
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.analyzed == 2
        assert prog.failed == 0
        assert queries.get_track_features(t1) == [1.0] * s.EXPECTED_DIMS
        assert queries.get_track_features(t2) == [1.0] * s.EXPECTED_DIMS

    def test_incremental_skips_already_analyzed(self, client, monkeypatch):
        _seed_two_tracks()
        monkeypatch.setattr(
            sonic_analysis, "extract_features",
            # The pass calls extract_features(path, excerpt_seconds); accept
            # and ignore the second arg.
            lambda path, excerpt_seconds=None: [1.0] * s.EXPECTED_DIMS,
        )
        sonic_analysis.analyze_all_blocking(force=False)
        # Second incremental run: nothing left to do.
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.total == 0
        assert prog.analyzed == 0

    def test_force_reanalyzes_everything(self, client, monkeypatch):
        _seed_two_tracks()
        monkeypatch.setattr(
            sonic_analysis, "extract_features",
            # The pass calls extract_features(path, excerpt_seconds); accept
            # and ignore the second arg.
            lambda path, excerpt_seconds=None: [1.0] * s.EXPECTED_DIMS,
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
            lambda path, excerpt_seconds=None: (
                [1.0] * s.EXPECTED_DIMS if path.endswith("a.mp3") else None
            ),
        )
        prog = sonic_analysis.analyze_all_blocking(force=False)
        assert prog.analyzed == 1
        assert prog.failed == 1
        assert queries.get_track_features(t1) is not None
        assert queries.get_track_features(t2) is None
