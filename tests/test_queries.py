"""
Direct unit tests for backend/db/queries.py.

Exercises the SQL layer without going through FastAPI so regressions in
the rewritten hot-path queries get caught here first.

Covers the rewrites that motivated this file:
  - list_albums frequent/recent CTE-based play aggregation
  - list_artists_indexed windowed cover-art pick
  - list_song_by_genre ORDER BY id + music_folder_id filter
  - library_stats single round-trip aggregation
  - upsert_artist/album/track RETURNING idempotency
  - cleanup_empty_albums_and_artists NOT EXISTS pruning
  - update_album_aggregates / update_artist_aggregates
  - normalize_sort_name behavior
"""

from __future__ import annotations

import time

import pytest

from backend.db import queries, transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track_row(
    *,
    path: str,
    title: str = "Song",
    album_id: int | None = None,
    artist_id: int | None = None,
    music_folder_id: int | None = None,
    duration: int = 180,
    genre: str | None = "Rock",
    year: int = 2024,
) -> dict:
    """Build a fully-populated track dict for upsert_track.

    Defaults keep tests terse; override only the fields a test cares about.
    """
    now = int(time.time())
    return {
        "album_id":        album_id,
        "artist_id":       artist_id,
        "music_folder_id": music_folder_id,
        "path":            path,
        "title":           title,
        "track_number":    1,
        "disc_number":     1,
        "duration":        duration,
        "bitrate":         320,
        "size":            7_200_000,
        "suffix":          "mp3",
        "content_type":    "audio/mpeg",
        "year":            year,
        "genre":           genre,
        "mtime":           now,
        "content_hash":    None,
        "last_scanned":    now,
    }


# ===========================================================================
# normalize_sort_name
# ===========================================================================


class TestNormalizeSortName:
    def test_strips_leading_the(self):
        assert queries.normalize_sort_name("The Wall") == "Wall"

    def test_strips_leading_a(self):
        assert queries.normalize_sort_name("A Day in the Life") == "Day in the Life"

    def test_strips_leading_an(self):
        assert queries.normalize_sort_name("An Evening Out") == "Evening Out"

    def test_article_case_insensitive(self):
        assert queries.normalize_sort_name("THE WALL") == "WALL"

    def test_symbols_prefixed_with_tilde_to_sort_last(self):
        # The whole point: '$' should sort after 'Z', not before 'A'.
        assert queries.normalize_sort_name("$ome $exy $ongs").startswith("~")

    def test_alphanumeric_left_alone(self):
        assert queries.normalize_sort_name("Wall") == "Wall"


# ===========================================================================
# Upsert RETURNING idempotency
# ===========================================================================


class TestUpsertArtist:
    def test_same_name_returns_same_id(self, client):
        with transaction():
            a = queries.upsert_artist("Daft Punk")
            b = queries.upsert_artist("Daft Punk")
        assert a == b

    def test_case_insensitive_dedup(self, client):
        """The name_lower constraint must merge 'AC/DC' with 'ac/dc'."""
        with transaction():
            a = queries.upsert_artist("AC/DC")
            b = queries.upsert_artist("ac/dc")
        assert a == b


class TestUpsertAlbum:
    def test_same_artist_and_name_returns_same_id(self, client):
        with transaction():
            artist = queries.upsert_artist("Artist X")
            a = queries.upsert_album(artist_id=artist, name="Album X", year=2020)
            b = queries.upsert_album(artist_id=artist, name="Album X", year=2020)
        assert a == b

    def test_null_year_filled_in_on_second_upsert(self, client):
        """COALESCE in DO UPDATE preserves the first non-NULL value."""
        with transaction():
            artist = queries.upsert_artist("Artist Y")
            first = queries.upsert_album(artist_id=artist, name="Album Y", year=None)
            second = queries.upsert_album(artist_id=artist, name="Album Y", year=2020)
        assert first == second
        assert queries.get_album(first)["year"] == 2020

    def test_existing_year_not_clobbered_by_null(self, client):
        """A later upsert with year=None must not erase the previously-set year."""
        with transaction():
            artist = queries.upsert_artist("Artist Z")
            first = queries.upsert_album(artist_id=artist, name="Album Z", year=2020)
            second = queries.upsert_album(artist_id=artist, name="Album Z", year=None)
        assert first == second
        assert queries.get_album(first)["year"] == 2020


