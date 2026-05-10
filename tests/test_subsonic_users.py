"""
Subsonic user-management endpoint tests.

Tests the /rest/* user management endpoints per the OpenSubsonic 1.16.1 spec:
  - getUser, getUsers, createUser, updateUser, deleteUser, changePassword
  - getOpenSubsonicExtensions (public, no auth)
  - OpenSubsonic response envelope fields on every response
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sub(client, endpoint, admin=True, **params):
    """Make a GET request to /rest/{endpoint} with Subsonic auth params."""
    username = "admin" if admin else "regular"
    password = "adminpass" if admin else "userpass"
    return client.get(
        f"/rest/{endpoint}",
        params={"u": username, "p": password, "v": "1.16.1", "c": "pytest", "f": "json", **params},
    )


def _ok(r):
    assert r.status_code == 200
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", f"Expected ok, got: {body}"
    return body


def _err(r):
    assert r.status_code == 200
    body = r.json()["subsonic-response"]
    assert body["status"] == "failed", f"Expected failed, got: {body}"
    return body


# ---------------------------------------------------------------------------
# OpenSubsonic envelope
# ---------------------------------------------------------------------------

class TestOpenSubsonicEnvelope:
    def test_envelope_has_opensubsonic_true(self, client):
        r = client.get("/rest/ping", params={
            "u": "admin", "p": "adminpass", "v": "1.16.1", "c": "test", "f": "json"
        })
        body = r.json()["subsonic-response"]
        assert body["openSubsonic"] is True

    def test_envelope_has_type(self, client):
        r = client.get("/rest/ping", params={
            "u": "admin", "p": "adminpass", "v": "1.16.1", "c": "test", "f": "json"
        })
        body = r.json()["subsonic-response"]
        assert "type" in body and body["type"]

    def test_envelope_has_server_version(self, client):
        r = client.get("/rest/ping", params={
            "u": "admin", "p": "adminpass", "v": "1.16.1", "c": "test", "f": "json"
        })
        body = r.json()["subsonic-response"]
        assert "serverVersion" in body and body["serverVersion"]

    def test_error_envelope_has_opensubsonic_true(self, client):
        r = client.get("/rest/ping", params={
            "u": "nobody", "p": "wrong", "v": "1.16.1", "c": "test", "f": "json"
        })
        body = r.json()["subsonic-response"]
        assert body["status"] == "failed"
        assert body.get("openSubsonic") is True


# ---------------------------------------------------------------------------
# getOpenSubsonicExtensions (public — no auth required)
# ---------------------------------------------------------------------------

class TestGetOpenSubsonicExtensions:
    def test_no_auth_required(self, client):
        r = client.get("/rest/getOpenSubsonicExtensions", params={"f": "json"})
        assert r.status_code == 200
        body = r.json()["subsonic-response"]
        assert body["status"] == "ok"

    def test_returns_extensions_array(self, client):
        r = client.get("/rest/getOpenSubsonicExtensions", params={"f": "json"})
        body = r.json()["subsonic-response"]
        assert "openSubsonicExtensions" in body
        exts = body["openSubsonicExtensions"]
        assert isinstance(exts, list)
        for ext in exts:
            assert "name" in ext
            assert "versions" in ext
            assert isinstance(ext["versions"], list)


# ---------------------------------------------------------------------------
# getUser
# ---------------------------------------------------------------------------

class TestGetUser:
    def test_admin_gets_own_user(self, client):
        body = _ok(_sub(client, "getUser", username="admin"))
        u = body["user"]
        assert u["username"] == "admin"
        assert u["adminRole"] is True

    def test_admin_gets_other_user(self, client):
        body = _ok(_sub(client, "getUser", username="regular"))
        assert body["user"]["username"] == "regular"

    def test_user_gets_own_user(self, client):
        body = _ok(_sub(client, "getUser", admin=False, username="regular"))
        assert body["user"]["username"] == "regular"

    def test_user_cannot_get_other_user(self, client):
        body = _err(_sub(client, "getUser", admin=False, username="admin"))
        assert body["error"]["code"] == 50  # ERR_NOT_AUTHORIZED

    def test_user_shape_has_all_required_roles(self, client):
        body = _ok(_sub(client, "getUser", username="admin"))
        u = body["user"]
        required = [
            "username", "email", "scrobblingEnabled", "adminRole",
            "settingsRole", "downloadRole", "uploadRole", "playlistRole",
            "coverArtRole", "commentRole", "podcastRole", "streamRole",
            "jukeboxRole", "shareRole", "videoConversionRole",
        ]
        for field in required:
            assert field in u, f"Missing field: {field}"

    def test_nonexistent_user_returns_404(self, client):
        body = _err(_sub(client, "getUser", username="nobody"))
        assert body["error"]["code"] == 70


# ---------------------------------------------------------------------------
# getUsers (admin only)
# ---------------------------------------------------------------------------

class TestGetUsers:
    def test_admin_gets_all_users(self, client):
        body = _ok(_sub(client, "getUsers"))
        user_list = body["users"]["user"]
        usernames = {u["username"] for u in user_list}
        assert "admin" in usernames
        assert "regular" in usernames

    def test_regular_user_forbidden(self, client):
        body = _err(_sub(client, "getUsers", admin=False))
        assert body["error"]["code"] == 50


# ---------------------------------------------------------------------------
# createUser (admin only)
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_admin_creates_user(self, client):
        body = _ok(_sub(client, "createUser", username="newguy", password="pass123"))
        # Verify the user now exists
        body2 = _ok(_sub(client, "getUser", username="newguy"))
        assert body2["user"]["username"] == "newguy"

    def test_creates_with_roles(self, client):
        _ok(_sub(client, "createUser",
                 username="roleduser", password="pass",
                 downloadRole="true", shareRole="true"))
        body = _ok(_sub(client, "getUser", username="roleduser"))
        u = body["user"]
        assert u["downloadRole"] is True
        assert u["shareRole"] is True

    def test_duplicate_username_fails(self, client):
        body = _err(_sub(client, "createUser", username="admin", password="pass"))
        assert body["error"]["code"] == 0  # ERR_GENERIC

    def test_regular_user_cannot_create(self, client):
        body = _err(_sub(client, "createUser", admin=False, username="x", password="y"))
        assert body["error"]["code"] == 50


# ---------------------------------------------------------------------------
# updateUser (admin only)
# ---------------------------------------------------------------------------

class TestUpdateUser:
    def test_admin_updates_download_role(self, client):
        # Regular user starts with downloadRole=False (spec default)
        before = _ok(_sub(client, "getUser", username="regular"))["user"]
        assert before["downloadRole"] is False

        _ok(_sub(client, "updateUser", username="regular", downloadRole="true"))

        after = _ok(_sub(client, "getUser", username="regular"))["user"]
        assert after["downloadRole"] is True

    def test_update_password(self, client):
        _ok(_sub(client, "updateUser", username="regular", password="newpassword"))
        # Verify new password works for auth
        r = client.get("/rest/ping", params={
            "u": "regular", "p": "newpassword", "v": "1.16.1", "c": "test", "f": "json"
        })
        assert _ok(r)["status"] == "ok"

    def test_nonexistent_user_returns_404(self, client):
        body = _err(_sub(client, "updateUser", username="nobody"))
        assert body["error"]["code"] == 70

    def test_regular_user_cannot_update(self, client):
        body = _err(_sub(client, "updateUser", admin=False, username="admin"))
        assert body["error"]["code"] == 50


# ---------------------------------------------------------------------------
# deleteUser (admin only)
# ---------------------------------------------------------------------------

class TestDeleteUser:
    def test_admin_deletes_user(self, client):
        # Create a throwaway user first
        _ok(_sub(client, "createUser", username="todelete", password="pass"))
        _ok(_sub(client, "deleteUser", username="todelete"))
        # Confirm they're gone
        body = _err(_sub(client, "getUser", username="todelete"))
        assert body["error"]["code"] == 70

    def test_cannot_delete_self(self, client):
        body = _err(_sub(client, "deleteUser", username="admin"))
        assert body["error"]["code"] == 0  # ERR_GENERIC

    def test_nonexistent_user_returns_404(self, client):
        body = _err(_sub(client, "deleteUser", username="nobody"))
        assert body["error"]["code"] == 70

    def test_regular_user_cannot_delete(self, client):
        body = _err(_sub(client, "deleteUser", admin=False, username="regular"))
        assert body["error"]["code"] == 50


# ---------------------------------------------------------------------------
# changePassword (Subsonic 1.1.0)
# ---------------------------------------------------------------------------

class TestChangePassword:
    def test_user_changes_own_password(self, client):
        _ok(_sub(client, "changePassword", admin=False, username="regular", password="freshpass"))
        # Confirm new password works
        r = client.get("/rest/ping", params={
            "u": "regular", "p": "freshpass", "v": "1.16.1", "c": "test", "f": "json"
        })
        assert _ok(r)["status"] == "ok"

    def test_user_cannot_change_others_password(self, client):
        body = _err(_sub(client, "changePassword", admin=False,
                         username="admin", password="hacked"))
        assert body["error"]["code"] == 50

    def test_admin_changes_others_password(self, client):
        _ok(_sub(client, "changePassword", username="regular", password="adminset"))
        r = client.get("/rest/ping", params={
            "u": "regular", "p": "adminset", "v": "1.16.1", "c": "test", "f": "json"
        })
        assert _ok(r)["status"] == "ok"

    def test_nonexistent_user_returns_404(self, client):
        body = _err(_sub(client, "changePassword", username="nobody", password="x"))
        assert body["error"]["code"] == 70
