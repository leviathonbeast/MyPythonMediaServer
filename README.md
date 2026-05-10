# Muse

A self-hosted, Subsonic-compatible music server. Designed for personal
archives in the 100k–500k track range.

- **Backend:** Python 3.11+, FastAPI, SQLite (WAL), FFmpeg for transcoding
- **Frontend:** TypeScript + Vite, no UI framework, ~9 KB gzipped
- **Protocol:** Subsonic API 1.16.1 — works with Symfonium, play:Sub,
  DSub, Substreamer, Sonixd, and any other Subsonic-compatible client.

---

## Features

**Phase 1 (working):**

- Recursive library scan across local directories and network mounts
- Incremental scans — unchanged files are skipped via `(mtime, size)` diff
- Metadata via mutagen → ffprobe → filename, with parent-directory fallback
- Embedded artwork extraction (ID3 APIC, MP4 `covr`, FLAC pictures)
  with folder-art fallback (`cover.jpg`, `folder.png`, etc.)
- HTTP Range requests on raw streams (instant seek in any client)
- On-the-fly transcoding presets (mp3 320/192/128, opus, ogg)
- Subsonic endpoints: `ping`, `getLicense`, `getMusicFolders`, `getIndexes`,
  `getMusicDirectory`, `getArtist`, `getAlbum`, `getAlbumList`,
  `getAlbumList2`, `search3`, `stream`, `download`, `getCoverArt`, `getUser`
- Web UI: login, A–Z artist index, album grid, single-album view with
  tracklist, search, persistent player dock, library/scan admin

**Phase 2 (placeholders return valid empty responses, marked TODO):**

- Playlists (CRUD)
- Starred / favourites
- Scrobble / play-count statistics
- Now-playing roster

---

## Installation

### 1. Prerequisites

- **Python 3.11 or newer**
- **FFmpeg** (must include `ffmpeg` and `ffprobe` on `PATH`):
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - macOS (Homebrew): `brew install ffmpeg`
  - Windows (Chocolatey): `choco install ffmpeg`
- **Node.js 18+** (only if you want to run the dev frontend)

### 2. Backend

```bash
git clone <this-repo> muse && cd muse

# Optional but recommended: a virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r backend/requirements.txt
```

### 3. Configuration

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

At minimum, set:

- `music_folders:` — list of paths to scan
- `admin_password:` — change from the default `admin`
- `jwt_secret:` — set to a long random string

Every setting can also be overridden by an env var with the `MUSE_`
prefix (e.g. `MUSE_DATABASE_PATH=/var/muse/muse.db`).

### 4. Run the backend

```bash
# from the repo root
python -m backend.main
# or, equivalently:
uvicorn backend.main:app --host 0.0.0.0 --port 4040
```

The server listens on `http://0.0.0.0:4040` by default. On first start
it creates the database, runs migrations, and seeds the admin user.

