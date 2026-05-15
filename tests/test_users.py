"""
User management endpoint tests.

Covers the business logic of /api/users/* and /api/me/password:
  - CRUD lifecycle
  - Duplicate username rejection
  - Self-protection (admin cannot delete/demote themselves)
  - Password change (correct current password required)
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# GET /api/users
# ===========================================================================

class TestListUsers:
    def test_returns_seeded_users(self, client, admin_token):
        r = client.get("/api/users", headers=_auth(admin_token))
        assert r.status_code == 200
        usernames = {u["username"] for u in r.json()}
        assert "admin" in usernames
        assert "regular" in usernames

    def test_password_hash_not_exposed(self, client, admin_token):
        r = client.get("/api/users", headers=_auth(admin_token))
        for user in r.json():
            assert "password_hash" not in user


# ===========================================================================
# POST /api/users
# ===========================================================================

class TestCreateUser:
    def test_create_regular_user(self, client, admin_token):
        r = client.post(
            "/api/users",
            json={"username": "newuser", "password": "secret123", "is_admin": False},
            headers=_auth(admin_token),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["username"] == "newuser"
        assert body["is_admin"] is False
        assert "id" in body

    def test_create_admin_user(self, client, admin_token):
        r = client.post(
            "/api/users",
            json={"username": "newadmin", "password": "secret123", "is_admin": True},
            headers=_auth(admin_token),
        )
        assert r.status_code == 201
        assert r.json()["is_admin"] is True

    def test_duplicate_username_rejected(self, client, admin_token):
        r = client.post(
            "/api/users",
            json={"username": "regular", "password": "anything"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 409

    def test_empty_username_rejected(self, client, admin_token):
        r = client.post(
            "/api/users",
            json={"username": "   ", "password": "secret"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 400

    def test_empty_password_rejected(self, client, admin_token):
        r = client.post(
            "/api/users",
            json={"username": "validname", "password": ""},
            headers=_auth(admin_token),
        )
        assert r.status_code == 400

    def test_created_user_appears_in_list(self, client, admin_token):
        client.post(
            "/api/users",
            json={"username": "listtest", "password": "pass"},
            headers=_auth(admin_token),
        )
        r = client.get("/api/users", headers=_auth(admin_token))
        usernames = {u["username"] for u in r.json()}
        assert "listtest" in usernames


# ===========================================================================
# GET /api/users/{id}
# ===========================================================================

class TestGetUser:
    def test_get_existing_user(self, client, admin_token):
        # Find admin via the list endpoint to avoid cross-thread connection issues.
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        admin = next(u for u in users if u["username"] == "admin")
        r = client.get(f"/api/users/{admin['id']}", headers=_auth(admin_token))
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

    def test_get_nonexistent_user(self, client, admin_token):
        r = client.get("/api/users/99999", headers=_auth(admin_token))
        assert r.status_code == 404


# ===========================================================================
# PATCH /api/users/{id}
# ===========================================================================

class TestPatchUser:
    def test_promote_regular_to_admin(self, client, admin_token):
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        regular = next(u for u in users if u["username"] == "regular")
        r = client.patch(
            f"/api/users/{regular['id']}",
            json={"is_admin": True},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["is_admin"] == 1  # SQLite stores as int

    def test_demote_other_admin(self, client, admin_token):
        # Create a second admin and get the id from the response body (not DB),
        # because the worker thread's INSERT may not yet be visible to the
        # test's thread-local DB connection.
        create_r = client.post(
            "/api/users",
            json={"username": "admin2", "password": "pass", "is_admin": True},
            headers=_auth(admin_token),
        )
        assert create_r.status_code == 201
        admin2_id = create_r.json()["id"]
        r = client.patch(
            f"/api/users/{admin2_id}",
            json={"is_admin": False},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["is_admin"] == 0

    def test_admin_cannot_demote_self(self, client, admin_token):
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        admin = next(u for u in users if u["username"] == "admin")
        r = client.patch(
            f"/api/users/{admin['id']}",
            json={"is_admin": False},
            headers=_auth(admin_token),
        )
        assert r.status_code == 400

    def test_reset_password(self, client, admin_token):
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        regular = next(u for u in users if u["username"] == "regular")
        r = client.patch(
            f"/api/users/{regular['id']}",
            json={"password": "brandnewpass"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200

    def test_patch_nonexistent_user(self, client, admin_token):
        r = client.patch("/api/users/99999", json={"is_admin": False}, headers=_auth(admin_token))
        assert r.status_code == 404

    def test_empty_password_patch_rejected(self, client, admin_token):
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        regular = next(u for u in users if u["username"] == "regular")
        r = client.patch(
            f"/api/users/{regular['id']}",
            json={"password": ""},
            headers=_auth(admin_token),
        )
        assert r.status_code == 400


# ===========================================================================
# DELETE /api/users/{id}
# ===========================================================================

class TestDeleteUser:
    def _get_user_id(self, client, admin_token, username):
        users = client.get("/api/users", headers=_auth(admin_token)).json()
        return next(u["id"] for u in users if u["username"] == username)

    def test_delete_regular_user(self, client, admin_token):
        uid = self._get_user_id(client, admin_token, "regular")
        r = client.delete(f"/api/users/{uid}", headers=_auth(admin_token))
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    def test_deleted_user_not_in_list(self, client, admin_token):
        uid = self._get_user_id(client, admin_token, "regular")
        client.delete(f"/api/users/{uid}", headers=_auth(admin_token))
        r = client.get("/api/users", headers=_auth(admin_token))
        usernames = {u["username"] for u in r.json()}
        assert "regular" not in usernames

    def test_admin_cannot_delete_self(self, client, admin_token):
        uid = self._get_user_id(client, admin_token, "admin")
        r = client.delete(f"/api/users/{uid}", headers=_auth(admin_token))
        assert r.status_code == 400

    def test_delete_nonexistent_user(self, client, admin_token):
        r = client.delete("/api/users/99999", headers=_auth(admin_token))
        assert r.status_code == 404


# ===========================================================================
# POST /api/me/password
# ===========================================================================

class TestChangeOwnPassword:
    def test_correct_current_password_succeeds(self, client, user_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "userpass", "new_password": "newpass999"},
            headers=_auth(user_token),
        )
        assert r.status_code == 200
        assert r.json()["updated"] is True

    def test_wrong_current_password_rejected(self, client, user_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "wrongpass", "new_password": "newpass999"},
            headers=_auth(user_token),
        )
        assert r.status_code == 400

    def test_empty_new_password_rejected(self, client, user_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "userpass", "new_password": ""},
            headers=_auth(user_token),
        )
        assert r.status_code == 400

    def test_admin_can_change_own_password(self, client, admin_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "adminpass", "new_password": "newadminpass"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200


# ===========================================================================
# Login endpoint
# ===========================================================================

class TestLogin:
    def test_valid_credentials(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert r.status_code == 200
        body = r.json()
        assert "token" in body
        assert body["username"] == "admin"
        assert body["is_admin"] is True

    def test_wrong_password(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_unknown_username(self, client):
        r = client.post("/api/auth/login", json={"username": "nobody", "password": "pass"})
        assert r.status_code == 401

    def test_token_grants_access(self, client):
        r = client.post("/api/auth/login", json={"username": "regular", "password": "userpass"})
        token = r.json()["token"]
        r2 = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200
        assert r2.json()["username"] == "regular"
