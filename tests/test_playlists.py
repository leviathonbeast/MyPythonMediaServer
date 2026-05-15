"""Playlist endpoint and query tests.

DB-layer tests go through the queries module directly; HTTP tests use the
TestClient. Both share the same isolated DB from the conftest `client`
fixture, so we don't need a parallel `db` fixture.
"""

from __future__ import annotations

from backend.db import queries, transaction

from ._subsonic import sub as _sub, ok as _ok


class TestListPlaylistsQuery:
    def test_empty_db_returns_empty_list(self, client):
        assert queries.list_playlists(1) == []

    def test_created_playlist_appears(self, client):
        with transaction():
            queries.create_playlist("name", 1, [])
        rows = queries.list_playlists(1)
        assert rows[0]["name"] == "name"


class TestGetPlaylists:
    def test_get_playlists(self, client):
        r = _sub(client, "getPlaylists")
        assert r.status_code == 200


class TestGetPlaylist:
    def test_create_then_get_round_trip(self, client):
        body = _ok(_sub(client, "createPlaylist", name="playlistname"))
        pl_id = body["playlist"]["id"]
        body2 = _ok(_sub(client, "getPlaylist", id=pl_id))
        assert body2["playlist"]["name"] == "playlistname"
