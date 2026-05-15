"""Shared helpers for Subsonic endpoint tests.

Module is prefixed with underscore so pytest doesn't try to collect it as
a test file. Import in test modules with:

    from ._subsonic import sub, ok, err

Three previous copies of these helpers existed across the test suite;
this is the canonical version.
"""

from __future__ import annotations

from typing import Any


def sub(client, endpoint: str, admin: bool = True, method: str = "GET", **params: Any):
    """Make a request to /rest/{endpoint} authenticated as one of the seeded users.

    Uses the conftest fixture's pre-seeded admin/regular users. `method`
    defaults to GET; pass "POST" for endpoints that require it.
    """
    username = "admin" if admin else "regular"
    password = "adminpass" if admin else "userpass"
    query = {
        "u": username, "p": password,
        "v": "1.16.1", "c": "pytest", "f": "json",
        **params,
    }
    if method == "GET":
        return client.get(f"/rest/{endpoint}", params=query)
    return client.post(f"/rest/{endpoint}", params=query)


def envelope(r):
    """Assert universal OpenSubsonic envelope fields and return the body."""
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"
    body = r.json().get("subsonic-response")
    assert body is not None, f"Response missing 'subsonic-response' key: {r.text}"
    for k in ("status", "version", "type", "serverVersion", "openSubsonic"):
        assert k in body, f"Envelope missing {k!r}: {body}"
    assert body["openSubsonic"] is True
    return body


def ok(r):
    """Assert status=ok envelope and return the body."""
    body = envelope(r)
    assert body["status"] == "ok", f"Expected ok, got: {body}"
    return body


def err(r, code=None):
    """Assert status=failed envelope (optionally with a specific code)."""
    body = envelope(r)
    assert body["status"] == "failed", f"Expected failed, got: {body}"
    e = body.get("error") or {}
    assert "code" in e and isinstance(e["code"], int), f"Error missing int code: {e}"
    assert "message" in e and e["message"], f"Error missing message: {e}"
    if code is not None:
        assert e["code"] == code, f"Expected error code {code}, got {e['code']}: {e}"
    return body
