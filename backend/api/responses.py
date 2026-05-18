"""
Subsonic response envelope.

Every Subsonic endpoint returns a wrapped response:

    {
      "subsonic-response": {
        "status": "ok" | "failed",
        "version": "1.16.1",
        "type": "muse",
        "serverVersion": "0.1.0",
        "openSubsonic": true,
        ... endpoint-specific payload ...
      }
    }

Or the XML equivalent. Clients send `?f=json` or `?f=xml` (or `?f=jsonp&callback=...`).
We honour all three. JSON is default because it's what modern clients use.

A critical quirk of the Subsonic protocol:
    ALL responses use HTTP 200, even errors. If a password is wrong, if a
    resource is not found, if the user lacks permission — the HTTP status is
    still 200. The real result is inside the body via "status": "failed" and
    an "error" object with a numeric code. Clients that receive a real HTTP
    error (like 401 or 404) assume a network or server problem and show a
    confusing "cannot connect" message instead of the actual error.

    The `error()` function below always returns HTTP 200. The `ok()` function
    also returns HTTP 200. The only difference is the "status" field in the body.

OpenSubsonic extension fields (required on every response):
    - `openSubsonic: true`  — signals we support OpenSubsonic extensions
    - `type`                — server software name, e.g. "muse"
    - `serverVersion`       — server software version, e.g. "0.1.0"

WHY a builder rather than per-endpoint hand-coding:
    Consistent envelope, consistent error format, single place to swap the
    XML serialiser if we ever need to.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import Response

from backend.config import get_settings

# JSONP callback must be a plain JS identifier (optionally dotted, e.g.
# `window.foo` for a method on a namespaced object). Anything else is
# injection territory: a callback of `alert(1);//` would render the
# response as `alert(1);//(...);` and execute when included via
# `<script src=...>`. Capped at 64 chars because a real JSONP callback
# is at most a function name.
_JSONP_CALLBACK_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]{0,63}")


# Standard Subsonic error codes — clients decide what to show based on these.
# For example, code 40 (wrong credentials) triggers a re-login prompt; code 70
# (not found) shows "album/track not available". They're part of the spec, not
# arbitrary — if you change them, existing clients will misinterpret the errors.
ERR_GENERIC          = 0    # catch-all (e.g. duplicate username on createUser)
ERR_PARAMETER        = 10   # required parameter missing or invalid
ERR_CLIENT_VERSION   = 20   # client's API version too old
ERR_SERVER_VERSION   = 30   # server's API version too old for the client
ERR_AUTH             = 40   # wrong username or password
ERR_TOKEN_AUTH       = 41   # token authentication not supported (we DO support it)
# OpenSubsonic extension codes (apiKeyAuthentication extension)
ERR_AUTH_MECH        = 42   # authentication mechanism not supported
ERR_AUTH_CONFLICT    = 43   # multiple conflicting auth mechanisms
ERR_INVALID_API_KEY  = 44   # invalid API key
ERR_NOT_AUTHORIZED   = 50   # user doesn't have permission for this action
ERR_NOT_FOUND        = 70   # resource (user, album, track) doesn't exist


def ok(payload: Optional[Dict[str, Any]] = None, fmt: str = "json", callback: Optional[str] = None) -> Response:
    """Build a success response. payload is merged into the envelope."""
    return _build({"status": "ok", **(payload or {})}, fmt=fmt, callback=callback)


def error(code: int, message: str, fmt: str = "json", callback: Optional[str] = None) -> Response:
    """Build a Subsonic-style error response (always HTTP 200, code in body)."""
    return _build(
        {"status": "failed", "error": {"code": code, "message": message}},
        fmt=fmt, callback=callback,
    )


def _build(inner: Dict[str, Any], fmt: str, callback: Optional[str]) -> Response:
    settings = get_settings()
    envelope = {
        "status":        inner.get("status", "ok"),
        "version":       settings.subsonic_api_version,
        "type":          settings.server_name.lower(),
        "serverVersion": settings.server_version,
        "openSubsonic":  True,
        **inner,
    }
    fmt = (fmt or "json").lower()

    if fmt == "xml":
        body = '<?xml version="1.0" encoding="UTF-8"?>' + _to_xml("subsonic-response", envelope, attrs_for_root=True)
        return Response(content=body, media_type="application/xml")

    body = json.dumps({"subsonic-response": envelope}, separators=(",", ":"))

    if fmt == "jsonp" and callback and _JSONP_CALLBACK_RE.fullmatch(callback):
        # JSONP: the response is callback(<json>); content-type is JS.
        # A non-matching callback silently falls through to plain JSON —
        # safer than echoing attacker-controlled text into a JS body.
        return Response(content=f"{callback}({body});", media_type="application/javascript")

    return Response(content=body, media_type="application/json")


# ---------------------------------------------------------------------------
# JSON -> XML conversion for Subsonic
# ---------------------------------------------------------------------------

def _to_xml(tag: str, value: Any, attrs_for_root: bool = False) -> str:
    """
    Subsonic's XML format uses attributes for scalars and child elements for
    arrays/objects. Convert our dicts accordingly.

    Convention:
        scalar fields  -> attributes on the parent
        list fields    -> repeated child elements named after the key
        dict fields    -> single child element

    The root element gets a fixed xmlns attribute.
    """
    if isinstance(value, dict):
        scalar_attrs: list[str] = []
        children: list[str] = []
        for k, v in value.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                # Subsonic uses lowercase 'true'/'false' for booleans.
                if isinstance(v, bool):
                    v = "true" if v else "false"
                scalar_attrs.append(f'{k}="{xml_escape(str(v), {chr(34): "&quot;"})}"')
            elif isinstance(v, list):
                # Pluralised key collapses to repeated singular child? Subsonic
                # doesn't do that — it uses the key as-is. Each list item becomes
                # a child element named `k`.
                for item in v:
                    children.append(_to_xml(k, item))
            elif isinstance(v, dict):
                children.append(_to_xml(k, v))

        attr_str = (" " + " ".join(scalar_attrs)) if scalar_attrs else ""
        if attrs_for_root:
            attr_str += ' xmlns="http://subsonic.org/restapi"'

        if not children:
            return f"<{tag}{attr_str}/>"
        return f"<{tag}{attr_str}>{''.join(children)}</{tag}>"

    # Scalars at root would be unusual but fine.
    return f"<{tag}>{xml_escape(str(value))}</{tag}>"
