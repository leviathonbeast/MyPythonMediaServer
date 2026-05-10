"""
Internal API for the web UI.

These endpoints sit at /api/* and use JWT auth. They're shaped for the
frontend's convenience, not Subsonic compatibility — clean JSON, conventional
HTTP status codes.

We deliberately keep the surface small:
    POST /api/auth/login   → JWT
    GET  /api/me           → current user
    GET  /api/stats        → library stats
    POST /api/scan         → trigger a scan
    GET  /api/scan         → scan progress

The frontend uses the same /rest/* Subsonic endpoints for data (browse,
search, stream). That keeps the contract surface small and means we eat our
own dogfood — if Subsonic clients work, the web UI works.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from backend.core import auth as auth_core
from backend.core import library as library_core
from backend.core import lastfm
from backend.db import queries
from backend.db import maintenance as db_maintenance
from backend.scanner import cancel_scan, get_progress, start_scan_async
from backend.streaming import presets as transcode_presets

router = APIRouter(prefix="/api", tags=["web"])
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# JWT dependency
# ---------------------------------------------------------------------------

def jwt_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> dict:
    """
    Extract and verify the JWT bearer. Raises 401 on any failure.
    Returns the payload (sub, username, is_admin).
    """
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    payload = auth_core.decode_jwt(creds.credentials)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return payload


def jwt_admin(user: dict = Depends(jwt_user)) -> dict:
    """JWT dependency that additionally requires the user to be an admin.

    Used to gate destructive maintenance endpoints (gc, vacuum). We deliberately
    return 403 (not 401) when the token is valid but the user lacks the role —
    that way the frontend doesn't sign the user out on a permission denial.
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest):
    user = queries.get_user_by_username(body.username)
    if user is None or not auth_core.verify_password(body.password, user["password_hash"]):
        # Same message for unknown user / bad password — don't leak existence.
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Side effect: cache the plaintext for Subsonic token+salt verification.
    # See core/auth.py::_verify_with_password — we re-run it here so a single
    # successful web login enables the user's Subsonic clients too.
    auth_core._verify_with_password(body.username, body.password)

    token = auth_core.create_jwt(user)
    return LoginResponse(token=token, username=user["username"], is_admin=bool(user["is_admin"]))


@router.get("/me")
def me(user=Depends(jwt_user)):
    """Return the JWT payload — useful for the frontend to refresh state."""
    return user


@router.get("/stats")
def stats(_=Depends(jwt_user)):
    return queries.library_stats()


@router.post("/scan")
def trigger_scan(_=Depends(jwt_user)):
    started = start_scan_async()
    return {"started": started, "progress": _progress_dict()}


@router.get("/scan")
def scan_progress(_=Depends(jwt_user)):
    return _progress_dict()


@router.post("/scan/cancel")
def cancel_scan_endpoint(_=Depends(jwt_user)):
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
# Maintenance endpoints
# ---------------------------------------------------------------------------
# These are admin-only because they're destructive: gc removes orphan rows
# and orphan artwork files, vacuum rewrites the database file. Both are
# safe in the sense that they only remove things already invisible to the
# user, but mistakes here would be hard to undo.

# We deliberately refuse to GC during a running scan. A scan is itself a
# write-heavy workload and GC would (a) compete for SQLite write locks and
# (b) potentially delete artwork files that the in-flight scan is about to
# reference. Cleaner to ask the user to wait.

@router.post("/maintenance/gc")
def maintenance_gc(_=Depends(jwt_admin)):
    """Run routine garbage collection: orphan rows + orphan artwork files."""
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot run GC while a scan is in progress",
        )
    result = db_maintenance.run_gc(vacuum=False)
    return result.as_dict()


@router.post("/maintenance/vacuum")
def maintenance_vacuum(_=Depends(jwt_admin)):
    """Run GC and additionally VACUUM the database (rewrites the file).

    This is potentially long-running and acquires an exclusive lock for the
    duration; expect every other request to block. Worth running occasionally
    after deleting a lot of music.
    """
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot vacuum while a scan is in progress",
        )
    result = db_maintenance.run_gc(vacuum=True)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Music folder management
# ---------------------------------------------------------------------------
# At first boot music_folders is seeded from config.yaml's `music_folders:`
# list (see migrations.py::_migration_003_seed_music_folders), but after
# that the database is the source of truth — these endpoints let an admin
# add and remove folders at runtime without restarting. The config.yaml
# list is essentially a bootstrap for fresh installs.

class FolderAddRequest(BaseModel):
    name: str
    path: str


