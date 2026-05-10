"""API routers."""
from .subsonic import router as subsonic_router
from .web import router as web_router
from .deps import SubsonicAuthError
from .subsonic import subsonic_auth_exception_handler

__all__ = ["subsonic_router", "web_router", "SubsonicAuthError", "subsonic_auth_exception_handler"]
