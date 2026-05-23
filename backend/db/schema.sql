-- ============================================================================
-- Muse — SQLite schema
-- ============================================================================
--
-- This is the canonical SQLite schema. A parallel `schema.postgres.sql`
-- exists for the Postgres backend; the loader in migrations.py picks one
-- at install time based on the configured dialect.
--
-- Squashed from migrations 001-016 (plus the musicbrainz_id additions) so
-- a fresh install creates the full current schema in one pass. The
-- migrations module retains the version-tracking infrastructure for any
-- *future* schema changes — those should be added as numbered migrations
-- on top of this baseline.
--
-- Design goals (unchanged from the original):
--   * Normalized: artists / albums / tracks are separate tables, joined by id.
--   * Fast browse: every column hit by an indexed query has an explicit index.
--   * Incremental scan: tracks carry mtime + size so we can decide quickly
--     whether a file changed without re-parsing tags.
--   * Subsonic-friendly ids: we generate stable string ids (ar-{rowid},
--     al-{rowid}, tr-{rowid}) at the API layer — internally we use INTEGER
--     PKs because they're faster and SQLite indexes them implicitly.
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------------------
-- schema_version: one-row table tracking the most-recently-applied migration.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- ---------------------------------------------------------------------------
-- music_folders: each row is a root directory the scanner walks.
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_lower      TEXT NOT NULL UNIQUE,        -- dedup key
    sort_name       TEXT,                         -- "Beatles, The" for sorting
    album_count     INTEGER NOT NULL DEFAULT 0,   -- denormalized for fast browse
    image_id        TEXT,                         -- artwork cache hash (Deezer-sourced)
    musicbrainz_id  TEXT                          -- MBID, optional, from tags
);
CREATE INDEX IF NOT EXISTS idx_artists_sort      ON artists(sort_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_artists_name_low  ON artists(name_lower);

-- ---------------------------------------------------------------------------
-- albums
-- An album is keyed by (artist_id, name_lower). Compilations get artist_id
-- pointing at the "Various Artists" sentinel artist (created on demand).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS albums (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id                       INTEGER NOT NULL,
    name                            TEXT NOT NULL,
    name_lower                      TEXT NOT NULL,
    sort_name                       TEXT,                -- name with leading punct/articles stripped
    year                            INTEGER,
    genre                           TEXT,
    release_type                    TEXT,                -- MusicBrainz primary type: album/ep/single/...
    track_count                     INTEGER NOT NULL DEFAULT 0,
    duration                        INTEGER NOT NULL DEFAULT 0,  -- total seconds
    cover_art_id                    TEXT,                -- file hash of the artwork blob
    created_at                      INTEGER NOT NULL,    -- unix epoch — Subsonic "created"
    musicbrainz_id                  TEXT,                -- release MBID
    musicbrainz_releasegroup_id     TEXT,                -- release-group MBID (cross-edition)
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
    UNIQUE (artist_id, name_lower)
);
CREATE INDEX IF NOT EXISTS idx_albums_artist        ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_year          ON albums(year);
CREATE INDEX IF NOT EXISTS idx_albums_genre         ON albums(genre);
CREATE INDEX IF NOT EXISTS idx_albums_created       ON albums(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_albums_name          ON albums(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_albums_sort          ON albums(sort_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_albums_release_type  ON albums(release_type);
-- Composite that lets the artist-index cover-art lookup hit a single index
-- without touching the table rows.
CREATE INDEX IF NOT EXISTS idx_albums_artist_year   ON albums(artist_id, year DESC, created_at DESC);

-- ---------------------------------------------------------------------------
-- tracks
-- One row per audio file on disk.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id        INTEGER,                    -- nullable: stray files allowed
    artist_id       INTEGER,                    -- track artist (may differ from album artist)
    music_folder_id INTEGER NOT NULL,
    path            TEXT NOT NULL UNIQUE,       -- absolute path (UNIQUE provides the index)
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
    content_hash    TEXT,                       -- partial hash; populated by future enrichment
    last_scanned    INTEGER NOT NULL,           -- epoch seconds
    musicbrainz_id  TEXT,                       -- recording MBID
    FOREIGN KEY (album_id)        REFERENCES albums(id)        ON DELETE SET NULL,
    FOREIGN KEY (artist_id)       REFERENCES artists(id)       ON DELETE SET NULL,
    FOREIGN KEY (music_folder_id) REFERENCES music_folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tracks_album   ON tracks(album_id, disc_number, track_number);
CREATE INDEX IF NOT EXISTS idx_tracks_artist  ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_folder  ON tracks(music_folder_id);
CREATE INDEX IF NOT EXISTS idx_tracks_title   ON tracks(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tracks_year    ON tracks(year);
-- Partial index — most rows have a non-NULL genre, so we skip the NULLs
-- and the index is roughly half the size.
CREATE INDEX IF NOT EXISTS idx_tracks_genre
    ON tracks(genre COLLATE NOCASE) WHERE genre IS NOT NULL;
-- Recording MBID lookup — used when importing external playlists
-- (ListenBrainz) where each track is matched by its MusicBrainz id first.
-- Partial for the same reason as the genre index: skip the NULLs.
CREATE INDEX IF NOT EXISTS idx_tracks_musicbrainz_id
    ON tracks(musicbrainz_id) WHERE musicbrainz_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- users
-- Includes all the Subsonic / OpenSubsonic role + preference columns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    username                 TEXT NOT NULL UNIQUE,
    password_hash            TEXT NOT NULL,
    is_admin                 INTEGER NOT NULL DEFAULT 0,
    created_at               INTEGER NOT NULL,
    password_changed_at      INTEGER,
    disabled                 INTEGER NOT NULL DEFAULT 0,
    -- Encrypted plaintext for Subsonic token+salt auth across restarts.
    encrypted_password       TEXT,
    -- Profile fields
    email                    TEXT,
    scrobbling_enabled       INTEGER NOT NULL DEFAULT 0,
    max_bit_rate             INTEGER NOT NULL DEFAULT 0,
    -- Subsonic 1.16.1 roles — defaults follow the spec: stream/settings/
    -- playlist are true for normal users, everything else admin-gated.
    settings_role            INTEGER NOT NULL DEFAULT 1,
    stream_role              INTEGER NOT NULL DEFAULT 1,
    download_role            INTEGER NOT NULL DEFAULT 0,
    upload_role              INTEGER NOT NULL DEFAULT 0,
    playlist_role            INTEGER NOT NULL DEFAULT 1,
    cover_art_role           INTEGER NOT NULL DEFAULT 0,
    comment_role             INTEGER NOT NULL DEFAULT 0,
    podcast_role             INTEGER NOT NULL DEFAULT 0,
    jukebox_role             INTEGER NOT NULL DEFAULT 0,
    share_role               INTEGER NOT NULL DEFAULT 0,
    video_conversion_role    INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- starred / favorites.
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
-- Composite supports "get this user's stars newest-first" without a temp sort.
CREATE INDEX IF NOT EXISTS idx_starred_user_at ON starred(user_id, starred_at DESC);

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
    user_id     INTEGER NOT NULL,
    track_id    INTEGER NOT NULL,
    play_count  INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);
-- frequent/recent album sort joins play_counts on track_id; without this
-- SQLite builds an automatic temp index every call.
CREATE INDEX IF NOT EXISTS idx_play_counts_track ON play_counts(track_id);

-- ---------------------------------------------------------------------------
-- play_queues / play_queue_entries (Subsonic savePlayQueue / getPlayQueue).
-- One header row per user; child table holds the ordered list of track ids.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS play_queues (
    user_id      INTEGER PRIMARY KEY,
    current_id   INTEGER,
    position_ms  INTEGER NOT NULL DEFAULT 0,
    changed_at   INTEGER NOT NULL,
    changed_by   TEXT NOT NULL,
    FOREIGN KEY (user_id)    REFERENCES users(id)  ON DELETE CASCADE,
    FOREIGN KEY (current_id) REFERENCES tracks(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS play_queue_entries (
    user_id  INTEGER NOT NULL,
    position INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, position),
    FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Full-text search: contentless FTS5 with contentless_delete=1.
--
-- We index (title, genre, artist_name, album_name) so search3 can MATCH
-- across the natural columns at once. The contentless option keeps the
-- on-disk footprint small (terms only, no source-text storage) and
-- contentless_delete=1 — added in SQLite 3.43 — re-enables plain DELETE
-- on the FTS5 table, which the maintenance triggers below rely on.
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS virt_fts5 USING fts5(
    title,
    genre,
    artist_name,
    album_name,
    content='',
    contentless_delete=1
);

CREATE TRIGGER IF NOT EXISTS fts5_track_insert AFTER INSERT ON tracks
BEGIN
    INSERT INTO virt_fts5(rowid, title, genre, artist_name, album_name)
    VALUES (
        NEW.id,
        NEW.title,
        NEW.genre,
        (SELECT name FROM artists WHERE id = NEW.artist_id),
        (SELECT name FROM albums  WHERE id = NEW.album_id)
    );
END;

CREATE TRIGGER IF NOT EXISTS fts5_track_delete AFTER DELETE ON tracks
BEGIN
    DELETE FROM virt_fts5 WHERE rowid = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS fts5_track_update AFTER UPDATE ON tracks
BEGIN
    DELETE FROM virt_fts5 WHERE rowid = OLD.id;
    INSERT INTO virt_fts5(rowid, title, genre, artist_name, album_name)
    VALUES (
        NEW.id,
        NEW.title,
        NEW.genre,
        (SELECT name FROM artists WHERE id = NEW.artist_id),
        (SELECT name FROM albums  WHERE id = NEW.album_id)
    );
END;