class TestUpsertTrack:
    def test_returning_matches_lookup_id(self, client, seeded_library):
        """RETURNING id must match a follow-up SELECT by path.

        Uses named binding (`:path`) — `?` positional binding works on
        SQLite but not on psycopg, and the rest of the codebase has
        long since standardised on named bindings anyway.
        """
        row = queries.get_conn().execute(
            "SELECT id FROM tracks WHERE path = :path",
            {"path": "/test/fixtures/music/song.mp3"},
        ).fetchone()
        assert row["id"] == seeded_library["track_id"]

    def test_same_path_updates_in_place(self, client, seeded_library):
        with transaction():
            new_id = queries.upsert_track(_track_row(
                path="/test/fixtures/music/song.mp3",
                title="Updated Title",
                artist_id=seeded_library["artist_id"],
                album_id=seeded_library["album_id"],
                music_folder_id=seeded_library["folder_id"],
            ))
        assert new_id == seeded_library["track_id"]
        assert queries.get_track(new_id)["title"] == "Updated Title"


# ===========================================================================
# list_albums frequent / recent CTE
# ===========================================================================


class TestListAlbumsFrequentRecent:
    """The CTE must order albums by total plays (frequent) or last-played (recent)."""

    @pytest.fixture()
    def seeded(self, client):
        """Two albums; one gets 5 play_count rows, the other none."""
        with transaction():
            folder = queries.add_music_folder(name="f", path="/freq-folder")
            artist = queries.upsert_artist("Frequent Artist")
            popular = queries.upsert_album(artist_id=artist, name="Popular", year=2024)
            unpopular = queries.upsert_album(artist_id=artist, name="Unpopular", year=2024)
            pt = queries.upsert_track(_track_row(
                path="/freq-folder/p.mp3", title="Pop",
                artist_id=artist, album_id=popular, music_folder_id=folder,
            ))
            queries.upsert_track(_track_row(
                path="/freq-folder/u.mp3", title="Unpop",
                artist_id=artist, album_id=unpopular, music_folder_id=folder,
            ))
            queries.update_album_aggregates(popular)
            queries.update_album_aggregates(unpopular)
            queries.update_artist_aggregates(artist)
            admin = queries.get_user_by_username("admin")
            for _ in range(5):
                queries.play_count(admin["id"], pt)
        return {"popular": popular, "unpopular": unpopular}

    def test_frequent_puts_most_played_first(self, client, seeded):
        order = [a["id"] for a in queries.list_albums(list_type="frequent", size=10)]
        assert order.index(seeded["popular"]) < order.index(seeded["unpopular"])

    def test_recent_puts_most_recently_played_first(self, client, seeded):
        order = [a["id"] for a in queries.list_albums(list_type="recent", size=10)]
        assert order.index(seeded["popular"]) < order.index(seeded["unpopular"])

    def test_unplayed_albums_still_returned(self, client, seeded):
        """LEFT JOIN against the CTE keeps zero-play albums in the result."""
        ids = {a["id"] for a in queries.list_albums(list_type="frequent", size=100)}
        assert seeded["unpopular"] in ids


# ===========================================================================
# list_artists_indexed (windowed cover-art pick)
# ===========================================================================


class TestListArtistsIndexed:
    def test_cover_art_id_picks_newest_album(self, client):
        """ROW_NUMBER() OVER (... ORDER BY year DESC) must select the newest cover."""
        with transaction():
            artist = queries.upsert_artist("Window Artist")
            old = queries.upsert_album(artist_id=artist, name="Old Album", year=2000)
            new = queries.upsert_album(artist_id=artist, name="New Album", year=2024)
            queries.set_album_cover_art(old, "cover-old-hash")
            queries.set_album_cover_art(new, "cover-new-hash")
            queries.update_artist_aggregates(artist)

        found = None
        for artists in queries.list_artists_indexed().values():
            for a in artists:
                if a["id"] == artist:
                    found = a
                    break
        assert found is not None, "artist should appear in indexed result"
        assert found["coverArtId"] == "cover-new-hash"

    def test_artists_without_albums_are_excluded(self, client):
        """The WHERE album_count > 0 filter must drop empty artists."""
        with transaction():
            queries.upsert_artist("No Albums Artist")
            # No upsert_album / no update_artist_aggregates → album_count stays 0.
        flat = [a for arts in queries.list_artists_indexed().values() for a in arts]
        assert all(a["name"] != "No Albums Artist" for a in flat)

    def test_symbol_artist_lands_in_hash_bucket(self, client):
        """sort_name starting with '~' (symbol-prefixed) goes to '#'."""
        with transaction():
            artist = queries.upsert_artist("$ymbolic", sort_name="~$ymbolic")
            album = queries.upsert_album(artist_id=artist, name="Album", year=2024)
            queries.update_artist_aggregates(artist)
        indexed = queries.list_artists_indexed()
        assert any(a["id"] == artist for a in indexed.get("#", []))


