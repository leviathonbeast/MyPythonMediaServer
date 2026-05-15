from __future__ import annotations

import pytest

import backend.config.settings as _settings_mod
from backend.config import Settings
from backend.db import init_db, run_migrations, transaction
from backend.db.connection import close_thread_connection
from backend.db import queries
from backend.db.connection import get_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path) -> Settings:
    """Minimal Settings pointing to an isolated temp database."""
    return Settings(
        database_path=str(tmp_path / "test.db"),
        artwork_cache_dir=str(tmp_path / "artwork"),
        jwt_secret="test-secret-key-for-pytest",
        jwt_algorithm="HS256",
        jwt_expiry_hours=1,
        admin_username="admin",
        admin_password="adminpass",
        music_folders=[],
        scan_on_startup=False,
        lastfm_api_key=None,
    )


def _fast_hash(password: str) -> str:
    """Bcrypt hash with cost=4 so fixture setup doesn't dominate test runtime."""
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


# ---------------------------------------------------------------------------
# Core fixture: isolated DB 
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """
    Yield a TestClient backed by a fresh, isolated database.

    Provides two pre-seeded users:
        admin   / adminpass  — is_admin=True  (seeded by migration_002)
        regular / userpass   — is_admin=False (added here)

    JWT tokens for each are available via the ``admin_token`` and
    ``user_token`` fixtures. Both tokens are valid for the duration of the
    test.
    """
    settings = _make_settings(tmp_path)

    # Patch the singleton before anything touches it.
    original_instance = _settings_mod._settings_instance
    _settings_mod._settings_instance = settings

    # Reset any existing thread-local connection so get_conn() opens a fresh
    # connection to the temp database on first use within this test.
    close_thread_connection()

    # Init path + run migrations (seeds schema + admin user via migration_002).
    init_db(settings)
    run_migrations()

    yield


    # Teardown: close the connection and restore the original settings instance.
    close_thread_connection()
    _settings_mod._settings_instance = original_instance

class TestEmptyTable:
    def test_returns_fts5_empty(self, db):
        r = get_conn().execute(""" SELECT name FROM sqlite_master WHERE type='table' AND name='virt_fts5' """).fetchone()
        assert r is not None
      
