# Muse

A self-hosted, [Subsonic-compatible](https://opensubsonic.netlify.app/)
music server for personal libraries up to ~500 k tracks.

- **Backend** — Python 3.11+, FastAPI, SQLite (WAL), FFmpeg
- **Frontend** — TypeScript + Vite, no framework (~54 KB JS / ~21 KB CSS)
- **Protocol** — OpenSubsonic 1.16.1 — works with Feishin, Symfonium, play:Sub,
  DSub, Substreamer, Sonixd, and other Subsonic clients

---

## Quick start (Docker)

```bash
git clone <this-repo> muse && cd muse
$EDITOR docker-compose.yml          # point the volume at your music
docker compose up -d
```

Open `http://localhost:4040`. Default login: `admin` / `admin` —
**change immediately**.

Key env vars in `docker-compose.yml`:

| Variable | Purpose |
|---|---|
| `MUSE_JWT_SECRET` | **Must** be a long random string in production |
| `MUSE_ADMIN_PASSWORD` | Initial admin password (first-run only) |
| `MUSE_MUSIC_FOLDERS` | JSON array if you have more than one root |
| `MUSE_LASTFM_API_KEY` | Optional — enables artist bios & photos |
| `MUSE_MAX_STREAMING_BITRATE` | Optional — server-wide kbps cap |
| `MUSE_AUTH_RATE_LIMITS` | Login rate limit (default: `5/minute`) |

---

## Manual install

Requires Python 3.11+, FFmpeg (`ffmpeg`, `ffprobe` on `PATH`), and
Node.js 18+ if you want to develop the frontend.

```bash
git clone <this-repo> muse && cd muse
cp config.example.yaml config.yaml
$EDITOR config.yaml                 # set music_folders, admin_password, jwt_secret
./run.sh                            # production mode (port 4040)
./run.sh dev                        # backend + Vite dev server, hot reload
```

`run.sh` creates `.venv` on first run and installs dependencies.

Every YAML setting can be overridden with the `MUSE_` env-var prefix
(e.g. `MUSE_DATABASE_PATH=/var/muse/library.db`).

---

## Features

**Library** — recursive scan over local and network mounts; incremental
rescans (mtime+size diff); FTS5 full-text search across title, artist,
album and genre; mutagen → ffprobe → filename metadata pipeline;
embedded + folder-art extraction with content-hash dedup; automatic GC
after each scan.

**Streaming** — HTTP Range on raw, on-the-fly FFmpeg transcoding piped
straight from stdout (MP3 320/192/128, Opus 128/96, OGG 192); per-server
bitrate cap; transcoding kill-switch for LAN-only installs.

**Playlists** — create, update and delete playlists; add and remove
tracks; public and private playlists; cross-client compatible via
Subsonic protocol.

**Play counts** — scrobble tracking per user; play count shown on track
detail pages; `frequent` and `recent` sort modes (in progress).

**Web UI** — A–Z artist library, paginated albums, full-text search,
artist pages with Last.fm bios, persistent player dock with queue and
stream-format badge, play count display, admin panels for
user/folder/scan management.

**Users & permissions** — admin and regular roles; full Subsonic role
flags (stream/download/upload/playlist/etc.); admin-only library and
user management; bcrypt-hashed passwords; login rate limiting.

---

## Subsonic compatibility

Fully OpenSubsonic 1.16.1 compliant — every response carries the
`openSubsonic`, `type`, and `serverVersion` envelope fields, and Song
objects include the extended fields (`mediaType`, `genres[]`,
`artists[]`, `displayArtist`, etc.).

**Implemented:**
- Browsing: `getMusicFolders`, `getIndexes`, `getMusicDirectory`, `getArtists`, `getArtist`
- Albums: `getAlbum`, `getAlbumList`, `getAlbumList2`, `getSong`
- Playback: `stream`, `download`, `getCoverArt`, `scrobble`
- Search: `search3` (FTS5-accelerated)
- Playlists: `getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`
- Users: `getUser`, `getUsers`, `createUser`, `updateUser`, `deleteUser`, `changePassword`
- System: `ping`, `getLicense`, `getOpenSubsonicExtensions`

**Stubbed** (returns valid empty responses so clients don't error):
`getStarred`, `getStarred2`, `star`, `unstar`, `getNowPlaying`,
`getArtistInfo`, `getArtistInfo2`, `getRandomSongs`, `getTopSongs`,
`getScanStatus`, `startScan`

**Not yet implemented:**
genres, similar-songs, podcasts, internet radio, bookmarks,
cross-device play queue, cover art resizing

---

## Connecting a Subsonic client

| Setting | Value |
|---|---|
| Server | `http://your-host:4040` |
| Username / Password | Your Muse credentials |

Prefer **token + salt** auth over plaintext if your client offers it.
Muse supports both.

Tested clients: **Feishin**, **Symfonium**, **play:Sub**, **DSub**,
**Substreamer**, **Sonixd**.

---

## Security

1. Change `admin` / `admin` before exposing the server to a network.
2. Set `jwt_secret` to a long random string — generate with:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
3. Run behind HTTPS. The Subsonic protocol authenticates every request
   with the user's password (plaintext or MD5 token+salt).
4. Passwords are stored with bcrypt (cost 12); plaintext is never written
   to disk.
5. Login endpoint is rate-limited (default 5 attempts/minute, configurable).
6. The web UI stores credentials in `localStorage` so Subsonic calls
   authenticate without re-prompting. Sign out wipes them.

---

## Architecture

```
   browsers ─▶  /api/*    (JWT bearer)      ┐
   clients  ─▶  /rest/*   (Subsonic auth)   ┘─▶  FastAPI
                                                    │
                                              core services
                                                    │
                              ┌─────────┬───────────┴─────┬──────────────┐
                              ▼         ▼                 ▼              ▼
                          Scanner   Streaming      SQLite (WAL)   Artwork cache
```

Notable design decisions:

- **SQLite over Postgres** — WAL handles concurrent reads during scans;
  no daemon, trivial backups.
- **FTS5 full-text search** — virtual table with triggers keeps search
  fast at 500k+ tracks without a separate search service.
- **Hand-written SQL** in `db/queries.py` — every query is visible and
  optimisable; no ORM surprises.
- **Prefixed Subsonic IDs** (`ar-N`/`al-N`/`tr-N`) — opaque to clients,
  type-safe on the server.
- **Streaming via subprocess pipe** — transcoded audio flows from FFmpeg
  stdout in 64 KB chunks; nothing buffers the whole file.
- **Hash-named artwork cache** — `sha1(bytes)[:16].ext` deduplicates art
  shared across albums.

---

## Development

```bash
./run.sh dev                        # backend (uvicorn --reload) + Vite HMR
```

Vite serves the UI on `:5173` and proxies `/api` + `/rest` to the
backend on `:4040`. The dev server binds to the host IP so other
devices on the network can reach it.

### Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

Tests use a per-test temporary SQLite DB — your real library is never
touched. 100+ tests covering permissions, user CRUD, playlist CRUD,
FTS5, and Subsonic protocol compliance.

### Maintenance

GC runs automatically after every scan. To trigger manually:

```bash
# Tidy up — remove orphan rows and artwork files
curl -X POST http://localhost:4040/api/maintenance/gc \
     -H "Authorization: Bearer $JWT"

# Tidy + VACUUM — additionally rewrites the .db file compactly
curl -X POST http://localhost:4040/api/maintenance/vacuum \
     -H "Authorization: Bearer $JWT"
```

Both are also exposed in the Settings page (admin only).

---

## License

Your project, your license. AGPL-3.0 if you intend to distribute;
MIT for personal use.