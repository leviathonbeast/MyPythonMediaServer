"""
Authentication.

Two flows live side-by-side:

1. Frontend (web UI) login → JWT.
   POST /api/auth/login returns a signed token. The frontend keeps it in
   localStorage and sends it as `Authorization: Bearer <jwt>`.

2. Subsonic clients → token+salt or password.
   The Subsonic API spec authenticates with either ?u=&p=<password> or
   ?u=&t=<token>&s=<salt> where token = md5(password + salt). We accept both.
   This is NOT secure on the open internet — the wire protocol predates TLS
   being universal — but it's how every Subsonic client works, so we have to
   support it. Always run behind HTTPS.

Why bcrypt for storage:
    Standard, slow-by-design, salt-included. Verifying takes a few ms which
    is fine for login flows. For Subsonic-style auth on every request we
    cache the verified result per-username for 60s (see _verify_cache).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional, Tuple

import bcrypt
from jose import JWTError, jwt

from backend.config import get_settings
from backend.db import queries

# Tiny in-process cache of (username, password) -> (user_dict, expires_at).
# Keeps the per-request Subsonic auth path from doing bcrypt every time.
_verify_cache: Dict[Tuple[str, str], Tuple[Dict[str, Any], float]] = {}
_CACHE_TTL = 60.0  # seconds


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plaintext: str) -> str:
    """Bcrypt-hash a password. Cost 12 is the current sane default."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time check via bcrypt."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash — treat as auth failure.
        return False


# ---------------------------------------------------------------------------
# JWT (web UI)
# ---------------------------------------------------------------------------

def create_jwt(user: Dict[str, Any]) -> str:
    """
    Issue a JWT for a successfully-authenticated user.

    We embed only id, username, is_admin. Anything else can be looked up.
    Tokens expire per settings.jwt_expiry_hours.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "iat": now,
        "exp": now + settings.jwt_expiry_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Verify a JWT. Returns the payload or None on any failure."""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Subsonic auth
# ---------------------------------------------------------------------------

def verify_subsonic_credentials(
    username: str,
    password: Optional[str] = None,
    token: Optional[str] = None,
    salt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Authenticate a Subsonic-style request. Returns the user dict on success.

    Supports three forms:
      * password=<plain>             — older clients
      * password=enc:<hexstring>     — older "encoded" form (hex of the password)
      * token=<md5>, salt=<salt>     — modern clients

    The salt+token form means we have to retrieve the plaintext password to
    recompute md5(password+salt). We can't do that with bcrypt-hashed storage.
    Solution: we keep the bcrypt hash for the JWT login flow, AND we cache
    the plaintext-verified result here so subsequent requests are fast.

    For the salt+token path to work, the user must have logged in via the web
    UI at least once OR we have to be told the plaintext via the password
    parameter on the first request. This is a Subsonic-protocol limitation;
    every server faces it.
    """
    if not username:
        return None

    # Path A: plain password (or hex-encoded plain).
    if password is not None:
        plaintext = _decode_subsonic_password(password)
        return _verify_with_password(username, plaintext)

    # Path B: token+salt. Look up cached plaintext for this username.
    if token and salt:
        return _verify_with_token(username, token, salt)

    return None


def _decode_subsonic_password(p: str) -> str:
    """
    Some clients send `enc:<hex>` instead of plain. Decode if present.
    """
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return p  # fall through; verification will simply fail
    return p


def _verify_with_password(username: str, plaintext: str) -> Optional[Dict[str, Any]]:
    """Bcrypt-verify and cache for token-based subsequent requests."""
    user = queries.get_user_by_username(username)
    if user is None:
        return None
    if not verify_password(plaintext, user["password_hash"]):
        return None
    # Cache plaintext for future token+salt verification.
    _verify_cache[(username, plaintext)] = (
        {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])},
        time.time() + _CACHE_TTL,
    )
    # Also keep a reverse cache: username -> last known plaintext, so token
    # path can find it. We store plaintext only in-memory; never on disk.
    _last_plaintext[username] = plaintext
    return _verify_cache[(username, plaintext)][0]


def _verify_with_token(username: str, token: str, salt: str) -> Optional[Dict[str, Any]]:
    """
    Replay the Subsonic token formula. Requires we know the plaintext password
    for this user (cached from a prior password login).
    """
    plaintext = _last_plaintext.get(username)
    if plaintext is None:
        # No cached plaintext — client must POST password once first.
        return None
    expected = hashlib.md5((plaintext + salt).encode("utf-8")).hexdigest()
    if not _constant_time_eq(expected, token.lower()):
        return None
    user = queries.get_user_by_username(username)
    if user is None:
        return None
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}


# Username -> last known plaintext password (in-memory only). Populated when
# a user authenticates via the password path; consumed by the token+salt path.
_last_plaintext: Dict[str, str] = {}


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string compare to avoid timing attacks on token check."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