# ===========================================================================
# Extended track stream properties (channels / sample_rate / bit_depth)
# ===========================================================================


class TestTrackStreamProps:
    def test_upsert_and_get_roundtrip(self, client):
        with transaction():
            folder = queries.add_music_folder(name="sp", path="/sp-folder")
            artist = queries.upsert_artist("SP Artist")
            album = queries.upsert_album(artist_id=artist, name="SP Album", year=2024)
            row = _track_row(
                path="/sp/song.flac", artist_id=artist, album_id=album,
                music_folder_id=folder,
            )
            row.update({
                "channels": 2, "sample_rate": 96000, "bit_depth": 24,
                "suffix": "flac", "content_type": "audio/flac",
            })
            tid = queries.upsert_track(row)
        t = queries.get_track(tid)
        assert t["channels"] == 2
        assert t["sample_rate"] == 96000
        assert t["bit_depth"] == 24

    def test_upsert_without_stream_props_defaults_null(self, client):
        # Legacy callers (and tests) omit the keys entirely; upsert must not
        # raise on the missing named placeholders and must store NULL.
        with transaction():
            folder = queries.add_music_folder(name="sp2", path="/sp2-folder")
            artist = queries.upsert_artist("SP2 Artist")
            album = queries.upsert_album(artist_id=artist, name="SP2 Album", year=2024)
            tid = queries.upsert_track(_track_row(
                path="/sp2/song.mp3", artist_id=artist, album_id=album,
                music_folder_id=folder,
            ))
        t = queries.get_track(tid)
        assert t["channels"] is None
        assert t["sample_rate"] is None
        assert t["bit_depth"] is None


class TestTrackToSubsonicStreamProps:
    def test_emits_when_present(self):
        from backend.core.library import track_to_subsonic
        out = track_to_subsonic(
            {"id": 1, "title": "x", "channels": 2, "sample_rate": 48000, "bit_depth": 16}
        )
        assert out["channelCount"] == 2
        assert out["samplingRate"] == 48000
        assert out["bitDepth"] == 16

    def test_omits_when_absent(self):
        from backend.core.library import track_to_subsonic
        out = track_to_subsonic({"id": 1, "title": "x"})
        assert "channelCount" not in out
        assert "samplingRate" not in out
        assert "bitDepth" not in out

    def test_omits_zero_bitdepth_for_lossy(self):
        # bit_depth 0/None (lossy formats) must be omitted, not sent as 0.
        from backend.core.library import track_to_subsonic
        out = track_to_subsonic(
            {"id": 1, "title": "x", "channels": 2, "sample_rate": 44100, "bit_depth": 0}
        )
        assert out["channelCount"] == 2
        assert "bitDepth" not in out


# ===========================================================================
# get_artist_cover_art_id (getCoverArt ar-N resolution)
# ===========================================================================


class TestGetArtistCoverArtId:
    def test_picks_newest_album_with_cover(self, client):
        with transaction():
            artist = queries.upsert_artist("Cover Artist")
            old = queries.upsert_album(artist_id=artist, name="Old", year=2001)
            new = queries.upsert_album(artist_id=artist, name="New", year=2023)
            queries.set_album_cover_art(old, "0000000000000001")
            queries.set_album_cover_art(new, "0000000000000002")
        assert queries.get_artist_cover_art_id(artist) == "0000000000000002"

    def test_skips_albums_without_cover(self, client):
        """An artist whose newest album has no cover falls back to an
        older album that does — NULL covers must not win the ORDER BY."""
        with transaction():
            artist = queries.upsert_artist("Partial Cover Artist")
            queries.upsert_album(artist_id=artist, name="Newer No Cover", year=2024)
            older = queries.upsert_album(artist_id=artist, name="Older", year=2010)
            queries.set_album_cover_art(older, "00000000000000aa")
        assert queries.get_artist_cover_art_id(artist) == "00000000000000aa"

    def test_returns_none_when_no_covers(self, client):
        with transaction():
            artist = queries.upsert_artist("Naked Artist")
            queries.upsert_album(artist_id=artist, name="Bare", year=2024)
        assert queries.get_artist_cover_art_id(artist) is None


