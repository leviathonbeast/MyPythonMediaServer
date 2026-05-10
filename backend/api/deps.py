"""
Subsonic auth dependency.

Every /rest/* endpoint takes the same auth params (u, p, t, s, v, c, f) so we
factor them into a single FastAPI dependency that:
    * pulls them from query string (Subsonic clients use GET)
    * verifies credentials via core.auth
    * returns a SubsonicContext object the handler uses

If auth fails, we return a 200 with status=failed in Subsonic's body — this
is the protocol's convention. NEVER return 401, it confuses clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Query, Request

from backend.core import auth as auth_core
from . import responses


@dataclass
class SubsonicContext:
    """Per-request context for Subsonic endpoints."""
    user_id: int
    username: str
    is_admin: bool
    client: str            # client identifier (Subsonic 'c' param)
    version: str           # client API version (Subsonic 'v' param)
    fmt: str               # 'json' | 'xml' | 'jsonp'
    callback: Optional[str]  # JSONP callback name


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

    user = auth_core.verify_subsonic_credentials(u, password=p, token=t, salt=s)
    if user is None:
        raise SubsonicAuthError(responses.ERR_AUTH, "Wrong username or password", fmt, callback)

    return SubsonicContext(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        client=c or "unknown",
        version=v or "1.16.1",
        fmt=fmt,
        callback=callback,
    )
