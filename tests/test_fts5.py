"""Verify the FTS5 virtual table is created by the migration.

SQLite-only: FTS5 is a SQLite virtual table. The Postgres schema uses a
`tracks.search_tsv` column + GIN index instead — see test_search_tsv.py
for the Postgres-side equivalent (added when PYTEST_POSTGRES_URL is set).
"""

from __future__ import annotations

import pytest

from backend.db.connection import get_conn
from tests.conftest import is_postgres_test_mode

pytestmark = pytest.mark.skipif(
    is_postgres_test_mode(),
    reason="FTS5 virtual table is SQLite-only; Postgres uses tsvector instead",
)


class TestFts5Table:
    def test_virt_fts5_table_exists(self, client):
        row = get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='virt_fts5'"
        ).fetchone()
        assert row is not None
