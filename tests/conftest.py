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
Each test function gets an isolated database. The dialect depends on the
environment:

  * Default (no env var)               → SQLite in a per-test tmp directory.
  * PYTEST_POSTGRES_URL set on env     → Postgres against that URL, with the
                                          schema wiped (`DROP SCHEMA public
                                          CASCADE; CREATE SCHEMA public;`)
                                          and migrations re-run before each
                                          test.

POINT THE URL AT A DEDICATED TEST DATABASE — every test wipes the schema
on the target, so pointing at prod would destroy your library. Suggested
setup:

    sudo -u postgres psql -c "CREATE DATABASE muse_test OWNER muse;"
    PYTEST_POSTGRES_URL=postgresql://muse:password@localhost/muse_test pytest

The Postgres pass adds ~100ms per test for the schema reset. With ~225
tests that's roughly 20s on top of the SQLite pass — acceptable for a
second dialect-coverage run.

Parallel execution (pytest-xdist) is not supported for the Postgres pass:
all workers would target the same database and stomp on each other. Run
serially with `pytest -p no:xdist` or omit the env var for parallel work.

Common to both dialects:

  1. Patch the settings singleton to point at the test DB.
  2. Reset the thread-local DB connection so get_conn() opens fresh.
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

import os
import time

import pytest
from fastapi.testclient import TestClient

import backend.config.settings as _settings_mod
from backend.config import Settings
from backend.core.auth import create_jwt, hash_password
from backend.db import init_db, run_migrations, transaction
from backend.db.connection import close_thread_connection
from backend.db import queries


# ---------------------------------------------------------------------------
# Dialect selection
# ---------------------------------------------------------------------------

# Set PYTEST_POSTGRES_URL=postgresql://user:pass@host/dbname to run the suite
# against Postgres. See module docstring for full setup notes — and please
# point at a dedicated TEST database, not your production one.
TEST_POSTGRES_URL: str | None = os.environ.get("PYTEST_POSTGRES_URL")


def is_postgres_test_mode() -> bool:
    """True if the suite is configured to run against Postgres."""
    return TEST_POSTGRES_URL is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path) -> Settings:
    """Minimal Settings pointing to an isolated test database.

    On SQLite we use a per-test file inside `tmp_path` so each test starts
    clean automatically. On Postgres we share one database across the test
    run; cleanup happens in `_wipe_postgres_schema` before each test.
    """
    kwargs = dict(
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
    if is_postgres_test_mode():
        kwargs["database_url"] = TEST_POSTGRES_URL
    else:
        kwargs["database_path"] = str(tmp_path / "test.db")
    return Settings(**kwargs)


def _wipe_postgres_schema() -> None:
    """Drop and recreate the public schema on the Postgres test DB.

    Runs before each test so we get the same clean-slate guarantee SQLite
    gets for free via per-test tmp directories. Uses a one-shot psycopg
    connection (autocommit) outside of our connection registry so it's
    independent of any test-thread state.
    """
    import psycopg

    assert TEST_POSTGRES_URL is not None
    with psycopg.connect(TEST_POSTGRES_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE;")
            cur.execute("CREATE SCHEMA public;")


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
    # connection to the test database on first use within this test.
    close_thread_connection()

    # On the Postgres path we share one database across every test, so the
    # rows from a previous test would still be there. Wipe the schema first
    # so migrations re-run against a clean slate. On SQLite the per-test
    # tmp_path already gives us that.
    if is_postgres_test_mode():
        _wipe_postgres_schema()

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


# ---------------------------------------------------------------------------
# Seeded library: 1 folder → 1 artist → 1 album → 1 track.
# Use for tests that need real ids to query against.
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_library(client):
    """
    Seed a minimal library and return its ids.

    Yields a dict with both the internal integer ids and the Subsonic-prefixed
    string ids (``ar-N``, ``al-N``, ``tr-N``) that real clients send. Calls
    update_*_aggregates so denormalized counts on artist/album rows are
    consistent — tests that exercise list_artists_indexed or library_stats
    rely on them being populated.
    """
    now = int(time.time())
    with transaction():
        folder_id = queries.add_music_folder(
            name="test", path="/test/fixtures/music"
        )
        artist_id = queries.upsert_artist("Test Artist", sort_name="Test Artist")
        album_id = queries.upsert_album(
            artist_id=artist_id,
            name="Test Album",
            year=2024,
            genre="Indie",
            release_type="album",
        )
        track_id = queries.upsert_track({
            "album_id":        album_id,
            "artist_id":       artist_id,
            "music_folder_id": folder_id,
            "path":            "/test/fixtures/music/song.mp3",
            "title":           "Test Song",
            "track_number":    1,
            "disc_number":     1,
            "duration":        180,
            "bitrate":         320,
            "size":            7_200_000,
            "suffix":          "mp3",
            "content_type":    "audio/mpeg",
            "year":            2024,
            "genre":           "Indie",
            "mtime":           now,
            "content_hash":    None,
            "last_scanned":    now,
        })
        queries.update_album_aggregates(album_id)
        queries.update_artist_aggregates(artist_id)
    return {
        "folder_id":     folder_id,
        "artist_id":     artist_id,
        "album_id":      album_id,
        "track_id":      track_id,
        "artist_prefix": f"ar-{artist_id}",
        "album_prefix":  f"al-{album_id}",
        "track_prefix":  f"tr-{track_id}",
    }