### 5. Frontend (development)

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /rest and /api to :4040
```

For production, build static assets:

```bash
npm run build    # output in frontend/dist/
```

Serve `frontend/dist/` from any static host (or behind nginx in front of
the backend). The frontend hits same-origin `/rest/*` and `/api/*`.

---

## Trigger a scan

The first time you run Muse the database is empty. Two ways to populate it:

- **Web UI:** sign in → Workshop → "Start a fresh scan"
- **API:** `curl -X POST http://localhost:4040/api/scan -H "Authorization: Bearer $JWT"`

Re-scans are safe to run any time. Unchanged files are skipped (they're
detected by `(path, mtime, size)`), so a re-scan of a 200k-track library
typically takes seconds, not hours.

---

## Maintenance & garbage collection

Over time a music server collects a few kinds of cruft that aren't
caught by ordinary scan-time deletion:

- Albums that became empty when every track on them got re-tagged
- Artists that lost their last album the same way
- Favourite/starred entries pointing at things that no longer exist
- Cover-art files in the cache directory that no album references anymore
- Database pages left fragmented after large delete cycles

A GC pass that handles the first four runs **automatically at the end of
every scan**. It's cheap — well under a second on a 500k-track library.

For the fifth (database fragmentation) and for manual triggers, two
admin endpoints are available:

```bash
# routine GC (also runs after every scan)
curl -X POST http://localhost:4040/api/maintenance/gc \
     -H "Authorization: Bearer $JWT"

# GC + VACUUM — rewrites the .db file compactly. Acquires an exclusive
# lock for its duration; expect a few seconds of read/write blocking.
curl -X POST http://localhost:4040/api/maintenance/vacuum \
     -H "Authorization: Bearer $JWT"
```

Both return a JSON breakdown: empty albums removed, dangling favourites
removed, orphan artwork files & bytes freed, before/after database size.
The Workshop view in the web UI shows the same controls and renders the
last result.

VACUUM is worth running occasionally — say monthly, or after a big
library re-organisation. The other steps are essentially free and run on
their own.

---

## Connecting a Subsonic client

Point any Subsonic-compatible app at:

- **Server:** `http://your-host:4040` (or your reverse-proxy URL)
- **Username:** what you set as `admin_username`
- **Password:** what you set as `admin_password`

Tested against:

- **Symfonium** (Android) — recommended
- **play:Sub** (iOS)
- **DSub** (Android)
- **Substreamer** (iOS / Android)
- **Sonixd** (desktop)

If a client offers "Use legacy authentication" or "Send password as
plaintext", **disable it** and prefer the token+salt scheme — Muse
supports both, but token+salt is safer over plain HTTP.

---

## Security notes

The Subsonic protocol predates modern auth. There are two facts worth
internalising before exposing Muse to the open internet:

1. **The protocol authenticates every call with the user's password**,
   either as a query-string `p=...` or as `t=md5(password+salt) & s=salt`.
   Either way, the server must be able to recover the plaintext password
   to verify against its bcrypt hash. Muse caches plaintext **in memory
   only**, after the first successful login. Nothing is written to disk
   in cleartext.

2. **Run Muse behind HTTPS in production**, full stop. With a TLS
   reverse proxy (nginx, Caddy, Traefik) the password never travels
   in cleartext on the wire. Without it, an attacker on the same
   network can sniff your password from a single Subsonic request.

Other notes:

- The web UI keeps your username and password in `localStorage` so
  that subsequent Subsonic calls can authenticate without re-prompting.
  This is roughly equivalent in threat-model terms to a session cookie.
  Sign out from the sidebar to wipe it.
- Set `jwt_secret` to a long random string. If it stays empty, Muse
  generates one at startup, which means every restart logs everyone out.
- Default credentials are `admin` / `admin`. Change them.

---

## Architecture

```
                ┌─────────────────────────────────────────┐
                │           FastAPI application           │
                │                                         │
   browsers ───▶│  /api/*       (web UI, JWT-bearer)      │
                │  /rest/*      (Subsonic, password auth) │
                └─────────────────────────────────────────┘
                                  │
                                  ▼
                ┌─────────────────────────────────────────┐
                │              Core services              │
                │   library  ·  search  ·  auth           │
                └─────────────────────────────────────────┘
                                  │
        ┌─────────────────┬───────┴────────┬─────────────────┐
        ▼                 ▼                ▼                 ▼
    Scanner          Streaming        SQLite (WAL)      Artwork cache
  walker / parse    range + ffmpeg    indexed schema    sha1-named files
```

Key design decisions:

- **SQLite, not Postgres.** Personal music libraries don't need
  multi-master writes; SQLite in WAL mode handles concurrent readers
  during scan writes. `cache_size = -50000` gives us ~200 MB of page
  cache, which keeps a 500k-track library hot.
- **Custom SQL layer over SQLAlchemy.** Predictable query shapes,
  no ORM overhead on the hot paths (browse/search), no surprise N+1s.
- **Subsonic id prefixes.** Artists are `ar-N`, albums `al-N`, tracks
  `tr-N`. Opaque to clients but typed for us — stops the whole class of
  "I passed an album id where I needed an artist id" bugs.
- **Streaming via subprocess pipe.** Transcoded audio is read out of
  ffmpeg's stdout in 64 KB chunks; no full file is ever held in memory,
  and disconnect-mid-stream cleanly terminates the encoder.
- **Hash-named artwork cache.** Files are named by `sha1(bytes)[:16].ext`
  so identical art across 50 albums is stored once.
- **Both `/rest/X` and `/rest/X.view`** are registered, because some
  legacy Subsonic clients hard-code one form or the other.

---

## Project layout

```
muse-server/
├── backend/
│   ├── main.py              # FastAPI app, lifespan, CORS, routers
│   ├── config/              # Pydantic Settings + YAML loader
│   ├── api/                 # HTTP layer
│   │   ├── subsonic.py      #   /rest/* — Subsonic-compatible router
│   │   ├── web.py           #   /api/*  — internal web UI router
│   │   ├── responses.py     #   Subsonic envelope (json/xml/jsonp)
│   │   └── deps.py          #   FastAPI dependencies (auth, ctx)
│   ├── core/                # Domain logic
│   │   ├── auth.py          #   bcrypt, JWT, Subsonic token+salt
│   │   ├── library.py       #   id helpers, Subsonic shape mappers
│   │   └── search.py
│   ├── db/                  # SQLite layer
│   │   ├── schema.sql       #   Versioned schema
│   │   ├── connection.py    #   Thread-local connections
│   │   ├── migrations.py    #   Versioned migrations
│   │   └── queries.py       #   All hand-written SQL
│   ├── scanner/             # Library scan
│   │   ├── walker.py        #   os.scandir-based walker
│   │   ├── metadata.py      #   mutagen → ffprobe → filename
│   │   ├── artwork.py       #   embedded + folder-art extraction
│   │   └── scanner.py       #   orchestration, progress, threads
│   └── streaming/           # Audio streaming
│       ├── presets.py       #   transcode preset table
│       ├── transcoder.py    #   ffmpeg subprocess pipe
│       └── streamer.py      #   range-aware streamer
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.ts          # Hash router, shell, mounts player
│       ├── api.ts           # Subsonic + JWT clients
│       ├── auth.ts          # Login state
│       ├── player.ts        # HTML5 audio + queue + dock
│       ├── style.css        # Editorial-zine aesthetic
│       └── views/           # login, library, albums, album, artist, search, settings
└── config.example.yaml
```

---

## License

Your project, your license. (Recommendation: AGPL-3.0 if you intend to
distribute, MIT for personal use.)