@router.get("/folders")
def folders_list(_=Depends(jwt_user)):
    """List all configured music folders with their track counts."""
    return queries.list_music_folders_with_counts()


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def folders_add(body: FolderAddRequest, _=Depends(jwt_admin)):
    """Add a music folder.

    Validation rules:
      - `path` must exist on disk and be readable as a directory at the
        moment of the request. Saves the user from "I added /mnt/foo
        but the scan finds nothing" — almost always a typo or an
        unmounted share.
      - `path` is normalised (expanduser + resolve) so that two
        cosmetically-different inputs pointing at the same directory
        don't both get added.
      - `name` falls back to the basename of the path if blank.
    """
    import os as _os, sqlite3 as _sqlite3
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
                "is the path correct and the share / mount available?"
            ),
        )
    if not _os.access(resolved, _os.R_OK):
        raise HTTPException(status_code=400, detail=f"{resolved} is not readable by the server process")

    name = body.name.strip() or _os.path.basename(resolved.rstrip("/")) or resolved

    try:
        new_id = queries.add_music_folder(name, resolved)
    except _sqlite3.IntegrityError:
        existing = queries.get_music_folder_by_path(resolved)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already configured as folder #{existing['id'] if existing else '?'}",
        )

    return {"id": new_id, "name": name, "path": resolved, "track_count": 0}


@router.delete("/folders/{folder_id}", status_code=status.HTTP_200_OK)
def folders_delete(folder_id: int, _=Depends(jwt_admin)):
    """Remove a music folder.

    All tracks under it are cascade-deleted by foreign key. Files on disk
    are NOT touched — Muse never modifies the user's music files. After
    deletion we kick off a GC to prune the now-empty albums/artists and
    their orphan artwork. We refuse to delete during a running scan.
    """
    if get_progress().running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove a folder while a scan is in progress",
        )
    folder = queries.get_music_folder(folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such folder")

    deleted = queries.delete_music_folder(folder_id)
    if not deleted:
        # Race: someone deleted it between our check and ours. Treat as success.
        return {"deleted": False, "folder": folder}

    # Best-effort GC; failure here doesn't undo the delete.
    try:
        gc_result = db_maintenance.run_gc(vacuum=False).as_dict()
    except Exception:
        gc_result = None

    return {"deleted": True, "folder": folder, "gc": gc_result}


# ---------------------------------------------------------------------------
# Transcoding policy
# ---------------------------------------------------------------------------
# Exposes the server's effective transcoding configuration so the frontend
# can populate its quality dropdown from a single source of truth and
# mirror the resolve_preset rules to display a "transcoded" badge in the
# player without an extra round-trip per track.

@router.get("/transcoding/policy")
def transcoding_policy(_=Depends(jwt_user)):
    """The server's transcoding rules + the menu of presets.

    Shape:
      {
        "transcoding_enabled":     bool,
        "default_format":          "raw" | "mp3" | "opus" | "ogg",
        "default_bitrate":         int,
        "max_streaming_bitrate":   int | null,
        "presets": [
          {"format": "mp3",  "bitrate": 320, "content_type": "audio/mpeg"},
          ...
        ]
      }
    """
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
# Artist page
# ---------------------------------------------------------------------------
# Custom non-Subsonic endpoint used by the new artist view. Subsonic's
# getMusicDirectory and getArtist both work, but neither returns:
#   - albums grouped by release type (album / EP / single / compilation)
#   - an artist biography
# So we add this one. The frontend prefers it for the artist page; mobile
# Subsonic clients keep using getMusicDirectory and don't see any of this.

# Mapping from the various tag values we see in the wild to the four
# UI sections. Anything not listed maps to "albums".
_RELEASE_TYPE_GROUPS = {
    # primary albums
    "album":        "albums",
    "":             "albums",
    None:           "albums",
    # EPs
    "ep":           "eps",
    # singles
    "single":       "singles",
    # compilations / soundtracks behave like compilations to most listeners
    "compilation":  "compilations",
    "soundtrack":   "compilations",
    "anthology":    "compilations",
    # everything else gets bucketed into "other": live, remix, demo,
    # broadcast, dj-mix, mixtape, audiobook, audio drama, spokenword,
    # interview. Treated as a junk drawer.
}


@router.get("/artist/{artist_id_str}")
def artist_detail(artist_id_str: str, _=Depends(jwt_user)):
    """Return artist + grouped albums + optional Last.fm bio."""
    # Accept Subsonic-prefixed ids ("ar-42") or bare integers ("42") so
    # frontend code can pass through whatever it has in hand.
    kind, internal_id = library_core.parse_id(artist_id_str)
    if kind != "artist":
        # Fall back to bare integer for testing convenience.
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

    # Group by mapped release type, preserving year ordering within each.
    grouped: dict[str, list] = {
        "albums":       [],
        "eps":          [],
        "singles":      [],
        "compilations": [],
        "other":        [],
    }
    for a in albums:
        rt = (a.get("release_type") or "").strip().lower() or None
        bucket = _RELEASE_TYPE_GROUPS.get(rt, "other")
        # Render each album in the same shape the album-card UI consumes.
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

    # Last.fm bio is best-effort. Failure is silent and the frontend just
    # doesn't render that section.
    bio = lastfm.get_artist_bio(artist["name"])

    return {
        "id":             library_core.make_artist_id(internal_id),
        "name":           artist["name"],
        "album_count":    artist.get("album_count"),
        "albums_grouped": grouped,
        "bio":            bio.as_dict() if bio else None,
    }
