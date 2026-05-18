"""
Auth dependencies — shared by both the web and (potentially) the Subsonic routers.

How FastAPI "dependencies" work (plain English):
    A dependency is a function that FastAPI calls automatically before the
    actual endpoint function runs. You declare what you need by writing
    `param: Type = Depends(some_function)` in an endpoint's arguments, and
    FastAPI will call `some_function`, check for errors, and hand its return
    value in as `param`. If the dependency raises an HTTPException, FastAPI
    stops and sends the error to the client without ever calling your endpoint.

    This is why every protected endpoint can just write:
        def my_endpoint(user: dict = Depends(jwt_user)):
    ...and get the authenticated user dict for free, with no manual
    "check the Authorization header" code in every function.

JWT layer (`jwt_user`, `jwt_admin`):
    Used by all /api/* endpoints. Raises HTTP 401/403 on failure — conventional
    HTTP semantics for the frontend.

Subsonic layer (`subsonic_context`):
    Used by all /rest/* endpoints. On auth failure it raises SubsonicAuthError,
    which is caught centrally and mapped to a Subsonic-shaped 200 error response.
    The Subsonic protocol NEVER returns HTTP 401 — it always returns HTTP 200
    with {"status": "failed", "error": {...}} in the body. Clients that receive
    a real 401 interpret it as a network problem, not a wrong password.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.core import auth as auth_core
from backend.core import throttle as auth_throttle
from backend.db import queries
from . import responses


# ---------------------------------------------------------------------------
# JWT auth (web UI, /api/*)
# ---------------------------------------------------------------------------

# HTTPBearer extracts the token from the "Authorization: Bearer <token>" header.
# auto_error=False means it returns None (instead of raising) when the header
# is missing — we then raise our own clearer error message below.
bearer_scheme = HTTPBearer(auto_error=False)


def jwt_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """
    Verify a JWT Bearer token. Returns the payload (sub, username, is_admin).
    Raises 401 if the token is missing or invalid.

    Also revokes tokens whose backing user has been disabled or deleted.
    Without this, an admin who disables a user has to wait up to
    `jwt_expiry_hours` (default 24h) before that user actually loses
    access — which is the opposite of what "disable" implies.
    """
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    payload = auth_core.decode_jwt(creds.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    # Refetch the account on every request so disable/delete takes effect
    # immediately. One indexed lookup per request, negligible compared to
    # the rest of any real endpoint.
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token subject",
        )
    db_user = queries.get_user_by_id(user_id)
    if db_user is None or db_user.get("disabled"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account disabled or removed",
        )

    return payload


def jwt_admin(user: dict = Depends(jwt_user)) -> dict:
    """
    JWT dependency that additionally requires is_admin.

    Returns 403 (not 401) when the token is valid but the user lacks the role,
    so the frontend doesn't sign the user out on a permission denial.
    """
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


# ---------------------------------------------------------------------------
# Subsonic auth (/rest/*)
# ---------------------------------------------------------------------------

@dataclass
class SubsonicContext:
    """
    Per-request context for Subsonic endpoints.

    Every /rest/* endpoint receives one of these after the auth check passes.
    Think of it as a "who is calling me and in what format do they want the
    response" bundle. Endpoints use ctx.fmt and ctx.callback when calling
    responses.ok() or responses.error() so the format choice propagates
    consistently through the whole request/response cycle.
    """
    user_id: int
    username: str
    is_admin: bool
    client: str            # client identifier (Subsonic 'c' param), e.g. "Symfonium"
    version: str           # client API version (Subsonic 'v' param), e.g. "1.16.1"
    fmt: str               # response format: 'json' | 'xml' | 'jsonp'
    callback: Optional[str]  # JSONP callback name (only relevant when fmt='jsonp')


class SubsonicAuthError(Exception):
    """Raised when auth params are missing/invalid. Caught by handlers to emit a Subsonic error."""
    def __init__(self, code: int, message: str, fmt: str = "json", callback: Optional[str] = None):
        self.code = code
        self.message = message
        self.fmt = fmt
        self.callback = callback


def subsonic_context(
    request: Request,
    u: Optional[str] = Query(default=None, description="Username"),
    p: Optional[str] = Query(default=None, description="Password (plain or 'enc:hex')"),
    t: Optional[str] = Query(default=None, description="Auth token (md5(password+salt))"),
    s: Optional[str] = Query(default=None, description="Salt for token"),
    v: Optional[str] = Query(default=None, description="Client API version"),
    c: Optional[str] = Query(default="muse", description="Client identifier"),
    f: Optional[str] = Query(default="json", description="Response format: json|xml|jsonp"),
    callback: Optional[str] = Query(default=None, description="JSONP callback"),
) -> SubsonicContext:
    """
    FastAPI dependency. Raises SubsonicAuthError on failure (handled centrally).
    """
    fmt = (f or "json").lower()
    if not u:
        raise SubsonicAuthError(responses.ERR_PARAMETER, "Missing 'u' parameter", fmt, callback)

    # Throttle BEFORE bcrypt so a brute-forcer can't keep us busy on the
    # rejection path. Keyed by (client_ip, username) so one attacker
    # grinding one user doesn't lock other clients on the same NAT.
    # Throttled requests return the same error code as a wrong password,
    # which keeps "is this account being attacked" indistinguishable from
    # the normal authentication failure path.
    client_ip = request.client.host if request.client else "unknown"
    if auth_throttle.is_blocked(client_ip, u):
        raise SubsonicAuthError(responses.ERR_AUTH, "Wrong username or password", fmt, callback)

    user = auth_core.verify_subsonic_credentials(u, password=p, token=t, salt=s)
    if user is None:
        auth_throttle.record_failure(client_ip, u)
        raise SubsonicAuthError(responses.ERR_AUTH, "Wrong username or password", fmt, callback)

    auth_throttle.record_success(client_ip, u)

    return SubsonicContext(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        client=c or "unknown",
        version=v or "1.16.1",
        fmt=fmt,
        callback=callback,
    )
