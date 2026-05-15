"""
Shared pytest fixtures for the Muse test suite.

What is a "fixture"? (novice explanation)
------------------------------------------
In pytest, a fixture is a function that prepares something a test needs and
then hands it over. If a test function lists `client` as an argument, pytest
automatically calls the `client` fixture below, runs the setup code, gives the
test the yielded value, then runs the teardown code after the test finishes.
This is how every test gets a clean, isolated database without having to set
one up manually.

Strategy
--------
Each test function gets an isolated SQLite database (tmp_path is a pytest
built-in fixture that provides a temporary directory that is automatically
deleted after the test). We:

  1. Patch the settings singleton to point at the temp DB instead of the real one.
  2. Reset the thread-local DB connection so the next get_conn() connects to the temp DB.
  3. Run migrations — this creates the schema and seeds the bootstrap admin user.
  4. Create a second, non-admin user for permission tests.
  5. Yield a TestClient — an in-process HTTP client that talks to the real FastAPI app.
  6. Tear down (close connection, restore settings) after the test.

WHY we use `with transaction():` to create the "regular" user:
    SQLite connections are per-thread. The test runs on the main thread;
    request handlers run on worker threads. Python's sqlite3 module wraps
    writes in an implicit BEGIN that isn't committed until you call conn.commit().
    If we just call create_user() without a transaction wrapper, the INSERT
    sits in an uncommitted state on the main thread's connection. Worker threads
    can't see it, so API calls that try to look up "regular" return None.
    `with transaction():` commits at the end of the block, making the row
    visible to all threads.

JWT tokens are generated directly (bypassing bcrypt on the login endpoint)
so tests are fast. bcrypt round cost is set to 4 (vs 12 in production) for
the same reason — slow hashing would make the test suite take minutes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.config.settings as _settings_mod
from backend.config import Settings
from backend.core.auth import create_jwt, hash_password
from backend.db import init_db, run_migrations, transaction
from backend.db.connection import close_thread_connection
from backend.db import queries


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
# Core fixture: isolated DB + TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
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

    # Add a non-admin user for permission tests.
    # Use transaction() so the INSERT is committed and visible to request
    # handler threads (which have their own SQLite connections).
    with transaction():
        queries.create_user("regular", _fast_hash("userpass"), is_admin=False)

    # Import app *after* patching settings so lifespan uses the test DB.
    from backend.main import app
    with TestClient(app, raise_server_exceptions=True) as tc:
        yield tc

    # Teardown: close the connection and restore the original settings instance.
    close_thread_connection()
    _settings_mod._settings_instance = original_instance


# ---------------------------------------------------------------------------
# Token fixtures (built from the pre-seeded users)
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_token(client) -> str:
    """JWT for the pre-seeded admin user (id=1, username='admin')."""
    user = queries.get_user_by_username("admin")
    assert user is not None, "admin user must be seeded by migration_002"
    return create_jwt(user)


@pytest.fixture()
def user_token(client) -> str:
    """JWT for the pre-seeded regular user."""
    user = queries.get_user_by_username("regular")
    assert user is not None
    return create_jwt(user)


@pytest.fixture()
def admin_headers(admin_token) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def user_headers(user_token) -> dict:
    return {"Authorization": f"Bearer {user_token}"}
