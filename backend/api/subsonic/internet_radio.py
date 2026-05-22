"""
Internet radio stations — getInternetRadioStations + create/update/delete
(Subsonic 1.9.0).

Stations are server-wide (the Subsonic model), stored in
internet_radio_stations (migration 10). Any authenticated user can manage them,
matching Navidrome/gonic; there's no per-user ownership. Station ids are bare
integers here (not the ar-/al-/tr- prefixed library ids), because they don't
refer to library entities — they're their own namespace.

createInternetRadioStation returns an empty ok envelope (Subsonic doesn't
return the new station object); clients re-fetch via getInternetRadioStations.
update/delete answer ERR_NOT_FOUND when the id doesn't exist so a client can
tell a no-op from a success.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Response

from backend.db import queries
from backend.db.connection import transaction

from .helpers import (
    _double_register,
    router,
    responses,
    SubsonicContext,
    subsonic_context,
)


def _parse_station_id(raw: str) -> Optional[int]:
    """Internet-radio ids are bare integers. Returns None for non-numeric."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@_double_register("getInternetRadioStations")
def get_internet_radio_stations(
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    stations = [
        {
            "id": str(s["id"]),
            "name": s["name"],
            "streamUrl": s["stream_url"],
            # Subsonic spells this field homePageUrl (capital P). Only emit it
            # when set, so clients don't get an empty attribute.
            **({"homePageUrl": s["homepage_url"]} if s.get("homepage_url") else {}),
        }
        for s in queries.list_internet_radio()
    ]
    return responses.ok(
        {"internetRadioStations": {"internetRadioStation": stations}},
        fmt=ctx.fmt,
        callback=ctx.callback,
    )


@_double_register("createInternetRadioStation")
def create_internet_radio_station(
    streamUrl: str = Query(...),
    name: str = Query(...),
    homepageUrl: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    with transaction():
        queries.create_internet_radio(name, streamUrl, homepageUrl)
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("updateInternetRadioStation")
def update_internet_radio_station(
    id: str = Query(...),
    streamUrl: str = Query(...),
    name: str = Query(...),
    homepageUrl: Optional[str] = Query(default=None),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    station_id = _parse_station_id(id)
    if station_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Station not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    with transaction():
        ok = queries.update_internet_radio(station_id, name, streamUrl, homepageUrl)
    if not ok:
        return responses.error(
            responses.ERR_NOT_FOUND, "Station not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)


@_double_register("deleteInternetRadioStation")
def delete_internet_radio_station(
    id: str = Query(...),
    ctx: SubsonicContext = Depends(subsonic_context),
) -> Response:
    station_id = _parse_station_id(id)
    if station_id is None:
        return responses.error(
            responses.ERR_NOT_FOUND, "Station not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    with transaction():
        ok = queries.delete_internet_radio(station_id)
    if not ok:
        return responses.error(
            responses.ERR_NOT_FOUND, "Station not found",
            fmt=ctx.fmt, callback=ctx.callback,
        )
    return responses.ok(fmt=ctx.fmt, callback=ctx.callback)
