"""
Internal API for the web UI.

These endpoints sit at /api/* and use JWT auth. They're shaped for the
frontend's convenience, not Subsonic compatibility — clean JSON, conventional
HTTP status codes.

Permission model
----------------
Admin-only (require is_admin=True):
    POST   /api/scan              — trigger a library scan
    POST   /api/scan/cancel       — cancel an in-progress scan
    POST   /api/maintenance/gc    — garbage-collect orphan rows + artwork
    POST   /api/maintenance/vacuum — GC + VACUUM database file
    POST   /api/folders           — add a music folder
    DELETE /api/folders/{id}      — remove a music folder
    GET    /api/users             — list all users
    POST   /api/users             — create a user
    GET    /api/users/{id}        — get a specific user
    PATCH  /api/users/{id}        — update is_admin flag or reset password
    DELETE /api/users/{id}        — remove a user

Any authenticated user:
    POST  /api/auth/login         — obtain JWT (no token required)
    GET   /api/me                 — current user info
    POST  /api/me/password        — change own password
    GET   /api/stats              — library counts
    GET   /api/scan               — scan progress (read-only)
    GET   /api/folders            — list music folders
    GET   /api/transcoding/policy — transcoding config
    GET   /api/artist/{id}        — artist detail + bio
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.core import auth as auth_core
from backend.core import lastfm
from backend.core import library as library_core
from backend.db import maintenance as db_maintenance
from backend.db import queries
from backend.db.connection import transaction
from backend.scanner import cancel_scan, get_progress, start_scan_async
from backend.streaming import presets as transcode_presets

from .deps import jwt_admin, jwt_user

router = APIRouter(prefix="/api", tags=["web"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    is_admin: bool


class UserCreateRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatchRequest(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class FolderAddRequest(BaseModel):
    name: str
    path: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest):
    user = queries.get_user_by_username(body.username)
    if user is None or not auth_core.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Warm the plaintext cache so that the Subsonic token+salt auth flow works
    # immediately after web-UI login. Without this, a user who logs in via the
    # web UI and then opens a Subsonic client would need to re-authenticate once
    # with the password path before token+salt starts working.
    auth_core._verify_with_password(body.username, body.password)

    token = auth_core.create_jwt(user)
    return LoginResponse(token=token, username=user["username"], is_admin=bool(user["is_admin"]))


@router.get("/me")
def me(user=Depends(jwt_user)):
    """Return the JWT payload — lets the frontend refresh its local state."""
    return user


@router.post("/me/password")
def change_own_password(body: PasswordChangeRequest, user: dict = Depends(jwt_user)):
    """Change the calling user's own password after verifying the current one."""
    db_user = queries.get_user_by_username(user["username"])
    if db_user is None or not auth_core.verify_password(body.current_password, db_user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if not body.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must not be empty")
    with transaction():
        queries.update_user_password(db_user["id"], auth_core.hash_password(body.new_password))
    return {"updated": True}


# ---------------------------------------------------------------------------
# Library stats
# ---------------------------------------------------------------------------

@router.get("/stats")
def stats(_=Depends(jwt_user)):
    return queries.library_stats()


# ---------------------------------------------------------------------------
# Scan management (admin-only write; user read)
# ---------------------------------------------------------------------------

@router.post("/scan")
def trigger_scan(_=Depends(jwt_admin)):
    """Start a library scan. Admin only."""
    started = start_scan_async()
    return {"started": started, "progress": _progress_dict()}


@router.get("/scan")
def scan_progress(_=Depends(jwt_user)):
    """Read-only scan progress. Available to all authenticated users."""
    return _progress_dict()


@router.post("/scan/cancel")
def cancel_scan_endpoint(_=Depends(jwt_admin)):
    """Cancel a running scan. Admin only."""
    cancelled = cancel_scan()
    return {"cancelled": cancelled, "progress": _progress_dict()}


def _progress_dict():
    p = get_progress()
    return {
        "running":        p.running,
        "started_at":     p.started_at,
        "finished_at":    p.finished_at,
        "folders_total":  p.folders_total,
        "folders_done":   p.folders_done,
        "files_seen":     p.files_seen,
        "files_to_parse": p.files_to_parse,
        "files_parsed":   p.files_parsed,
        "files_added":    p.files_added,
        "files_updated":  p.files_updated,
        "files_removed":  p.files_removed,
        "files_skipped":  p.files_skipped,
        "errors":         p.errors,
        "current_folder": p.current_folder,
    }


# ---------------------------------------------------------------------------
# Maintenance (admin-only)
# ---------------------------------------------------------------------------

@router.post("/maintenance/gc")
def maintenance_gc(_=Depends(jwt_admin)):
    """Garbage-collect orphan rows and orphan artwork files."""
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot run GC while a scan is in progress",
        )
    return db_maintenance.run_gc(vacuum=False).as_dict()


