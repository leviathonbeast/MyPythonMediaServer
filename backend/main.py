"""
Muse — Subsonic-compatible music server entry point.

Run with:
    uvicorn backend.main:app --reload
or via the helper:
    python -m backend.main

Startup sequence:
    1. Load settings (env / config.yaml).
    2. Create runtime directories (DB folder, artwork cache).
    3. Init DB connection + run migrations.
    4. Optionally kick off a startup scan.
    5. Start serving HTTP.

We do all of this in `lifespan` (FastAPI's modern startup/shutdown hook) so
that uvicorn workers initialise cleanly and shutdown is clean too.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.deps import subsonic_context, SubsonicContext
from backend.api.web import limiter
from backend.api import responses
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded


from backend.api import (
    SubsonicAuthError,
    subsonic_auth_exception_handler,
    subsonic_router,
    web_router,
)
from backend.config import ensure_directories, get_settings
from backend.db import init_db, run_migrations
from backend.scanner import start_scan_async, start_watcher, stop_watcher

# We configure logging eagerly at module import (before lifespan runs)
# because import-time log lines from submodules (e.g. database init when
# someone imports `backend.main` for tests) would otherwise be swallowed
# by Python's default WARNING-level root handler. The level is read from
# settings, which means MUSE_LOG_LEVEL=DEBUG works as expected.
_settings = get_settings()
logging.basicConfig(
    level=getattr(logging, _settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("muse")


def _redact_db_url(url: str) -> str:
    """Mask the password component of a database URL for safe logging.

    `postgresql://user:secret@host:5432/db` → `postgresql://user:***@host:5432/db`
    SQLite URLs have no credentials, so they pass through unchanged.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if not parsed.password:
        return url
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@", 1)
    return urlunparse(parsed._replace(netloc=netloc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook — runs once per worker."""
    settings = get_settings()
    log.info("Muse starting on %s:%d", settings.host, settings.port)

    ensure_directories(settings)
    init_db(settings)
    run_migrations()
    # Log the resolved URL with credentials masked, so it's immediately
    # obvious whether we landed on the dialect the operator intended.
    # Bare "Database ready at /data/library.db" is easy to skim past
    # when you thought you'd configured Postgres.
    log.info("Database ready: %s", _redact_db_url(settings.resolved_database_url()))

    _DEFAULT_SECRET = "muse-dev-secret-change-me"
    if settings.jwt_secret == _DEFAULT_SECRET:
        log.warning(
            "SECURITY: jwt_secret is set to the default development value. "
            "Set MUSE_JWT_SECRET (or jwt_secret in config.yaml) to a long random "
            "string before exposing this server to the internet."
        )

    if settings.scan_on_startup:
        log.info("Triggering startup scan")
        start_scan_async()

    if settings.scanner_watch_enabled:
        start_watcher()

    yield
    log.info("Muse shutting down")
    stop_watcher()


_boot_settings = get_settings()
_docs_url = "/docs" if _boot_settings.expose_docs else None
_redoc_url = "/redoc" if _boot_settings.expose_docs else None
_openapi_url = "/openapi.json" if _boot_settings.expose_docs else None

app = FastAPI(
    title="Muse — Subsonic-compatible music server",
    version="0.1.0",
    description=(
        "Subsonic API at /rest/*  •  Web UI API at /api/*  •  Web UI at /web/*."
    ),
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
)


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — only matters when the dev frontend (Vite, port 5173) hits the API
# on a different origin. In production, the frontend is served by the same
# host and CORS is a no-op.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers.
app.include_router(subsonic_router)
app.include_router(web_router)

# Subsonic responds with status=failed in body, never HTTP 401, so we map our
# auth exception to a Subsonic-shaped 200 response.
app.add_exception_handler(SubsonicAuthError, subsonic_auth_exception_handler)


# ---------------------------------------------------------------------------
# Root info doc — always served. The SPA lives at /web (below) so this gives
# monitoring tools and humans-with-curl a small, harmless landing page at /.
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def root():
    return JSONResponse(
        {
            "name": "Muse",
            "version": "0.1.0",
            "rest": "/rest/ping",
            "web": "/web/",
        }
    )


# ---------------------------------------------------------------------------
# Static frontend at /web — served when the Vite dist is present (i.e. in
# Docker / any production build). In dev, the Vite dev-server handles this
# instead. Vite must be built with `base: "/web/"` so emitted asset paths
# match the mount points below.
# ---------------------------------------------------------------------------
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.is_dir():
    # Hashed assets (JS/CSS bundles) get a long-lived cache header via
    # StaticFiles. The mount must be registered before the catch-all below.
    _assets = _dist / "assets"
    if _assets.is_dir():
        app.mount(
            "/web/assets",
            StaticFiles(directory=str(_assets)),
            name="static-assets",
        )

    @app.get("/web", include_in_schema=False)
    @app.get("/web/", include_in_schema=False)
    def web_root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/rest/{method:path}", include_in_schema=False)
    def subsonic_unknown_method(
        method: str,
        ctx: SubsonicContext = Depends(subsonic_context),
    ) -> Response:
        return responses.error(
            responses.ERR_GENERIC,
            f"Unknown Subsonic method: {method}",
            fmt=ctx.fmt,
            callback=ctx.callback,
        )

    @app.get("/web/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        """Serve matching files from dist, fall back to index.html for SPA routing."""
        candidate = _dist / path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))


# `python -m backend.main` convenience wrapper.
if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("backend.main:app", host=s.host, port=s.port, reload=False)
