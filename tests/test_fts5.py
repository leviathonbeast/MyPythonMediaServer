"""Verify the FTS5 virtual table is created by the migration."""

from __future__ import annotations

from backend.db.connection import get_conn


class TestFts5Table:
    def test_virt_fts5_table_exists(self, client):
        row = get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='virt_fts5'"
        ).fetchone()
        assert row is not None