@router.post("/maintenance/vacuum")
def maintenance_vacuum(_=Depends(jwt_admin)):
    """GC + VACUUM the database file. Potentially long-running."""
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot vacuum while a scan is in progress",
        )
    return db_maintenance.run_gc(vacuum=True).as_dict()


# ---------------------------------------------------------------------------
# Music folder management (admin-only write; user read)
# ---------------------------------------------------------------------------

@router.get("/folders")
def folders_list(_=Depends(jwt_user)):
    """List all configured music folders with their track counts."""
    return queries.list_music_folders_with_counts()


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def folders_add(body: FolderAddRequest, _=Depends(jwt_admin)):
    """Add a music folder. Admin only."""
    import os as _os
    from pathlib import Path as _Path

    raw_path = body.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Path is required")

    try:
        resolved = str(_Path(raw_path).expanduser().resolve())
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Path not resolvable: {e}")

    if not _os.path.isdir(resolved):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{resolved} is not a readable directory — "
                "is the path correct and the share/mount available?"
            ),
        )
    if not _os.access(resolved, _os.R_OK):
        raise HTTPException(status_code=400, detail=f"{resolved} is not readable by the server process")

    name = body.name.strip() or _os.path.basename(resolved.rstrip("/")) or resolved

    try:
        with transaction():
            new_id = queries.add_music_folder(name, resolved)
    except sqlite3.IntegrityError:
        existing = queries.get_music_folder_by_path(resolved)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already configured as folder #{existing['id'] if existing else '?'}",
        )

    return {"id": new_id, "name": name, "path": resolved, "track_count": 0}


@router.delete("/folders/{folder_id}", status_code=status.HTTP_200_OK)
def folders_delete(folder_id: int, _=Depends(jwt_admin)):
    """Remove a music folder and cascade-delete its tracks. Admin only."""
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove a folder while a scan is in progress",
        )
    folder = queries.get_music_folder(folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such folder")

    with transaction():
        deleted = queries.delete_music_folder(folder_id)
    if not deleted:
        return {"deleted": False, "folder": folder}

    try:
        gc_result = db_maintenance.run_gc(vacuum=False).as_dict()
    except Exception:
        gc_result = None

    return {"deleted": True, "folder": folder, "gc": gc_result}


# ---------------------------------------------------------------------------
# User management (admin-only)
# ---------------------------------------------------------------------------

@router.get("/users")
def users_list(_=Depends(jwt_admin)):
    """List all users. Password hashes are never included."""
    return queries.list_users()


@router.post("/users", status_code=status.HTTP_201_CREATED)
def users_create(body: UserCreateRequest, _=Depends(jwt_admin)):
    """Create a new user. Admin only."""
    if not body.username.strip():
        raise HTTPException(status_code=400, detail="Username must not be empty")
    if not body.password:
        raise HTTPException(status_code=400, detail="Password must not be empty")
    try:
        with transaction():
            new_id = queries.create_user(
                body.username.strip(),
                auth_core.hash_password(body.password),
                is_admin=body.is_admin,
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' is already taken",
        )
    return {"id": new_id, "username": body.username.strip(), "is_admin": body.is_admin}


@router.get("/users/{user_id}")
def users_get(user_id: int, _=Depends(jwt_admin)):
    """Get a specific user by id. Admin only."""
    user = queries.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    return user


@router.patch("/users/{user_id}")
def users_patch(user_id: int, body: UserPatchRequest, admin: dict = Depends(jwt_admin)):
    """Update a user's admin flag and/or password. Admin only.

    Supplying neither field is a no-op. Admins cannot demote themselves to
    prevent lockout (they can demote other admins).
    """
    user = queries.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")

    # Validate before opening the transaction — we don't want to leave a
    # dangling open transaction on this thread when raising.
    if body.is_admin is not None and user_id == int(admin["sub"]) and not body.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove your own admin role",
        )
    if body.password is not None and not body.password:
        raise HTTPException(status_code=400, detail="Password must not be empty")

    with transaction():
        if body.is_admin is not None:
            queries.set_user_admin(user_id, body.is_admin)
        if body.password is not None:
            queries.update_user_password(user_id, auth_core.hash_password(body.password))

    return queries.get_user_by_id(user_id)


