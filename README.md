# Muse

A self-hosted, Subsonic-compatible music server built for personal archives
in the 100 k – 500 k track range.

| Layer | Stack |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLite (WAL mode), FFmpeg |
| Frontend | TypeScript + Vite — no UI framework, ~54 KB JS / ~21 KB CSS |
| Protocol | OpenSubsonic API 1.16.1 (100% compliant) |

Compatible with **Symfonium**, **play:Sub**, **DSub**, **Substreamer**,
**Sonixd**, and any other Subsonic/OpenSubsonic client.

---

## What's implemented

### User management & permissions

Muse has a full role-based permission system. There are two classes of user:

- **Admin users** can manage the library (scan, add folders), manage other users, and access all music.
- **Regular users** can browse and stream music, and change their own password. They cannot see other users' accounts or trigger library scans.

User accounts can be managed from:
- The web UI (Settings → Users) — admin only
- The `/api/users/*` REST endpoints — admin only
- The Subsonic `/rest/createUser`, `/rest/updateUser`, `/rest/deleteUser` endpoints — admin only

#### Subsonic roles (per user)

Each user has individual role flags that control what Subsonic clients can do. These match the OpenSubsonic 1.16.1 spec exactly:

| Role | Default | What it controls |
|---|---|---|
| `adminRole` | false | Can manage the server |
| `settingsRole` | true | Can change personal settings |
| `streamRole` | true | Can stream audio |
| `downloadRole` | false | Can download files |
| `uploadRole` | false | Can upload files |
| `playlistRole` | true | Can create/edit playlists |
| `coverArtRole` | false | Can change cover art |
| `commentRole` | false | Can comment |
| `podcastRole` | false | Can manage podcasts |
| `jukeboxRole` | false | Can control jukebox mode |
| `shareRole` | false | Can create public shares |
| `videoConversionRole` | false | Can convert video |

---

### Library management

- Recursive scan across any number of local or network-mounted directories
- Incremental rescans — files unchanged since last scan are skipped via
  `(mtime, size)` diff; a 200 k-track rescan typically takes seconds
- Metadata pipeline: mutagen → ffprobe → filename, with parent-directory
  fallback for untagged files
- Release-type tagging (`album`, `EP`, `single`, `compilation`, `live`, …)
  sourced from the MusicBrainz `RELEASETYPE` tag
- Embedded artwork extraction (ID3 APIC, MP4 `covr`, FLAC `PICTURE`) with
  folder-art fallback (`cover.jpg`, `folder.png`, etc.)
- Deduplication of identical artwork by content hash — 50 albums sharing
  the same art store it once
- Post-scan GC: removes empty albums/artists, dangling starred rows, and
  orphaned artwork files automatically

### Streaming

- HTTP Range support on raw streams — instant seek in every client
- On-the-fly transcoding via FFmpeg subprocess pipe (never loads the whole
  file into memory)
- Transcode presets: MP3 320 / 192 / 128, Opus 128 / 96, OGG 192
- Per-user quality cap: server-side `max_streaming_bitrate` clamps any
  request that would exceed it
- Master kill-switch (`transcoding_enabled: false`) to bypass all transcoding
  on local-network installs

### Web UI

- **Library** — A–Z artist index with two view modes:
  - *List mode* — per-letter buckets, collapsed to 5 with a "show more" toggle
  - *Grid/poster mode* — circular artist cards; shows album cover art
    immediately, upgrades to a Last.fm artist photo when one is available
- **Albums** — paginated album grid; sort by newest, A–Z, year, or random
- **Album** — full tracklist with disc grouping, cover art, play all
- **Artist** — albums grouped by release type, Last.fm biography and tags
- **Search** — full-text across artists, albums, and tracks; per-section
  "load more" backed by server-side offsets
- **Settings / Workshop** — trigger scans, manage music folders, manage users,
  configure transcoding quality, run GC / vacuum
- **Persistent player dock** — queue, scrubber, volume, skip/prev,
  stream-format badge showing the actual delivered format and bitrate

