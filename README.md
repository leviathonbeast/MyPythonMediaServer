# Muse

A self-hosted, Subsonic-compatible music server built for personal archives
in the 100 k – 500 k track range.

| Layer | Stack |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLite (WAL mode), FFmpeg |
| Frontend | TypeScript + Vite — no UI framework, ~54 KB JS / ~21 KB CSS |
| Protocol | Subsonic API 1.16.1 |

Compatible with **Symfonium**, **play:Sub**, **DSub**, **Substreamer**,
**Sonixd**, and any other Subsonic client.

---

## What's implemented

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
- Transcode presets: MP3 320 / 192 / 128, Opus 192 / 128, OGG 192 / 128
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
- **Settings / Workshop** — trigger scans, manage music folders, configure
  transcoding quality, run GC / vacuum
- **Persistent player dock** — queue, scrubber, volume, skip/prev,
  stream-format badge showing the actual delivered format and bitrate

### Subsonic endpoints

#### Fully implemented

| Endpoint | Notes |
|---|---|
| `ping` | Auth probe |
| `getLicense` | Always returns valid (FOSS) |
| `getMusicFolders` | All configured roots |
| `getIndexes` | A–Z artist index; includes `coverArt` per artist |
| `getMusicDirectory` | Artist and album directory traversal |
| `getAlbum` | Album with full track list; returns `name` + `artistId` (AlbumID3) |
| `getAlbumList` | All sort modes including random; `byYear` filters by `fromYear`/`toYear`; `byGenre` filters by `genre` |
| `getAlbumList2` | ID3 variant; same data shape as `getAlbumList` |
| `getSong` | Single track by id |
| `search3` | Artists / albums / tracks with server-side pagination (all six offset params) |
| `stream` | Raw + transcoded, Range-aware; `timeOffset` not supported |
| `download` | Raw only (no transcode) |
| `getCoverArt` | Serves from artwork cache; `size` accepted but not resized (full resolution always returned) |
| `getUser` | Returns roles for requesting user (or any user if admin); `folder[]` not included |
| `createUser` | Admin-only; `email` accepted but not stored |

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
| `getUsers` / `updateUser` / `deleteUser` | User management |
| `changePassword` | Account self-service |
| `getPlayQueue` / `savePlayQueue` | Cross-device queue sync |
| `createBookmark` / `getBookmarks` / `deleteBookmark` | Audiobook / podcast position |
| `getPodcasts` / `getNewestPodcasts` | Podcast feeds |
| `getInternetRadioStations` | Internet radio |
| `getScanStatus` / `startScan` | Subsonic-native scan control |

---

## Installation

### Prerequisites

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

1. **Subsonic authenticates every request with the user's password** —
   either as plaintext `p=` or as an MD5 token+salt pair. Muse caches the
   plaintext password **in memory only** after first login; nothing is
   written to disk in cleartext.
2. **Run behind HTTPS in production.** Without TLS, the password can be
   read from any Subsonic request on the same network.
3. The web UI stores your username and password in `localStorage` so
   Subsonic calls can authenticate without re-prompting. Sign out from the
   sidebar to wipe the credentials.
4. Change the default `admin` / `admin` credentials before exposing to
   a network.
5. Set `jwt_secret` to a long random string. An empty or guessable secret
   allows anyone to forge session tokens.

---

## Possible future work

- **Playlists** — full CRUD, shareable, Subsonic-synced across clients
- **Starred / favourites** — per-user across the full Subsonic hierarchy
- **Play counts and scrobbling** — Last.fm integration, internal play history
- **Now-playing roster** — see what's streaming across all sessions
- **User management** — add/remove users, role assignment from the web UI
- **FTS5 full-text search** — fast fuzzy search at 500 k+ tracks without
  table-scan LIKE queries
- **On-the-fly cover art resizing** — serve thumbnails at the requested
  `size` instead of always returning full resolution
- **`getArtistInfo2`** — expose Last.fm artist data via the Subsonic
  protocol (currently web-UI only)
- **Cross-device play queue** — `getPlayQueue` / `savePlayQueue`
- **Audiobook / podcast bookmarks**
- **Docker image** — single-container deploy with ffmpeg bundled
- **MusicBrainz metadata enrichment** — MBID lookup for canonical tags and
  richer artist data

---

## Architecture

```
                ┌──────────────────────────────────────────┐
                │           FastAPI application            │
                │                                          │
   browsers ───▶│  /api/*    (web UI, JWT-bearer)          │
   clients  ───▶│  /rest/*   (Subsonic, password auth)     │
                └──────────────────────────────────────────┘
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

---

## Project layout

```
muse-server/
├── backend/
│   ├── main.py              # FastAPI app, lifespan, CORS, routers
│   ├── config/              # Pydantic Settings + YAML loader
│   ├── api/
│   │   ├── subsonic.py      # /rest/* — Subsonic-compatible router
│   │   ├── web.py           # /api/*  — internal web UI router
│   │   ├── responses.py     # Subsonic envelope (json / xml / jsonp)
│   │   └── deps.py          # FastAPI dependencies (auth context)
│   ├── core/
│   │   ├── auth.py          # bcrypt, JWT, Subsonic token+salt
│   │   ├── library.py       # ID helpers, Subsonic shape builders
│   │   ├── search.py        # search3 business logic
│   │   └── lastfm.py        # Last.fm artist bio + image fetcher
│   ├── db/
│   │   ├── schema.sql       # Table definitions and indexes
│   │   ├── connection.py    # Thread-local SQLite connections
│   │   ├── migrations.py    # Versioned schema migrations
│   │   ├── queries.py       # All hand-written SQL
│   │   └── maintenance.py   # GC, VACUUM, WAL checkpoint
│   ├── scanner/
│   │   ├── walker.py        # os.scandir-based directory walker
│   │   ├── metadata.py      # mutagen → ffprobe → filename pipeline
│   │   ├── artwork.py       # Embedded + folder-art extraction
│   │   └── scanner.py       # Orchestration, progress, thread pool
│   └── streaming/
│       ├── presets.py       # Transcode preset table
│       ├── transcoder.py    # FFmpeg subprocess pipe
│       └── streamer.py      # Range-aware HTTP streamer
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

## License

Your project, your license. AGPL-3.0 if you intend to distribute; MIT for
personal use.