# ===========================================================================
# _resolve_cover_art_id (getCoverArt prefixed-id → hash resolution)
# ===========================================================================


class TestResolveCoverArtId:
    """getCoverArt must accept both the content hash and prefixed entity
    ids, because getStarred emits al-/ar- coverArt and many clients pass a
    song id straight through. Regression for clients (e.g. Feishin/Arpeggi)
    that 404'd on tr- ids before resolution was added."""

    def test_track_id_resolves_to_album_cover(self, client):
        from backend.api.subsonic.albums import _resolve_cover_art_id

        with transaction():
            artist = queries.upsert_artist("Res Artist")
            album = queries.upsert_album(artist_id=artist, name="Res Album", year=2024)
            queries.set_album_cover_art(album, "00000000000000bb")
            folder = queries.add_music_folder(name="res", path="/res-folder")
            track = queries.upsert_track(_track_row(
                path="/res/track.mp3", artist_id=artist, album_id=album,
                music_folder_id=folder,
            ))
        assert _resolve_cover_art_id(f"tr-{track}") == "00000000000000bb"

    def test_album_id_resolves_to_its_cover(self, client):
        from backend.api.subsonic.albums import _resolve_cover_art_id

        with transaction():
            artist = queries.upsert_artist("Alb Artist")
            album = queries.upsert_album(artist_id=artist, name="Alb", year=2024)
            queries.set_album_cover_art(album, "00000000000000cc")
        assert _resolve_cover_art_id(f"al-{album}") == "00000000000000cc"

    def test_artist_id_resolves_to_representative_cover(self, client):
        from backend.api.subsonic.albums import _resolve_cover_art_id

        with transaction():
            artist = queries.upsert_artist("Art Artist")
            album = queries.upsert_album(artist_id=artist, name="A", year=2024)
            queries.set_album_cover_art(album, "00000000000000dd")
        assert _resolve_cover_art_id(f"ar-{artist}") == "00000000000000dd"

    def test_bare_hash_passes_through_unchanged(self, client):
        from backend.api.subsonic.albums import _resolve_cover_art_id

        # No recognised prefix → assumed to already be a content hash.
        assert _resolve_cover_art_id("0487ee2dffeb6a9f") == "0487ee2dffeb6a9f"

    def test_unknown_track_id_resolves_to_none(self, client):
        from backend.api.subsonic.albums import _resolve_cover_art_id

        assert _resolve_cover_art_id("tr-999999") is None


# ===========================================================================
# list_song_by_genre
# ===========================================================================