---

### Subsonic / OpenSubsonic endpoints

#### Fully implemented

| Endpoint | Notes |
|---|---|
| `ping` | Auth probe |
| `getLicense` | Always returns valid (FOSS) |
| `getMusicFolders` | All configured roots |
| `getIndexes` | A–Z artist index; includes `coverArt` per artist |
| `getMusicDirectory` | Artist and album directory traversal |
| `getAlbum` | Album with full track list; returns `name` + `artistId` (AlbumID3) |
| `getAlbumList` | All sort modes including random; `byYear` and `byGenre` filters |
| `getAlbumList2` | ID3 variant; same data shape as `getAlbumList` |
| `getSong` | Single track by id |
| `search3` | Artists / albums / tracks with server-side pagination |
| `stream` | Raw + transcoded, Range-aware; `timeOffset` not supported |
| `download` | Raw only (no transcode) |
| `getCoverArt` | Serves from artwork cache; `size` accepted but not resized |
| `getUser` | Own account for regular users; any account for admins |
| `getUsers` | All accounts; admin only |
| `createUser` | Admin only; all Subsonic role flags supported |
| `updateUser` | Admin only; update any user field or role |
| `deleteUser` | Admin only; cannot delete own account |
| `changePassword` | Any user can change their own; admin can change any |
| `getOpenSubsonicExtensions` | No auth required; advertises supported extensions |

#### OpenSubsonic extensions

All responses include the required OpenSubsonic envelope fields:
- `openSubsonic: true`
- `type: "muse"`
- `serverVersion: "0.1.0"`

Song objects include OpenSubsonic extended fields: `mediaType`, `genres[]`,
`artists[]`, `albumArtists[]`, `displayArtist`, `displayAlbumArtist`.

#### Stubs — valid empty responses, not yet implemented

These return well-formed Subsonic responses so clients don't error out,
but carry no real data yet.

| Endpoint | Status |
|---|---|
| `getPlaylists` | Returns empty list |
| `getPlaylist` | Returns 404 |
| `createPlaylist` | Returns stub playlist |
| `getStarred` / `getStarred2` | Returns empty lists |
| `star` / `unstar` | No-op success |
| `scrobble` | No-op success |
| `getNowPlaying` | Returns empty list |

#### Not yet implemented

| Endpoint | Category |
|---|---|
| `getArtists` / `getArtist` | ID3 browse (artist detail) |
| `getGenres` / `getSongsByGenre` | Genre browse |
| `getArtistInfo` / `getArtistInfo2` | Artist bio via Subsonic |
| `getAlbumInfo` / `getAlbumInfo2` | Album notes via Subsonic |
| `getSimilarSongs` / `getSimilarSongs2` | Similar-artist recommendations |
| `getTopSongs` | Charted tracks by artist |
| `getRandomSongs` | Random track selection |
| `search2` | Legacy search (pre–search3) |
| `updatePlaylist` / `deletePlaylist` | Playlist management |
| `getPlayQueue` / `savePlayQueue` | Cross-device queue sync |
| `createBookmark` / `getBookmarks` / `deleteBookmark` | Audiobook / podcast position |
| `getPodcasts` / `getNewestPodcasts` | Podcast feeds |
| `getInternetRadioStations` | Internet radio |
| `getScanStatus` / `startScan` | Subsonic-native scan control |

---

## Installation

### Docker (recommended)

A single container includes the built frontend, Python backend, and ffmpeg.

```bash
# 1. Clone the repo
git clone <this-repo> muse && cd muse

# 2. Set your music path — edit the volume under services.muse.volumes
$EDITOR docker-compose.yml

# 3. Start
docker compose up -d
```

Open `http://localhost:4040`. Default login: `admin` / `admin`.

**Key settings in `docker-compose.yml`:**

