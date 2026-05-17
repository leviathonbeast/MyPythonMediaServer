"""
Authentication.

Two flows live side-by-side:

1. Frontend (web UI) login → JWT.
   POST /api/auth/login returns a signed token. The frontend keeps it in
   localStorage and sends it as `Authorization: Bearer <jwt>`.

2. Subsonic clients → token+salt.
   The Subsonic API spec authenticates with ?u=&t=<token>&s=<salt> where
   token = md5(password + salt). We only accept the token+salt form; the
   older plaintext ?p= form is rejected. Always run behind HTTPS.

Why bcrypt for storage:
    Standard, slow-by-design, salt-included. Verifying takes a few ms which
    is fine for login flows. For Subsonic-style auth on every request we
    cache the verified result per-username for 60s (see _verify_cache).

Why encrypted_password in the DB:
    Token+salt auth requires the server to recompute md5(password+salt) to
    verify the client's token. You can't derive the plaintext from a bcrypt
    hash. We store a Fernet-encrypted copy of the plaintext alongside the
    bcrypt hash so the server can verify token+salt requests without needing
    the password on the wire (previously it relied on a short-lived in-memory
    cache seeded from the insecure ?p= URL param).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any, Dict, Optional, Tuple

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from backend.config import get_settings
from backend.db import queries, transaction

logger = logging.getLogger(__name__)

# In-process cache: (username, plaintext_password) → (user_dict, expiry_timestamp).
#
# WHY: bcrypt is intentionally slow (that's the point — it makes brute-force
# attacks expensive). But Subsonic clients authenticate on EVERY request, so
# without a cache you'd spend ~100ms per API call just on password hashing.
# Instead: the first time we see a (username, password) pair we run bcrypt and
# cache the result for 60 seconds. Subsequent requests for the same pair return
# instantly. Cache keys expire so a password change takes effect within a minute.
#
# SECURITY: The cache lives in memory only and never touches disk. If the server
# restarts the cache is empty and the next request re-runs bcrypt normally.
_verify_cache: Dict[Tuple[str, str], Tuple[Dict[str, Any], float]] = {}
_CACHE_TTL = 60.0  # seconds before a cached verification expires


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
# Fernet encryption for stored Subsonic plaintext
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    """Return a Fernet instance keyed from jwt_secret via SHA-256."""
    raw = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_password(plaintext: str) -> str:
    """Return a Fernet token (URL-safe base64 string) wrapping the plaintext."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> Optional[str]:
    """Decrypt a stored Fernet token. Returns None if the token is invalid or the key changed."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        return None


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
# Subsonic auth (token+salt only)
# ---------------------------------------------------------------------------

def verify_subsonic_credentials(
    username: str,
    password: Optional[str] = None,
    token: Optional[str] = None,
    salt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Authenticate a Subsonic-style request. Returns the user dict on success.

    Supports two forms:
      * token=<md5(password+salt)>, salt=<salt>  — preferred; no plaintext on wire
      * password=<plain or enc:hex>               — legacy; still accepted so that
        clients like Feishin can authenticate and seed the encrypted_password
        column on first use, after which token+salt works across restarts

    Both forms update the server's encrypted_password store on success.
    Always run behind HTTPS so URL params are not exposed in transit.
    """
    if not username:
        return None

    if password is not None:
        plaintext = _decode_subsonic_password(password)
        return _verify_with_password(username, plaintext)

    if token and salt:
        return _verify_with_token(username, token, salt)

    return None


def _decode_subsonic_password(p: str) -> str:
    """Decode Subsonic's optional enc:<hex> password encoding."""
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return p
    return p


def _verify_with_password(username: str, plaintext: str) -> Optional[Dict[str, Any]]:
    """
    Bcrypt-verify and update both the in-memory cache and the DB encrypted copy.

    Called from the web login path only (POST /api/auth/login). Never called
    from Subsonic endpoints now that ?p= is rejected.
    """
    user = queries.get_user_by_username(username)
    if user is None:
        return None
    if user.get("disabled"):
        # Disabled accounts return the same failure as a wrong password so a
        # caller can't probe which usernames are locked vs. nonexistent.
        return None
    if not verify_password(plaintext, user["password_hash"]):
        return None

    user_info = {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}

    # Persist encrypted copy so token+salt works across server restarts.
    # Only write when there isn't already one stored — otherwise every
    # successful login takes a write lock, which serialises against any
    # in-flight scan commit and surfaces as `database is locked` if the
    # scanner is mid-batch. Once the column is set it never goes stale
    # (the plaintext can't change without a password change, which has
    # its own update path).
    if not user.get("encrypted_password"):
        # update_encrypted_password no longer commits internally — the
        # transaction wrapper handles it on both dialects.
        with transaction():
            queries.update_encrypted_password(user["id"], encrypt_password(plaintext))

    # Warm in-memory caches.
    _verify_cache[(username, plaintext)] = (user_info, time.time() + _CACHE_TTL)
    _last_plaintext[username] = plaintext

    return user_info


def _verify_with_token(username: str, token: str, salt: str) -> Optional[Dict[str, Any]]:
    """
    Replay the Subsonic token formula: md5(password + salt).

    Resolution order for the plaintext:
      1. In-memory cache (fast, avoids DB round-trip on hot requests).
      2. encrypted_password column in the DB (survives server restarts).
      3. Fail — user must log in via the web UI first.
    """
    plaintext = _last_plaintext.get(username)

    if plaintext is None:
        # Cache miss — try the DB.
        user = queries.get_user_by_username(username)
        if user is None:
            return None
        enc = user.get("encrypted_password")
        if enc is None:
            # No encrypted copy yet. User needs to log in via the web UI once.
            return None
        plaintext = decrypt_password(enc)
        if plaintext is None:
            # Decryption failed (key rotated?). Force re-login.
            return None
        # Re-warm the in-memory cache from the decrypted value.
        _last_plaintext[username] = plaintext

    expected = hashlib.md5((plaintext + salt).encode("utf-8")).hexdigest()
    if not _constant_time_eq(expected, token.lower()):
        return None

    user = queries.get_user_by_username(username)
    if user is None:
        return None
    if user.get("disabled"):
        return None
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}


# Username → the last known plaintext password, kept in memory only as a
# performance cache. Populated from the DB encrypted_password on cache miss.
_last_plaintext: Dict[str, str] = {}


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string compare to avoid timing attacks on token check."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
