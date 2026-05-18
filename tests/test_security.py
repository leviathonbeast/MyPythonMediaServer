"""
Regression tests for the security fixes in 68759be… + follow-ups.

Each test here exists to catch a specific revert — not to enumerate
every possible input. If a future refactor removes one of these
mitigations, the corresponding test fires.
"""

from __future__ import annotations

import pytest

from backend.core import throttle
from backend.db import queries, transaction

from ._subsonic import sub, ok, err


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The throttle is module-level state — reset between tests."""
    throttle.reset_for_tests()
    yield
    throttle.reset_for_tests()


# ---------------------------------------------------------------------------
# JSONP callback injection (responses.py:_JSONP_CALLBACK_RE)
# ---------------------------------------------------------------------------

class TestJsonpCallbackInjection:
    """The callback must not be echoed into the response body verbatim.

    Catches anyone removing the regex or widening it to allow `(`, `;`,
    `<`, etc. Pure presence-of-payload-in-body check, no parsing.
    """

    def test_injection_payload_does_not_appear_in_body(self, client):
        r = sub(client, "ping", f="jsonp", callback="alert(1);//")
        assert r.status_code == 200
        assert "alert(1)" not in r.text, (
            "callback was echoed into response body — JSONP regex bypassed?"
        )

    def test_legit_callback_still_works(self, client):
        # Defense against an over-tightening of the regex breaking real clients.
        r = sub(client, "ping", f="jsonp", callback="myCb")
        assert r.status_code == 200
        assert r.text.startswith("myCb(") or r.text.startswith("/**/myCb(")


# ---------------------------------------------------------------------------
# SPA path traversal (main.py:spa_fallback)
# ---------------------------------------------------------------------------

class TestSpaTraversal:
    """A request that escapes the dist root must NOT resolve to a real file.

    We test the containment helper directly rather than going through the
    HTTP layer because httpx (the engine behind TestClient) normalises
    `..` segments in the URL before the request leaves the client. That
    means a TestClient-based traversal test passes even when the server-
    side containment is removed — false confidence. Uvicorn in production
    does NOT normalise, so the vulnerability is real; the test just has
    to bypass the test client's well-meaning sanitisation.
    """

    def test_dotdot_escapes_dist_root(self, client):
        # `app.state._safe_dist_path` is registered by main.py when the
        # dist directory exists. Skip the test in environments where the
        # frontend hasn't been built (no dist → no helper → no codepath).
        safe = getattr(client.app.state, "_safe_dist_path", None)
        if safe is None:
            pytest.skip("frontend/dist not built — spa_fallback not registered")

        # Inside dist: real file → returned. (Smoke-check the helper
        # actually returns something on the happy path; otherwise a test
        # that always returns None looks like it's passing.)
        assert safe("index.html") is not None

        # Escapes via `..` — must return None. If the containment check
        # were removed, the resolved path would point at the real source
        # file on disk and the helper would happily return it.
        for hostile in (
            "../backend/config/settings.py",
            "../../backend/config/settings.py",
            "../../../etc/passwd",
            "..",
        ):
            assert safe(hostile) is None, (
                f"SPA path-traversal containment regressed for {hostile!r}"
            )


# ---------------------------------------------------------------------------
# JWT disabled-user revocation (deps.py:jwt_user)
# ---------------------------------------------------------------------------

class TestJwtDisabledRevocation:
    """A token issued before a disable must stop working after the disable."""

    def test_disable_revokes_existing_jwt(self, client, user_token, admin_headers):
        # Sanity: the user's token currently works.
        r = client.get("/api/me", headers={"Authorization": f"Bearer {user_token}"})
        assert r.status_code == 200

        # Admin disables the user.
        user = queries.get_user_by_username("regular")
        assert user is not None
        r = client.patch(
            f"/api/users/{user['id']}",
            json={"disabled": True},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text

        # Same JWT now rejected — the dep refetches `disabled` from the DB.
        r = client.get("/api/me", headers={"Authorization": f"Bearer {user_token}"})
        assert r.status_code == 401, (
            f"Expected 401 after disable, got {r.status_code}: {r.text}"
        )


# ---------------------------------------------------------------------------
# FTS5 hostile input (queries.py:search3 SQLite branch)
# ---------------------------------------------------------------------------

from tests.conftest import is_postgres_test_mode


class TestFtsHostileInput:
    """A malformed FTS5 query must not 500 the search endpoint.

    Skipped on Postgres because the Postgres branch uses
    websearch_to_tsquery, which is permissive by design.
    """

    @pytest.mark.skipif(
        is_postgres_test_mode(),
        reason="FTS5 sanitisation only applies to the SQLite branch",
    )
    def test_unterminated_quote_returns_empty_envelope(self, client):
        # Pre-fix this raised sqlite3.OperationalError → 500 from the handler.
        r = sub(client, "search3", query='"unterminated')
        body = ok(r)
        # Songs list (if present) is empty; no exception leaked.
        sr = body.get("searchResult3", {})
        assert sr.get("song", []) == []


# ---------------------------------------------------------------------------
# Subsonic auth throttle (throttle.py + deps.py:subsonic_context)
# ---------------------------------------------------------------------------

class TestSubsonicAuthThrottle:
    """After N failures from one (ip, username) pair the next attempt is
    rejected even if the password is correct, returning the same error
    code as a wrong password so attackers can't distinguish."""

    def test_correct_password_blocked_after_burst_of_failures(self, client):
        # Burn the failure budget with wrong passwords.
        for _ in range(throttle.MAX_FAILURES):
            r = client.get(
                "/rest/ping",
                params={"u": "admin", "p": "wrong", "v": "1.16.1", "c": "t", "f": "json"},
            )
            assert r.status_code == 200  # Subsonic always 200, error in body

        # Right password now: still rejected, same code 40.
        r = client.get(
            "/rest/ping",
            params={"u": "admin", "p": "adminpass", "v": "1.16.1", "c": "t", "f": "json"},
        )
        err(r, code=40)

    def test_other_user_unaffected_by_throttle_of_first(self, client):
        # Lock out "admin" from the test client's IP.
        for _ in range(throttle.MAX_FAILURES):
            client.get(
                "/rest/ping",
                params={"u": "admin", "p": "wrong", "v": "1.16.1", "c": "t", "f": "json"},
            )
        # "regular" still authenticates fine — keys are per-(ip, username).
        r = client.get(
            "/rest/ping",
            params={"u": "regular", "p": "userpass", "v": "1.16.1", "c": "t", "f": "json"},
        )
        ok(r)


# ---------------------------------------------------------------------------
# star with garbage id (annotation.py None-skip)
# ---------------------------------------------------------------------------

class TestStarNoneIdSkip:
    """A garbage id must not 500 the endpoint or insert a NULL track_id."""

    def test_garbage_id_does_not_500(self, client):
        r = sub(client, "star", id="not-a-real-id")
        ok(r)  # status=ok envelope; row silently skipped