| Variable | Default | Notes |
|---|---|---|
| `MUSE_JWT_SECRET` | `change-me-in-production` | **Change before exposing to a network** |
| `MUSE_ADMIN_USERNAME` | `admin` | Applied once on first run only |
| `MUSE_ADMIN_PASSWORD` | `admin` | Applied once on first run only |
| `MUSE_SCAN_ON_STARTUP` | `true` | Scans the library on every container start |
| `MUSE_LASTFM_API_KEY` | — | Enables artist bios and photos in the web UI |
| `MUSE_MAX_STREAMING_BITRATE` | — | Caps transcoding output (kbps) |

**Multiple music folders:**

```yaml
# docker-compose.yml
environment:
  MUSE_MUSIC_FOLDERS: '["/music/lossless", "/music/podcasts"]'
volumes:
  - /mnt/nas/lossless:/music/lossless:ro
  - /mnt/nas/podcasts:/music/podcasts:ro
```

**Without docker-compose:**

```bash
docker build -t muse .
docker run -d \
  -p 4040:4040 \
  -v "$(pwd)/data":/data \
  -v /path/to/your/music:/music:ro \
  -e MUSE_JWT_SECRET=change-me \
  muse
```

---

### Manual install

#### Prerequisites

- **Python 3.11+**
- **FFmpeg** (`ffmpeg` and `ffprobe` on `PATH`):
  ```bash
  # Debian / Ubuntu
  sudo apt install ffmpeg
  # macOS
  brew install ffmpeg
  ```
- **Node.js 18+** — only needed to build or develop the frontend

### Backend

```bash
git clone <this-repo> muse && cd muse

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r backend/requirements.txt
```

### Configuration

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Minimum required changes:

```yaml
music_folders:
  - /path/to/your/music

admin_password: change-me
jwt_secret: a-long-random-string
```

Every setting can also be overridden by an environment variable prefixed
with `MUSE_` (e.g. `MUSE_DATABASE_PATH=/var/muse/library.db`).

Key optional settings:

```yaml
# Transcode everything to MP3 192 by default (good for remote streaming)
default_transcode_format: mp3
default_transcode_bitrate: 192

# Hard cap — transcodes any stream that would exceed this
max_streaming_bitrate: 320

# Last.fm API key — enables artist bios + photos in the web UI
# Free key at https://www.last.fm/api/account/create
lastfm_api_key: your_key_here
```

### Run

```bash
python -m backend.main
# or
uvicorn backend.main:app --host 0.0.0.0 --port 4040
```

On first start the database is created, migrations are applied, and the
admin user is seeded. The server listens on `http://0.0.0.0:4040`.

After restarting following config changes, run a scan from the web UI
(Settings → Start scan) or via the API:

```bash
curl -X POST http://localhost:4040/api/scan \
     -H "Authorization: Bearer $JWT"
```

