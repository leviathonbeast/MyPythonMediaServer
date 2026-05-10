"""
Permission gate tests.

Verifies that every endpoint enforces the correct access level:
  - unauthenticated requests get 401
  - regular-user requests to admin-only endpoints get 403
  - admin requests succeed (HTTP 200 / 201)
  - regular-user requests to user-level endpoints succeed

We deliberately do NOT test the full business logic of each endpoint here —
that belongs in per-feature tests. The sole concern is the permission gate.
"""

from __future__ import annotations

import pytest


# ===========================================================================
# Helpers
# ===========================================================================

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# Admin-only endpoints
# ===========================================================================

class TestScanAdminOnly:
    """POST /api/scan and POST /api/scan/cancel are admin-only."""

    def test_trigger_scan_unauthenticated(self, client):
        r = client.post("/api/scan")
        assert r.status_code == 401

    def test_trigger_scan_regular_user(self, client, user_token):
        r = client.post("/api/scan", headers=_auth(user_token))
        assert r.status_code == 403

    def test_trigger_scan_admin(self, client, admin_token):
        r = client.post("/api/scan", headers=_auth(admin_token))
        # 200 means the endpoint was reached; the scan may or may not start
        # (no folders configured), but permission was granted.
        assert r.status_code == 200

    def test_cancel_scan_unauthenticated(self, client):
        r = client.post("/api/scan/cancel")
        assert r.status_code == 401

    def test_cancel_scan_regular_user(self, client, user_token):
        r = client.post("/api/scan/cancel", headers=_auth(user_token))
        assert r.status_code == 403

    def test_cancel_scan_admin(self, client, admin_token):
        r = client.post("/api/scan/cancel", headers=_auth(admin_token))
        assert r.status_code == 200


class TestMaintenanceAdminOnly:
    """POST /api/maintenance/* are admin-only."""

    def test_gc_unauthenticated(self, client):
        assert client.post("/api/maintenance/gc").status_code == 401

    def test_gc_regular_user(self, client, user_token):
        assert client.post("/api/maintenance/gc", headers=_auth(user_token)).status_code == 403

    def test_gc_admin(self, client, admin_token):
        # 200 = permission granted; 409 = scan in progress (also means auth passed)
        r = client.post("/api/maintenance/gc", headers=_auth(admin_token))
        assert r.status_code in (200, 409)

    def test_vacuum_unauthenticated(self, client):
        assert client.post("/api/maintenance/vacuum").status_code == 401

    def test_vacuum_regular_user(self, client, user_token):
        assert client.post("/api/maintenance/vacuum", headers=_auth(user_token)).status_code == 403

    def test_vacuum_admin(self, client, admin_token):
        r = client.post("/api/maintenance/vacuum", headers=_auth(admin_token))
        assert r.status_code in (200, 409)


class TestFolderWriteAdminOnly:
    """POST /api/folders and DELETE /api/folders/{id} are admin-only."""

    def test_add_folder_unauthenticated(self, client):
        r = client.post("/api/folders", json={"name": "x", "path": "/tmp"})
        assert r.status_code == 401

    def test_add_folder_regular_user(self, client, user_token):
        r = client.post("/api/folders", json={"name": "x", "path": "/tmp"}, headers=_auth(user_token))
        assert r.status_code == 403

    def test_add_folder_admin(self, client, admin_token):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            r = client.post("/api/folders", json={"name": "music", "path": d}, headers=_auth(admin_token))
            assert r.status_code == 201

    def test_delete_folder_unauthenticated(self, client):
        assert client.delete("/api/folders/1").status_code == 401

    def test_delete_folder_regular_user(self, client, user_token):
        assert client.delete("/api/folders/1", headers=_auth(user_token)).status_code == 403

    def test_delete_folder_admin_not_found(self, client, admin_token):
        # 404 = auth passed, folder just doesn't exist
        r = client.delete("/api/folders/9999", headers=_auth(admin_token))
        assert r.status_code == 404


