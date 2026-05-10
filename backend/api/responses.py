"""
Subsonic response envelope.

Every Subsonic endpoint returns a wrapped response:

    {
      "subsonic-response": {
        "status": "ok" | "failed",
        "version": "1.16.1",
        "type": "muse",
        "serverVersion": "0.1.0",
        ... endpoint-specific payload ...
      }
    }

Or the XML equivalent. Clients send `?f=json` or `?f=xml` (or `?f=jsonp&callback=...`).
We honour all three. JSON is default because it's what modern clients use.

WHY a builder rather than per-endpoint hand-coding:
    Consistent envelope, consistent error format, single place to swap the
    XML serialiser if we ever need to.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import Response

from backend.config import get_settings


# Standard Subsonic error codes — clients pattern-match on these.
ERR_GENERIC          = 0
ERR_PARAMETER        = 10
ERR_CLIENT_VERSION   = 20
ERR_SERVER_VERSION   = 30
ERR_AUTH             = 40
ERR_TOKEN_AUTH       = 41
ERR_NOT_AUTHORIZED   = 50
ERR_NOT_FOUND        = 70


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
        "version":       settings.subsonic_api_version,
        "type":          settings.server_name.lower(),
        "serverVersion": settings.server_version,
        **inner,
    }
    fmt = (fmt or "json").lower()

    if fmt == "xml":
        body = '<?xml version="1.0" encoding="UTF-8"?>' + _to_xml("subsonic-response", envelope, attrs_for_root=True)
        return Response(content=body, media_type="application/xml")

    body = json.dumps({"subsonic-response": envelope}, separators=(",", ":"))

    if fmt == "jsonp" and callback:
        # JSONP: the response is callback(<json>); content-type is JS.
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