### Frontend (development)

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173, proxies /rest and /api to :4040
```

Production build:

```bash
npm run build  # output in frontend/dist/
```

Serve `frontend/dist/` as static files, with `/rest/*` and `/api/*`
proxied to the backend.

---

## Connecting a Subsonic client

| Setting | Value |
|---|---|
| Server | `http://your-host:4040` |
| Username | Your `admin_username` |
| Password | Your `admin_password` |

Prefer **token + salt** authentication over plaintext if your client
offers the choice. Muse supports both.

---

## Maintenance

A lightweight GC pass runs automatically at the end of every scan. For
manual cleanup and database compaction, two admin endpoints are available
from **Settings → Workshop** or via API:

```bash
# Routine GC — removes orphan rows and artwork files
curl -X POST http://localhost:4040/api/maintenance/gc \
     -H "Authorization: Bearer $JWT"

# GC + VACUUM — additionally rewrites the .db file compactly
# Takes a few seconds; exclusively locks the database
curl -X POST http://localhost:4040/api/maintenance/vacuum \
     -H "Authorization: Bearer $JWT"
```

---

## Security notes

1. **Two separate authentication systems run side by side:**
   - The web UI uses **JWT Bearer tokens** (`/api/*`). You log in once, get a token, and every subsequent request sends that token in an `Authorization` header.
   - Subsonic clients use **username + password** or **username + token + salt** (`/rest/*`). Every request is authenticated — there are no sessions.

2. **Subsonic authenticates every request with the user's password** —
   either as plaintext `p=` or as an MD5 token+salt pair. Run behind HTTPS
   to prevent credentials being intercepted on the network.

3. **Admin actions are protected.** Only admin accounts can trigger scans,
   manage music folders, create/update/delete other users, or view all user accounts.
   Regular users are limited to browsing, streaming, and changing their own password.

4. **Passwords are stored with bcrypt** (cost 12). Bcrypt is deliberately slow to
   compute, making brute-force attacks expensive. Plaintext passwords are never
   written to disk — only the hash is stored.

5. Change the default `admin` / `admin` credentials before exposing to
   a network.

6. Set `jwt_secret` to a long random string. An empty or guessable secret
   allows anyone to forge session tokens.

7. The web UI stores your username and password in `localStorage` so
   Subsonic calls can authenticate without re-prompting. Sign out from the
   sidebar to wipe the credentials.

---

## Possible future work

- **Playlists** — full CRUD, shareable, Subsonic-synced across clients
- **Starred / favourites** — per-user across the full Subsonic hierarchy
- **Play counts and scrobbling** — Last.fm integration, internal play history
- **Now-playing roster** — see what's streaming across all sessions
- **FTS5 full-text search** — fast fuzzy search at 500 k+ tracks without
  table-scan LIKE queries
- **On-the-fly cover art resizing** — serve thumbnails at the requested
  `size` instead of always returning full resolution
- **`getArtistInfo2`** — expose Last.fm artist data via the Subsonic
  protocol (currently web-UI only)
- **Cross-device play queue** — `getPlayQueue` / `savePlayQueue`
- **Audiobook / podcast bookmarks**
- **MusicBrainz metadata enrichment** — MBID lookup for canonical tags and
  richer artist data

---

## Architecture

```
                ┌──────────────────────────────────────────┐
                │           FastAPI application            │
                │                                          │
   browsers ───▶│  /api/*    (web UI, JWT-bearer auth)     │
   clients  ───▶│  /rest/*   (Subsonic, password auth)     │
                └──────────────────────────────────────────┘
                                   │
                    Auth & permissions (deps.py)
                    ├── jwt_admin  → admin-only /api/* endpoints
                    ├── jwt_user   → any-user /api/* endpoints
                    └── subsonic_context → all /rest/* endpoints
                                   │
                                   ▼
                ┌──────────────────────────────────────────┐
                │             Core services                │
                │   library  ·  search  ·  auth  ·  lastfm │
                └──────────────────────────────────────────┘
                                   │
         ┌──────────────┬──────────┴──────────┬────────────────┐
         ▼              ▼                     ▼                ▼
     Scanner        Streaming            SQLite (WAL)    Artwork cache
   walk/parse    range + ffmpeg       indexed schema   sha1-named files
```

Key design decisions:

- **SQLite over Postgres.** WAL mode handles concurrent readers during scan
  writes. `cache_size = -50000` keeps ~200 MB of pages hot. No daemon, no
  separate process, trivial backups.
- **Hand-written SQL over ORM.** Every query is in `db/queries.py`, visible
  and optimisable. No N+1 surprises, no migration-framework overhead.
- **Prefixed Subsonic IDs.** `ar-N` / `al-N` / `tr-N` — opaque to clients,
  typed for the server. Eliminates a whole class of wrong-type ID bugs.
- **Streaming via subprocess pipe.** Transcoded audio flows directly from
  ffmpeg's stdout in 64 KB chunks. No full file in memory; a disconnection
  mid-stream terminates the encoder immediately.
- **Hash-named artwork cache.** Art is stored as `sha1(bytes)[:16].ext`.
  Identical artwork shared across many albums is stored once.
- **Both `/rest/X` and `/rest/X.view`** are registered; legacy clients
  hard-code one form or the other.
- **OpenSubsonic 1.16.1 compliant.** Every response carries `openSubsonic: true`,
  `type`, and `serverVersion`. Song objects include the extended fields
  (`mediaType`, `genres[]`, `artists[]`, etc.).

---

## Project layout

```
muse-server/
├── Dockerfile           # Multi-stage: Node builds frontend, Python serves everything
├── docker-compose.yml   # Ready-to-run with volume and env-var placeholders
├── backend/
│   ├── main.py              # FastAPI app, lifespan, CORS, routers
│   ├── config/              # Pydantic Settings + YAML loader
│   ├── api/
│   │   ├── subsonic.py      # /rest/* — Subsonic-compatible router
│   │   ├── web.py           # /api/*  — internal web UI router
│   │   ├── responses.py     # Subsonic envelope (json / xml / jsonp)
│   │   └── deps.py          # FastAPI dependencies (JWT + Subsonic auth)
│   ├── core/
│   │   ├── auth.py          # bcrypt, JWT, Subsonic token+salt
│   │   ├── library.py       # ID helpers, Subsonic shape builders
│   │   ├── search.py        # search3 business logic
│   │   └── lastfm.py        # Last.fm artist bio + image fetcher
│   ├── db/
│   │   ├── schema.sql       # Table definitions and indexes
│   │   ├── connection.py    # Thread-local SQLite connections, transaction()
│   │   ├── migrations.py    # Versioned schema migrations (runs on startup)
│   │   ├── queries.py       # All hand-written SQL — every DB call lives here
│   │   └── maintenance.py   # GC, VACUUM, WAL checkpoint
│   ├── scanner/
│   │   ├── walker.py        # os.scandir-based directory walker
│   │   ├── metadata.py      # mutagen → ffprobe → filename pipeline
│   │   ├── artwork.py       # Embedded + folder-art extraction and cache
│   │   └── scanner.py       # Orchestration, progress, thread pool
│   └── streaming/
│       ├── presets.py       # Transcode preset table
│       ├── transcoder.py    # FFmpeg subprocess pipe
│       └── streamer.py      # Range-aware HTTP streamer
├── tests/
│   ├── conftest.py              # Shared pytest fixtures (isolated test DB)
│   ├── test_permissions.py      # Auth / 401 / 403 gate tests
│   ├── test_users.py            # Web UI user CRUD (/api/users/*)
│   └── test_subsonic_users.py   # Subsonic user management (/rest/*)
├── frontend/
│   ├── src/
│   │   ├── main.ts          # Hash router, shell, player mount
│   │   ├── api.ts           # Subsonic + JWT API clients
│   │   ├── auth.ts          # Login state (localStorage)
│   │   ├── player.ts        # HTML5 audio, queue, dock rendering
│   │   ├── style.css        # Editorial-zine aesthetic
│   │   └── views/
│   │       ├── library.ts   # A–Z index (list + grid mode)
│   │       ├── albums.ts    # Album grid with pagination
│   │       ├── album.ts     # Single album tracklist
│   │       ├── artist.ts    # Artist page with bio
│   │       ├── search.ts    # Search results with load-more
│   │       ├── track.ts     # Track detail
│   │       ├── settings.ts  # Workshop / admin panel
│   │       └── _util.ts     # Shared helpers
│   ├── package.json
│   └── vite.config.ts
└── config.example.yaml
```

---

## Running the tests

```bash
# Install dependencies (first time)
.venv/bin/pip install -r backend/requirements.txt pytest httpx

# Run all tests
.venv/bin/pytest tests/ -v

# Run a specific file
.venv/bin/pytest tests/test_subsonic_users.py -v
```

The test suite uses a temporary SQLite database per test — nothing is written to
your real library. Tests cover permissions, user CRUD, and Subsonic protocol compliance.

---

## License

Your project, your license. AGPL-3.0 if you intend to distribute; MIT for
personal use.
