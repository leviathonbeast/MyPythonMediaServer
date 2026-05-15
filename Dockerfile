# ---- Stage 1: Build the frontend --------------------------------------------
FROM node:20-alpine AS frontend-builder
WORKDIR /build

# Copy the lockfile first so `npm ci` can install reproducibly. Splitting
# this from the source copy keeps the install layer cached as long as deps
# don't change — frontend code edits won't bust it.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ---- Stage 2: Runtime -------------------------------------------------------
# Matching the dev environment (3.13) so runtime behaviour matches what the
# test suite was run against. Both 3.12 and 3.13 work for the code; pinning
# to 3.13 just removes one source of "works on my machine" drift.
FROM python:3.13-slim

# ffmpeg is needed for transcoding. tini gives us a real init so signal
# handling and zombie reaping are correct (uvicorn responds to SIGTERM,
# but child processes spawned by ffmpeg need a proper parent).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies — install before copying app code so dep changes are
# the only thing that bust this layer.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY backend/ ./backend/

# Built frontend from stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist

# Persistent data and music live outside the image
VOLUME ["/data", "/music"]

EXPOSE 4040

# Defaults — override via environment variables or docker-compose.
ENV MUSE_DATABASE_PATH=/data/library.db \
    MUSE_ARTWORK_CACHE_DIR=/data/artwork \
    MUSE_MUSIC_FOLDERS='["/music"]' \
    MUSE_HOST=0.0.0.0 \
    MUSE_PORT=4040 \
    PYTHONUNBUFFERED=1

# Run as a non-root user. We chown the mount points so the volumes are
# writable by `muse` on first start regardless of host UID. Numeric UID
# kept low so it's compatible with most NAS shares.
RUN useradd --uid 1000 --create-home --shell /sbin/nologin muse \
    && mkdir -p /data \
    && chown -R muse:muse /app /data
USER muse

# Healthcheck — pings the root route (serves the SPA / JSON info). We use
# Python rather than curl to avoid pulling in extra packages. Generous
# start-period because the first scan + migration can take a beat on big
# libraries.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4040/', timeout=4).status == 200 else 1)" || exit 1

# tini handles PID 1 duties (signals, reaping) before handing off to uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "4040"]