class TestListSongByGenre:
    def test_filters_by_genre(self, client):
        with transaction():
            folder = queries.add_music_folder(name="g", path="/genre-folder")
            artist = queries.upsert_artist("Genre Artist")
            album = queries.upsert_album(artist_id=artist, name="Mix", year=2024)
            rock = queries.upsert_track(_track_row(
                path="/genre-folder/rock.mp3",
                artist_id=artist, album_id=album, music_folder_id=folder, genre="Rock",
            ))
            jazz = queries.upsert_track(_track_row(
                path="/genre-folder/jazz.mp3",
                artist_id=artist, album_id=album, music_folder_id=folder, genre="Jazz",
            ))

        assert {t["id"] for t in queries.list_song_by_genre("Rock", 10, 0)} == {rock}
        assert {t["id"] for t in queries.list_song_by_genre("Jazz", 10, 0)} == {jazz}

    def test_pagination_pages_do_not_overlap(self, client):
        """ORDER BY t.id was added so OFFSET pagination is stable."""
        with transaction():
            folder = queries.add_music_folder(name="p", path="/page-folder")
            artist = queries.upsert_artist("Page Artist")
            album = queries.upsert_album(artist_id=artist, name="Pages", year=2024)
            for i in range(5):
                queries.upsert_track(_track_row(
                    path=f"/page-folder/{i}.mp3", title=f"t{i}",
                    artist_id=artist, album_id=album, music_folder_id=folder,
                    genre="Pagey",
                ))

        page1 = {t["id"] for t in queries.list_song_by_genre("Pagey", 2, 0)}
        page2 = {t["id"] for t in queries.list_song_by_genre("Pagey", 2, 2)}
        assert page1.isdisjoint(page2)

    def test_music_folder_id_filter(self, client):
        """The newly-honored music_folder_id param scopes results to one folder."""
        with transaction():
            f1 = queries.add_music_folder(name="f1", path="/folder-one")
            f2 = queries.add_music_folder(name="f2", path="/folder-two")
            artist = queries.upsert_artist("Multi Folder")
            album = queries.upsert_album(artist_id=artist, name="Spread", year=2024)
            t1 = queries.upsert_track(_track_row(
                path="/folder-one/song.mp3",
                artist_id=artist, album_id=album, music_folder_id=f1, genre="Mixed",
            ))
            queries.upsert_track(_track_row(
                path="/folder-two/song.mp3",
                artist_id=artist, album_id=album, music_folder_id=f2, genre="Mixed",
            ))

        results = queries.list_song_by_genre("Mixed", 10, 0, music_folder_id=f1)
        assert {t["id"] for t in results} == {t1}


# ===========================================================================
# library_stats
# ===========================================================================


class TestLibraryStats:
    def test_empty_library(self, client):
        assert queries.library_stats() == {
            "artists": 0,
            "albums": 0,
            "tracks": 0,
            "total_duration_seconds": 0,
        }

    def test_counts_match_seeded_library(self, client, seeded_library):
        s = queries.library_stats()
        assert s["artists"] == 1
        assert s["albums"] == 1
        assert s["tracks"] == 1
        assert s["total_duration_seconds"] == 180


# ===========================================================================
# cleanup_empty_albums_and_artists
# ===========================================================================


class TestCleanupEmpty:
    def test_album_with_no_tracks_is_pruned(self, client):
        with transaction():
            artist = queries.upsert_artist("Cleanup Artist")
            album = queries.upsert_album(artist_id=artist, name="Empty", year=2024)
        assert queries.get_album(album) is not None

        with transaction():
            albums_deleted, _ = queries.cleanup_empty_albums_and_artists()
        assert albums_deleted >= 1
        assert queries.get_album(album) is None

    def test_artist_with_no_albums_is_pruned(self, client):
        with transaction():
            artist = queries.upsert_artist("Lonely Artist")
        with transaction():
            _, artists_deleted = queries.cleanup_empty_albums_and_artists()
        assert artists_deleted >= 1
        assert queries.get_artist(artist) is None

    def test_artist_and_album_with_tracks_survive(self, client, seeded_library):
        with transaction():
            queries.cleanup_empty_albums_and_artists()
        assert queries.get_artist(seeded_library["artist_id"]) is not None
        assert queries.get_album(seeded_library["album_id"]) is not None


# ===========================================================================
# Aggregate denormalization
# ===========================================================================


class TestUpdateAggregates:
    def test_update_album_aggregates_counts_tracks_and_sums_duration(self, client):
        with transaction():
            folder = queries.add_music_folder(name="agg", path="/agg-folder")
            artist = queries.upsert_artist("Agg Artist")
            album = queries.upsert_album(artist_id=artist, name="Agg", year=2024)
            for i in range(3):
                queries.upsert_track(_track_row(
                    path=f"/agg-folder/{i}.mp3", title=f"t{i}",
                    artist_id=artist, album_id=album, music_folder_id=folder,
                    duration=60,
                ))
            queries.update_album_aggregates(album)

        row = queries.get_album(album)
        assert row["track_count"] == 3
        assert row["duration"] == 180

    def test_update_artist_aggregates_counts_albums(self, client):
        with transaction():
            artist = queries.upsert_artist("Multi Album")
            queries.upsert_album(artist_id=artist, name="A", year=2024)
            queries.upsert_album(artist_id=artist, name="B", year=2024)
            queries.update_artist_aggregates(artist)
        assert queries.get_artist(artist)["album_count"] == 2
