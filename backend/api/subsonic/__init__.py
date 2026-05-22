from .helpers import router, _handle_auth_error
from ..deps import SubsonicAuthError
from fastapi import Request
from . import (
    system,
    users,
    searching,
    streaming,
    annotation,
    playlists,
    play_queue,
    albums,
    browsing,
    artist_info,
    album_info,
    sonic,
    similar,
    lyrics,
    bookmarks,
    internet_radio,
)


async def subsonic_auth_exception_handler(request: Request, exc: SubsonicAuthError):
    return _handle_auth_error(exc)
