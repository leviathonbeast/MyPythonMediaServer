from __future__ import annotations
from typing import Optional
from fastapi import Depends, Query, Request, Response
from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)
from backend.db import queries
from backend.scanner import get_progress, start_scan_async

# ---- ping -----------------------------------------------------------------


@_double_register("ping")
def ping(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """
    Health check + auth probe. Clients call this to verify their credentials.

    Returns just the envelope, no payload.
    """
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


# ---- getLicense (most clients call this; cheap to satisfy) ----------------


@_double_register("getLicense")
def get_license(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    """
    Subsonic upstream uses this to gate features behind a paid license.
    We're FOSS, so we always say 'valid'.
    """
    return responses.ok(
        {
            "license": {
                "valid": True,
                "email": "noreply@example.com",
                "trialExpires": None,
                "licenseExpires": None,
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- getMusicFolders ------------------------------------------------------


@_double_register("getMusicFolders")
def get_music_folders(ctx: SubsonicContext = Depends(subsonic_context)) -> Response:
    folders = queries.list_music_folders()
    return responses.ok(
        {
            "musicFolders": {
                "musicFolder": [{"id": f["id"], "name": f["name"]} for f in folders]
            }
        },
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- getOpenSubsonicExtensions ------------------------------------------------------
@_double_register("getOpenSubsonicExtensions")
def get_open_subsonic_extensions(request: Request) -> Response:
    """
    Publicly accessible (no auth required) endpoint that advertises which
    OpenSubsonic extensions this server supports.
    """
    extensions = [
        {"name": "httpFormPost", "versions": [1]},
    ]
    return responses.ok(
        {"openSubsonicExtensions": extensions},
        fmt="json",
    )


# ---- getOpenScanStatus ------------------------------------------------------
@_double_register("getScanStatus")
def get_scan_status(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    p = get_progress()
    return responses.ok(
        {"scanStatus": {"scanning": p.running, "count": p.files_parsed}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


# ---- startScan ------------------------------------------------------
@_double_register("startScan")
def start_scan(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    start_scan_async()
    p = get_progress()

    return responses.ok(
        {"scanStatus": {"scanning": p.running, "count": p.files_parsed}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )
