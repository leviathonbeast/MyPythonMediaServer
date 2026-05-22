"""
Lyrics: LRC parsing (backend.core.lyrics) + sidecar .lrc extraction.

The parser is pure, so it's tested directly with strings. The sidecar test
exercises metadata.extract() against a real file on disk — no valid audio is
needed because we only assert the .lrc handling.
"""

from __future__ import annotations

from backend.core import lyrics
from backend.scanner import metadata


# ===========================================================================
# LRC parser
# ===========================================================================


class TestLrcParser:
    def test_plain_text_is_unsynced(self):
        parsed = lyrics.parse("first line\nsecond line")
        assert parsed.synced is False
        assert [ln["value"] for ln in parsed.lines] == ["first line", "second line"]
        assert all("start" not in ln for ln in parsed.lines)

    def test_basic_synced(self):
        parsed = lyrics.parse("[00:12.34]hello\n[00:15.00]world")
        assert parsed.synced is True
        assert parsed.lines[0] == {"start": 12340, "value": "hello"}
        assert parsed.lines[1] == {"start": 15000, "value": "world"}

    def test_fraction_precision(self):
        # 2-digit = centiseconds, 3-digit = ms, 1-digit = tenths, none = 0.
        assert lyrics.parse("[00:01.5]a").lines[0]["start"] == 1500
        assert lyrics.parse("[00:01.05]a").lines[0]["start"] == 1050
        assert lyrics.parse("[00:01.345]a").lines[0]["start"] == 1345
        assert lyrics.parse("[00:01]a").lines[0]["start"] == 1000
        assert lyrics.parse("[01:00]a").lines[0]["start"] == 60000

    def test_multiple_timestamps_expand_and_sort(self):
        # A repeated chorus: one text, two times — emitted twice, time-ordered.
        parsed = lyrics.parse("[00:50.00][00:10.00]chorus")
        assert parsed.synced is True
        assert [ln["start"] for ln in parsed.lines] == [10000, 50000]
        assert all(ln["value"] == "chorus" for ln in parsed.lines)

    def test_offset_tag_reported_not_applied(self):
        parsed = lyrics.parse("[offset:+250]\n[00:10.00]line")
        assert parsed.offset == 250
        assert parsed.lines[0]["start"] == 10000  # offset NOT baked in

    def test_metadata_tags_stripped_and_dropped(self):
        parsed = lyrics.parse("[ar:Some Artist]\n[ti:Song]\n[00:01.00]real line")
        # Pure-metadata lines are not lyrics.
        assert parsed.lines == [{"start": 1000, "value": "real line"}]

    def test_to_plain_text_strips_timestamps(self):
        text = "[ar:X]\n[00:01.00]one\n[00:02.00]two"
        assert lyrics.to_plain_text(text) == "one\ntwo"

    def test_empty_and_none(self):
        assert lyrics.parse(None).lines == []
        assert lyrics.parse("").lines == []
        assert lyrics.parse(None).synced is False
        assert lyrics.to_plain_text(None) == ""


# ===========================================================================
# Sidecar .lrc extraction
# ===========================================================================


class TestSidecarLyrics:
    def test_sidecar_lrc_is_read(self, tmp_path):
        audio = tmp_path / "01 - Song.mp3"
        audio.write_bytes(b"not really audio")
        (tmp_path / "01 - Song.lrc").write_text(
            "[00:01.00]synced line", encoding="utf-8"
        )
        meta = metadata.extract(str(audio))
        assert meta.lyrics == "[00:01.00]synced line"

    def test_no_sidecar_leaves_lyrics_none(self, tmp_path):
        audio = tmp_path / "02 - Other.mp3"
        audio.write_bytes(b"not really audio")
        meta = metadata.extract(str(audio))
        assert meta.lyrics is None

    def test_bom_is_stripped(self, tmp_path):
        audio = tmp_path / "03 - Bom.mp3"
        audio.write_bytes(b"not really audio")
        (tmp_path / "03 - Bom.lrc").write_text(
            "plain words", encoding="utf-8-sig"  # writes a BOM
        )
        meta = metadata.extract(str(audio))
        assert meta.lyrics == "plain words"  # no leading
