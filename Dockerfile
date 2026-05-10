# ---- Stage 1: Build the frontend --------------------------------------------
FROM node:20-alpine AS frontend-builder
WORKDIR /build

COPY frontend/package*.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build


# ---- Stage 2: Runtime -------------------------------------------------------
FROM python:3.12-slim

# ffmpeg is needed for transcoding (optional — remove if you don't transcode).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
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
    MUSE_PORT=4040

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "4040"]