class TestUserManagementAdminOnly:
    """All /api/users/* endpoints are admin-only."""

    def test_list_users_unauthenticated(self, client):
        assert client.get("/api/users").status_code == 401

    def test_list_users_regular_user(self, client, user_token):
        assert client.get("/api/users", headers=_auth(user_token)).status_code == 403

    def test_list_users_admin(self, client, admin_token):
        assert client.get("/api/users", headers=_auth(admin_token)).status_code == 200

    def test_create_user_unauthenticated(self, client):
        r = client.post("/api/users", json={"username": "x", "password": "y"})
        assert r.status_code == 401

    def test_create_user_regular_user(self, client, user_token):
        r = client.post("/api/users", json={"username": "x", "password": "y"}, headers=_auth(user_token))
        assert r.status_code == 403

    def test_get_user_unauthenticated(self, client):
        assert client.get("/api/users/1").status_code == 401

    def test_get_user_regular_user(self, client, user_token):
        assert client.get("/api/users/1", headers=_auth(user_token)).status_code == 403

    def test_patch_user_unauthenticated(self, client):
        assert client.patch("/api/users/1", json={"is_admin": False}).status_code == 401

    def test_patch_user_regular_user(self, client, user_token):
        assert client.patch("/api/users/1", json={"is_admin": False}, headers=_auth(user_token)).status_code == 403

    def test_delete_user_unauthenticated(self, client):
        assert client.delete("/api/users/1").status_code == 401

    def test_delete_user_regular_user(self, client, user_token):
        assert client.delete("/api/users/1", headers=_auth(user_token)).status_code == 403


# ===========================================================================
# User-level endpoints (any authenticated user)
# ===========================================================================

class TestUserLevelEndpoints:
    """These endpoints accept any valid JWT."""

    def test_me_unauthenticated(self, client):
        assert client.get("/api/me").status_code == 401

    def test_me_regular_user(self, client, user_token):
        r = client.get("/api/me", headers=_auth(user_token))
        assert r.status_code == 200
        assert r.json()["username"] == "regular"

    def test_me_admin(self, client, admin_token):
        r = client.get("/api/me", headers=_auth(admin_token))
        assert r.status_code == 200
        assert r.json()["is_admin"] is True

    def test_stats_unauthenticated(self, client):
        assert client.get("/api/stats").status_code == 401

    def test_stats_regular_user(self, client, user_token):
        assert client.get("/api/stats", headers=_auth(user_token)).status_code == 200

    def test_scan_progress_unauthenticated(self, client):
        assert client.get("/api/scan").status_code == 401

    def test_scan_progress_regular_user(self, client, user_token):
        # Regular users can read scan progress (read-only)
        assert client.get("/api/scan", headers=_auth(user_token)).status_code == 200

    def test_folders_list_unauthenticated(self, client):
        assert client.get("/api/folders").status_code == 401

    def test_folders_list_regular_user(self, client, user_token):
        assert client.get("/api/folders", headers=_auth(user_token)).status_code == 200

    def test_transcoding_policy_unauthenticated(self, client):
        assert client.get("/api/transcoding/policy").status_code == 401

    def test_transcoding_policy_regular_user(self, client, user_token):
        assert client.get("/api/transcoding/policy", headers=_auth(user_token)).status_code == 200

    def test_change_own_password_unauthenticated(self, client):
        r = client.post("/api/me/password", json={"current_password": "x", "new_password": "y"})
        assert r.status_code == 401

    def test_change_own_password_regular_user_wrong_current(self, client, user_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "wrong", "new_password": "newpass"},
            headers=_auth(user_token),
        )
        assert r.status_code == 400

    def test_change_own_password_regular_user_correct(self, client, user_token):
        r = client.post(
            "/api/me/password",
            json={"current_password": "userpass", "new_password": "newpass123"},
            headers=_auth(user_token),
        )
        assert r.status_code == 200
        assert r.json()["updated"] is True


# ===========================================================================
# Token validation
# ===========================================================================

class TestTokenValidation:
    """Malformed / expired tokens must be rejected."""

    def test_garbage_token(self, client):
        r = client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401

    def test_wrong_scheme(self, client):
        r = client.get("/api/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert r.status_code == 401

    def test_no_authorization_header(self, client):
        assert client.get("/api/me").status_code == 401
