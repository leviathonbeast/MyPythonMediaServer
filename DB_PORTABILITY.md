# db-portability-prep — branch complete

This file lives on the `db-portability-prep` branch only. The prep work
described in `~/.claude/plans/export-async-function-staritems-snappy-snail.md`
is **complete**. Branch is ready for review and merge into `main`.

## Goal (recap)

Reduce SQLite coupling in the codebase so that a future port to Postgres
(or MariaDB / MySQL) is a long afternoon rather than two weeks. **No
behaviour change.** Every commit on this branch leaves the test suite
at 225 passed / 6 xfailed and serves the app identically.

We are **not** porting to Postgres in this branch. We are making the
codebase neutral enough that the eventual port is a small focused
change rather than a global rewrite.

## Done

### Phase 1 — exception centralisation

| # | Commit subject | What it did |
|---|---|---|
| 1 | `db: add backend/db/errors.py for centralised exception re-exports` | New file aliasing `sqlite3.IntegrityError` / `OperationalError` / `DatabaseError`. Future driver swap edits this one file. |
| 2 | `cli: switch sqlite3.IntegrityError to backend.db.errors` | [backend/cli.py](backend/cli.py) no longer imports `sqlite3`. |
| 3 | `api/web: switch sqlite3.IntegrityError to backend.db.errors` | [backend/api/web.py](backend/api/web.py) no longer imports `sqlite3`. Two except clauses updated. |
| 4 | `api/subsonic/users: switch sqlite3.IntegrityError to backend.db.errors` | [backend/api/subsonic/users.py](backend/api/subsonic/users.py) no longer imports `sqlite3`. |

### Phase 2 — connection-layer separation

| # | Commit subject | What it did |
|---|---|---|
| 5 | `db/connection: extract PRAGMA block into _tune_sqlite() helper` | Seven SQLite-only PRAGMAs collected into a dedicated function in [backend/db/connection.py](backend/db/connection.py). `_new_connection()` is now a thin wrapper that's trivial to swap for a Postgres factory. |

### Phase 3 — parameter-style normalisation (`?` → `:name`)

| # | Commit subject | What it did |
|---|---|---|
| 6 | `queries: convert starred section to named parameter binding` | `star_item`, `unstar_item`, `get_starred_items`. |
| 7 | `queries: convert music_folders section to named parameter binding` | `get_music_folder`, `get_music_folder_by_path`, `add_music_folder`, `delete_music_folder`. |
| 8 | `queries: convert play_counts section to named parameter binding` | `play_count`, `get_playcount_by_user`. |
| 9 | `queries: convert playlists section to named parameter binding` | Eight functions including the dynamic-SET `update_playlist` and four `executemany` paths. |
| 10 | `queries: convert play_queue section to named parameter binding` | `get_play_queue`, `save_play_queue`. |
| 11 | `queries: convert artists section to named parameter binding` | `upsert_artist`, `get_artist`, `list_artist_appearances`, `list_artist_albums`. |
| 12 | `queries: convert albums section to named parameter binding` | Seven functions including the dynamic-WHERE `list_albums` and the `update_*_aggregates` helpers. |
| 13 | `queries: convert tracks section to named parameter binding` | Six functions; `delete_tracks` got the `:id0, :id1, …` IN-list pattern. `upsert_track` was already named — left alone. |
| 14 | `queries: convert search3 to named parameter binding` | The LIKE branches share `:pattern`; the FTS5 branch uses `:query`. |
| 15 | `queries: convert users section to named parameter binding` | Eleven functions including dynamic-SET `update_user`. |

## Verification at branch tip

```bash
$ python -m pytest tests/ --no-header -q
# 225 passed, 6 xfailed — same as main

$ grep -rn "^import sqlite3\|sqlite3\." backend/ --include="*.py" | grep -v "backend/db/"
# (empty — no sqlite3 references outside backend/db/)

$ grep -n "?[,)]\|= ?\| ?$" backend/db/queries.py
# (matches only inside comments — every SQL placeholder is now named)
```

Manual smoke checks:

- Browse `/web/#/library` — index loads, artist cards render.
- Open an artist with appearances (e.g. `/web/#/artist/ar-63730`) — both
  the regular album sections AND the "Appears on" track table populate.
- `/web/#/search?q=…` — uses FTS5 with `:query` binding; results return.
- Trigger `POST /api/scan` — full scanner pipeline writes via the named
  `upsert_artist` / `upsert_album` / `upsert_track` triple. Counts match
  pre-conversion runs.

## What is NOT done (out of scope, by design)

The plan deliberately stopped here. These remain real porting work,
not prep work:

- **FTS5** — `virt_fts5` table + three triggers + `contentless_delete=1`
  in [backend/db/migrations.py](backend/db/migrations.py). SQLite-only.
- **`INSERT OR IGNORE` / `INSERT OR REPLACE`** at three sites
  ([queries.py:71](backend/db/queries.py#L71),
   [queries.py:506](backend/db/queries.py#L506),
   [migrations.py:75](backend/db/migrations.py#L75)).
- **`VACUUM`** and `PRAGMA wal_checkpoint(TRUNCATE)` in
  [backend/db/maintenance.py](backend/db/maintenance.py).
- **`COLLATE NOCASE`** usage throughout `queries.py` — needs a Postgres
  equivalent (case-insensitive collation or `LOWER(col) = LOWER(?)`).
- **Threading model** — per-thread `sqlite3.Connection` cache in
  [backend/db/connection.py](backend/db/connection.py) would become a
  `psycopg_pool.ConnectionPool` for Postgres.
- **Migration dispatch** — each `_migration_NNN()` function would need
  a `dialect` arg, or maintain parallel migration sets per dialect.
- **Config** — add a `database_url` field that picks the driver.

If you ever commit to the port, the bigger plan in
`~/.claude/plans/export-async-function-staritems-snappy-snail.md`
sizes that work at ~1–2 weeks of focused evenings for Postgres, ~3–5
extra evenings on top for MariaDB/MySQL (because of `RETURNING` and
`ON DUPLICATE KEY` differences).

## How to merge

Branch is clean and rebased on `main`. Open a normal PR. This file
should be deleted in the merge commit since it describes branch-only
in-flight state; the high-level rationale is captured in commit
messages and is enough for the long-term record.
