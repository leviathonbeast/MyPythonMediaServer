-- ============================================================================
-- Muse — SQLite schema
-- ============================================================================
-- Design goals:
--   * Normalized: artists / albums / tracks are separate tables, joined by id.
--   * Fast browse: every column hit by an indexed query has an explicit index.
--   * Incremental scan: tracks carry mtime + size + content_hash so we can
--     decide quickly whether a file changed without re-parsing tags.
--   * Subsonic-friendly ids: we generate stable string ids (ar-{rowid},
--     al-{rowid}, tr-{rowid}) at the API layer — internally we use INTEGER
--     PKs because they're faster and SQLite indexes them implicitly.
--
-- Why SQLite (not Postgres):
--   100k–500k tracks fits comfortably in SQLite. WAL mode gives us concurrent
--   reads during a scan write. Single-file backups. No daemon. Migration to
--   Postgres later is straightforward — the schema is plain SQL.
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------------------
-- schema_version
-- One-row table that tracks the current migration. db/migrations.py reads
-- this on startup and applies any pending migrations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- ---------------------------------------------------------------------------
-- music_folders
-- Subsonic's top-level concept: each music folder is a root directory.
-- Mapping rows to settings.music_folders happens at startup; ids are stable
-- across runs as long as the path doesn't change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS music_folders (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- artists
-- We deduplicate by lower-cased name. "The Beatles" and "the beatles" are
-- the same artist. Display name keeps original casing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    name_lower  TEXT NOT NULL UNIQUE,        -- dedup key
    sort_name   TEXT,                         -- "Beatles, The" for sorting
    album_count INTEGER NOT NULL DEFAULT 0,   -- denormalized for fast browse
    image_id    TEXT                          -- artwork cache hash (Deezer-sourced)
);
CREATE INDEX IF NOT EXISTS idx_artists_sort      ON artists(sort_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_artists_name_low  ON artists(name_lower);

-- ---------------------------------------------------------------------------
-- albums
-- An album is keyed by (artist_id, name_lower). Compilations get artist_id
-- pointing at the "Various Artists" sentinel artist (created on demand).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS albums (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    name_lower   TEXT NOT NULL,
    sort_name    TEXT,                         -- name with leading punct/articles stripped
    year         INTEGER,
    genre        TEXT,
    track_count  INTEGER NOT NULL DEFAULT 0,
    duration     INTEGER NOT NULL DEFAULT 0,  -- total seconds
    cover_art_id TEXT,                         -- file hash of the artwork blob
    created_at   INTEGER NOT NULL,             -- unix epoch — Subsonic "created"
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
    UNIQUE (artist_id, name_lower)
);
-- Browse-by-artist needs this. Browse-by-newest uses created_at.
CREATE INDEX IF NOT EXISTS idx_albums_artist  ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_year    ON albums(year);
CREATE INDEX IF NOT EXISTS idx_albums_genre   ON albums(genre);
CREATE INDEX IF NOT EXISTS idx_albums_created ON albums(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_albums_name    ON albums(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_albums_sort    ON albums(sort_name COLLATE NOCASE);

-- ---------------------------------------------------------------------------
-- tracks
-- One row per audio file on disk.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id        INTEGER,                    -- nullable: stray files allowed
    artist_id       INTEGER,                    -- track artist (may differ from album artist)
    music_folder_id INTEGER NOT NULL,
    path            TEXT NOT NULL UNIQUE,       -- absolute path
    title           TEXT NOT NULL,
    track_number    INTEGER,
    disc_number     INTEGER,
    duration        INTEGER,                    -- seconds
    bitrate         INTEGER,                    -- kbps
    size            INTEGER NOT NULL,           -- bytes
    suffix          TEXT NOT NULL,              -- "mp3", "flac", ...
    content_type    TEXT NOT NULL,              -- "audio/mpeg", ...
    year            INTEGER,
    genre           TEXT,
    -- Scan bookkeeping
    mtime           INTEGER NOT NULL,           -- file mtime (epoch seconds)
    content_hash    TEXT,                       -- partial hash; see scanner/walker.py
    last_scanned    INTEGER NOT NULL,           -- epoch seconds
    FOREIGN KEY (album_id)        REFERENCES albums(id)        ON DELETE SET NULL,
    FOREIGN KEY (artist_id)       REFERENCES artists(id)       ON DELETE SET NULL,
    FOREIGN KEY (music_folder_id) REFERENCES music_folders(id) ON DELETE CASCADE
);
-- Indexes chosen specifically for the queries we run in db/queries.py:
CREATE INDEX IF NOT EXISTS idx_tracks_album    ON tracks(album_id, disc_number, track_number);
CREATE INDEX IF NOT EXISTS idx_tracks_artist   ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_folder   ON tracks(music_folder_id);
CREATE INDEX IF NOT EXISTS idx_tracks_path     ON tracks(path);
CREATE INDEX IF NOT EXISTS idx_tracks_title    ON tracks(title COLLATE NOCASE);

-- ---------------------------------------------------------------------------
-- users
-- Even single-user installs get a user row. Multi-user comes free later.
-- password_hash is bcrypt; we never store plaintext.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- starred / favorites (Phase 2 placeholders).
-- One row per (user, target). target_type ∈ ('track','album','artist').
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS starred (
    user_id     INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    starred_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, target_type, target_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- playlists 
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    comment    TEXT,
    is_public  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    track_id    INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, position),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id)    REFERENCES tracks(id)    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pltracks_track ON playlist_tracks(track_id);

-- ---------------------------------------------------------------------------
-- play_counts / scrobble.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS play_counts (
    user_id    INTEGER NOT NULL,
    track_id   INTEGER NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);