@router.delete("/users/{user_id}", status_code=status.HTTP_200_OK)
def users_delete(user_id: int, admin: dict = Depends(jwt_admin)):
    """Delete a user. Admins cannot delete their own account."""
    if user_id == int(admin["sub"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )
    user = queries.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    with transaction():
        queries.delete_user(user_id)
    return {"deleted": True, "user": user}


# ---------------------------------------------------------------------------
# Transcoding policy (any user)
# ---------------------------------------------------------------------------

@router.get("/transcoding/policy")
def transcoding_policy(_=Depends(jwt_user)):
    from backend.config import get_settings
    s = get_settings()
    return {
        "transcoding_enabled":   s.transcoding_enabled,
        "default_format":        s.default_transcode_format,
        "default_bitrate":       s.default_transcode_bitrate,
        "max_streaming_bitrate": s.max_streaming_bitrate,
        "presets":               transcode_presets.list_presets(),
    }


# ---------------------------------------------------------------------------
# Artist detail (any user)
# ---------------------------------------------------------------------------

_RELEASE_TYPE_GROUPS = {
    "album":        "albums",
    "":             "albums",
    None:           "albums",
    "ep":           "eps",
    "single":       "singles",
    "compilation":  "compilations",
    "soundtrack":   "compilations",
    "anthology":    "compilations",
}


@router.get("/artist/{artist_id_str}")
def artist_detail(artist_id_str: str, _=Depends(jwt_user)):
    """Return artist + grouped albums + optional Last.fm bio."""
    kind, internal_id = library_core.parse_id(artist_id_str)
    if kind != "artist":
        try:
            internal_id = int(artist_id_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid artist id")

    if internal_id is None:
        raise HTTPException(status_code=400, detail="Invalid artist id")

    artist = queries.get_artist(internal_id)
    if artist is None:
        raise HTTPException(status_code=404, detail="No such artist")

    albums = queries.list_artist_albums(internal_id)

    grouped: dict[str, list] = {
        "albums": [], "eps": [], "singles": [], "compilations": [], "other": [],
    }
    for a in albums:
        rt = (a.get("release_type") or "").strip().lower() or None
        bucket = _RELEASE_TYPE_GROUPS.get(rt, "other")
        grouped[bucket].append({
            "id":           library_core.make_album_id(a["id"]),
            "name":         a["name"],
            "artist":       artist["name"],
            "artistId":     library_core.make_artist_id(internal_id),
            "year":         a.get("year"),
            "genre":        a.get("genre"),
            "release_type": rt,
            "track_count":  a.get("track_count"),
            "duration":     a.get("duration"),
            "coverArt":     a.get("cover_art_id"),
        })

    bio = lastfm.get_artist_bio(artist["name"])

    return {
        "id":             library_core.make_artist_id(internal_id),
        "name":           artist["name"],
        "album_count":    artist.get("album_count"),
        "albums_grouped": grouped,
        "bio":            bio.as_dict() if bio else None,
    }
