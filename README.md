# Muse

A self-hosted, [Subsonic-compatible](https://opensubsonic.netlify.app/)
music server for personal libraries up to ~500 k tracks.

- **Backend** — Python 3.11+, FastAPI, SQLite (WAL) or Postgres 14+, FFmpeg
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
| `MUSE_DATABASE_URL` | Optional — `sqlite:///...` or `postgresql://user:pass@host/db`. Default is SQLite. |
| `MUSE_LASTFM_API_KEY` | Optional — enables artist bios & photos |
| `MUSE_MAX_STREAMING_BITRATE` | Optional — server-wide kbps cap |
| `MUSE_AUTH_RATE_LIMITS` | Login rate limit (default: `5/minute`) |

To bring up the optional bundled Postgres alongside Muse:

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  docker compose --profile postgres up -d
```

…then uncomment the `MUSE_DATABASE_URL` line in `docker-compose.yml`
so Muse connects to it. See [Database backend](#database-backend) for
when this is worth doing.

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
(e.g. `MUSE_DATABASE_URL=postgresql://user:pass@localhost/muse`).

---

## Features

**Library** — recursive scan over local and network mounts; incremental
rescans (mtime+size diff); full-text search across title, artist,
album and genre (FTS5 on SQLite, `tsvector + GIN` on Postgres);
mutagen → ffprobe → filename metadata pipeline; MusicBrainz IDs
extracted from tags and exposed through every relevant endpoint
(getAlbumInfo, getArtistInfo, etc.); embedded + folder-art extraction
with content-hash dedup; automatic GC after each scan.

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
- Albums: `getAlbum`, `getAlbumList`, `getAlbumList2`, `getSong`, `getAlbumInfo`, `getAlbumInfo2`
- Artists: `getArtistInfo`, `getArtistInfo2` (Last.fm bio + Deezer images)
- Playback: `stream`, `download`, `getCoverArt` (with on-the-fly resize), `scrobble`
- Search: `search3` (FTS5 on SQLite, `tsvector` on Postgres)
- Starring: `star`, `unstar`, `getStarred`, `getStarred2`
- Playlists: `getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`
- Play queue: `getPlayQueue`, `savePlayQueue` (cross-device sync)
- Users: `getUser`, `getUsers`, `createUser`, `updateUser`, `deleteUser`, `changePassword`
- Scan: `getScanStatus`, `startScan`
- Random/top: `getRandomSongs`, `getTopSongs`
- System: `ping`, `getLicense`, `getOpenSubsonicExtensions`

**Stubbed** (returns valid empty responses so clients don't error):
`getNowPlaying`

**Not yet implemented:**
genres endpoint, similar-songs, podcasts, internet radio, bookmarks

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
                              ┌─────────┬───────────┴─────────┬──────────────┐
                              ▼         ▼                     ▼              ▼
                          Scanner   Streaming      SQLite (WAL) /        Artwork cache
                                                   Postgres (citext +
                                                   tsvector)
```

Notable design decisions:

- **SQLite by default, Postgres optional** — single-user installs get
  zero-daemon, copy-one-file backups; multi-user or hosted deployments
  flip to Postgres via a URL change (see below).
- **Hand-written SQL** in `db/queries.py` — every query is visible and
  optimisable; no ORM surprises. The `:name` named-binding style ports
  cleanly between dialects.
- **Full-text search** — virtual FTS5 table + triggers on SQLite;
  weighted `tsvector` column + GIN index on Postgres. Same query
  interface (`search3`), dialect-aware behind it.
- **Prefixed Subsonic IDs** (`ar-N`/`al-N`/`tr-N`) — opaque to clients,
  type-safe on the server.
- **Streaming via subprocess pipe** — transcoded audio flows from FFmpeg
  stdout in 64 KB chunks; nothing buffers the whole file.
- **Hash-named artwork cache** — `sha1(bytes)[:16].ext` deduplicates art
  shared across albums.

---

## Database backend

Muse runs on SQLite by default and supports Postgres 14+ as an
alternative. The backend is selected by URL scheme via
`MUSE_DATABASE_URL` (env) or `database_url` (config.yaml):

```yaml
# SQLite — the default, fine for almost everyone
database_url: sqlite:///./data/library.db

# Postgres — server-mode database
database_url: postgresql://muse:password@host:5432/muse
```

### When to pick which

**Stay on SQLite** if:
- It's a single-user install (one library, one or two listeners).
- You like that backups are `cp library.db backup.db`.
- You don't want a separate database process to babysit.

For libraries up to ~500k tracks SQLite is genuinely fast — often
faster than Postgres on the same hardware for this workload, because
there's no IPC or network hop. WAL mode handles the "scan while
browsing" concurrent-read case cleanly.

**Switch to Postgres** if:
- You're running multiple Muse instances against one shared library.
- Your library lives on a hosted database (e.g. Supabase, RDS) rather
  than on the same box as the app.
- You want online VACUUM (no exclusive lock during cleanup) on a very
  large library.
- You prefer the operational story you already know (`pg_dump`, point-
  in-time recovery, role-based access).

### Postgres tuning

The bundled `docker compose --profile postgres` brings up a Postgres
18 service tuned for a single-host Muse install (~1 GiB DB RAM
budget). The relevant knobs in [docker-compose.yml](docker-compose.yml):

| Setting | Value | Why |
|---|---|---|
| `shared_buffers` | `256MB` | ~25% of RAM available to Postgres. Browse-page queries hit the same pages over and over. |
| `effective_cache_size` | `768MB` | Tells the planner how much OS page cache it can assume — affects index-vs-seq-scan decisions. |
| `work_mem` | `21845kB` | Per-sort/hash memory. Search and album-list joins benefit; too high risks OOM under concurrency. |
| `maintenance_work_mem` | `64MB` | VACUUM, REINDEX, CREATE INDEX speed. |
| `max_connections` | `40` | Muse uses thread-local connections; FastAPI's default thread pool is ~40. Higher costs RAM per connection. |
| `random_page_cost` | `1.1` | SSD-tuned (the 4.0 default assumes spinning rust). |
| `synchronous_commit` | `off` | Acceptable for a music server — a power-cut might lose the last sub-second of writes, which means at worst one re-scrobble. |
| `wal_compression` | `on` | Roughly halves WAL volume; the CPU cost is negligible. |

For larger installs (multiple libraries, busy multi-user), scale
proportionally: bump `shared_buffers` to ~25% of available RAM,
`effective_cache_size` to ~75%, and raise `max_connections` only as
far as `max_connections × work_mem ≈ available RAM / 4`.

The `citext` extension is created on first run, so the configured
database role needs `CREATE` permission on the database. A throwaway
role with just `CONNECT` + table-level rights won't work for the
initial migration.

### Backup notes

- **SQLite**: `cp /data/library.db backup.db` while Muse is idle, or
  `sqlite3 library.db ".backup backup.db"` while it's running.
- **Postgres**: `pg_dump -Fc -U muse muse > backup.dump`. Restore with
  `pg_restore -d muse backup.dump` into a fresh empty DB. The artwork
  cache (`/data/artwork`) is separate from both — back it up alongside.

### Migrating SQLite → Postgres

Not currently supported. The schemas are intentionally separate
(different ID strategies, FTS5 vs tsvector), so a clean migration would
need a data-only dump → adjust IDs → reload pipeline that doesn't exist
yet. For now: fresh install on Postgres, point Muse at it, let the
scanner re-populate from your music folders. Playlists and starred
items are not preserved.

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
touched. 225 tests covering permissions, user CRUD, playlist CRUD,
FTS5, queries, starring, and Subsonic protocol compliance.

**Postgres pass.** To also run the suite against Postgres (catches
dialect divergence the SQLite pass can't), create a dedicated test
database and set `PYTEST_POSTGRES_URL`:

```bash
sudo -u postgres psql -c "CREATE DATABASE muse_test OWNER muse;"
PYTEST_POSTGRES_URL=postgresql://muse:password@localhost/muse_test \
    pytest tests/ -v
```

**Warning:** every test wipes the target schema (`DROP SCHEMA public
CASCADE`). Point at a throwaway database, never at production.

The FTS5 test is skipped automatically on the Postgres pass (no
virtual table on that backend; the equivalent is the tsvector trigger
exercised implicitly by search3 tests).

The Postgres pass takes a few seconds longer than SQLite because of
per-test schema reset. `pytest -p no:xdist` is recommended — the
shared test DB doesn't tolerate parallel workers.

### Maintenance

GC runs automatically after every scan. To trigger manually:

```bash
# Tidy up — remove orphan rows and artwork files
curl -X POST http://localhost:4040/api/maintenance/gc \
     -H "Authorization: Bearer $JWT"

# Tidy + VACUUM — additionally rewrites the database compactly
curl -X POST http://localhost:4040/api/maintenance/vacuum \
     -H "Authorization: Bearer $JWT"
```

On SQLite, VACUUM takes an exclusive lock for the duration (seconds to
a minute on large libraries) and rewrites the `.db` file. On Postgres,
VACUUM is online — readers and writers proceed normally — so it's safe
to run any time.

Both are also exposed in the Settings page (admin only).

---

## License

Your project, your license. AGPL-3.0 if you intend to distribute;
MIT for personal use.