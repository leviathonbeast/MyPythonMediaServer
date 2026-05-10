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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import (
    SubsonicAuthError,
    subsonic_auth_exception_handler,
    subsonic_router,
    web_router,
)
from backend.config import ensure_directories, get_settings
from backend.db import init_db, run_migrations
from backend.scanner import start_scan_async

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook — runs once per worker."""
    settings = get_settings()
    log.info("Muse starting on %s:%d", settings.host, settings.port)

    ensure_directories(settings)
    init_db(settings)
    run_migrations()
    log.info("Database ready at %s", settings.database_path)

    if settings.scan_on_startup:
        log.info("Triggering startup scan")
        start_scan_async()

    yield
    log.info("Muse shutting down")


app = FastAPI(
    title="Muse — Subsonic-compatible music server",
    version="0.1.0",
    description=(
        "Subsonic API at /rest/*  •  Web UI API at /api/*  •  "
        "OpenAPI docs at /docs."
    ),
    lifespan=lifespan,
)

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
# Static frontend — served when the Vite dist is present (i.e. in Docker /
# any production build). In dev, the Vite dev-server handles this instead.
# ---------------------------------------------------------------------------
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.is_dir():
    # Hashed assets (JS/CSS bundles) get a long-lived cache header via
    # StaticFiles. The mount must be registered before the catch-all below.
    _assets = _dist / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="static-assets")

    @app.get("/", include_in_schema=False)
    def root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        """Serve matching files from dist, fall back to index.html for SPA routing."""
        candidate = _dist / path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))
else:
    @app.get("/", include_in_schema=False)
    def root():
        """Dev-mode info endpoint — replaced by the SPA in production."""
        return JSONResponse({
            "name":    "Muse",
            "version": "0.1.0",
            "docs":    "/docs",
            "rest":    "/rest/ping",
            "web":     "Open the frontend dev server at http://localhost:5173",
        })


# `python -m backend.main` convenience wrapper.
if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run("backend.main:app", host=s.host, port=s.port, reload=False)
