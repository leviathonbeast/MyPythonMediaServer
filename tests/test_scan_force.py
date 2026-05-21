"""
Scanner force-rescan tests.

The normal scan fast-path skips files whose (mtime, size) is unchanged, which
means columns added after the original scan would never be backfilled on an
existing library. `force=True` must bypass that skip and re-parse every file.

We verify this with a dummy on-disk file — content validity doesn't matter
because metadata.extract() never raises and the skip keys on stat(), not on
the bytes. The negative control (a normal rescan skips the same file) proves
the force path is actually doing something, not just always re-parsing.
"""

from __future__ import annotations

from backend.db import queries, transaction
from backend.scanner import scan_all_blocking


def _make_library(tmp_path):
    """Create a one-file music folder and register it. Returns the dir."""
    music = tmp_path / "music"
    music.mkdir()
    (music / "song.mp3").write_bytes(b"\x00" * 1024)
    with transaction():
        queries.add_music_folder(name="forcetest", path=str(music))
    return music


class TestForceRescan:
    def test_unchanged_file_skipped_without_force(self, client, tmp_path):
        _make_library(tmp_path)

        first = scan_all_blocking()
        assert first.files_added == 1

        # Negative control: a second normal scan must skip the untouched file.
        second = scan_all_blocking()
        assert second.files_skipped == 1
        assert second.files_updated == 0

    def test_force_reparses_unchanged_file(self, client, tmp_path):
        _make_library(tmp_path)
        scan_all_blocking()  # initial add

        forced = scan_all_blocking(force=True)
        assert forced.files_skipped == 0
        assert forced.files_updated == 1
