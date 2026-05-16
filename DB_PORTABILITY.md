# db-portability-prep — resume notes

This file lives on the `db-portability-prep` branch only. It tracks the
progress of the optional second-database-backend prep work so the next
session can pick up exactly where this one stopped.

Companion plan with full context: `~/.claude/plans/export-async-function-staritems-snappy-snail.md`.

## Goal

Reduce SQLite coupling in the codebase so that a future port to Postgres
(or MariaDB / MySQL) is a long afternoon rather than two weeks. **No
behaviour change today.** Every commit on this branch leaves the test
suite at 225 passed / 6 xfailed and serves the app identically.

We are **not** porting to Postgres in this branch. We're making the
codebase neutral enough that the eventual port is a small focused
change rather than a global rewrite.

## Done so far

| # | Commit subject | What it did |
|---|---|---|
| 1 | `db: add backend/db/errors.py for centralised exception re-exports` | New file aliasing `sqlite3.IntegrityError` / `OperationalError` / `DatabaseError`. Future driver swap edits this one file. |
| 2 | `cli: switch sqlite3.IntegrityError to backend.db.errors` | [backend/cli.py](backend/cli.py) no longer imports `sqlite3`. |
| 3 | `api/web: switch sqlite3.IntegrityError to backend.db.errors` | [backend/api/web.py](backend/api/web.py) no longer imports `sqlite3`. Two except clauses updated. |
| 4 | `api/subsonic/users: switch sqlite3.IntegrityError to backend.db.errors` | [backend/api/subsonic/users.py](backend/api/subsonic/users.py) no longer imports `sqlite3`. |
| 5 | `db/connection: extract PRAGMA block into _tune_sqlite() helper` | Seven SQLite-only PRAGMAs collected into a dedicated function in [backend/db/connection.py](backend/db/connection.py). `_new_connection()` is now a thin wrapper that's trivial to swap for a Postgres factory. |
| 6 | `queries: convert starred section to named parameter binding` | First batch of `?` → `:name` conversion: `star_item`, `unstar_item`, `get_starred_items`. |
| 7 | `queries: convert music_folders section to named parameter binding` | `get_music_folder`, `get_music_folder_by_path`, `add_music_folder`, `delete_music_folder`. |

Verification after each commit: targeted pytest run, then occasionally the
full suite. State at the end of commit 7: **225 passed, 6 xfailed**.

## Confirmed: no `sqlite3.*` references survive outside `backend/db/`

Run this to verify it stays that way:

```bash
grep -rn "^import sqlite3\|sqlite3\." backend/ --include="*.py" | grep -v "backend/db/"
# expected: empty
```

## What remains — the rest of the `?` → `:name` conversion in `backend/db/queries.py`

The pattern is identical for every function. Each query has two changes:

1. **In the SQL string**: replace every `?` with `:meaningful_name`.
2. **At the `.execute(...)` call site**: replace the tuple of values with a
   dict whose keys match the names you chose.

Example, before:

```python
get_conn().execute(
    "DELETE FROM starred WHERE user_id = ? AND target_type = ? AND target_id = ?",
    (user_id, target_type, target_id),
)
```

After:

```python
get_conn().execute(
    """
    DELETE FROM starred
     WHERE user_id = :user_id
       AND target_type = :target_type
       AND target_id = :target_id
    """,
    {"user_id": user_id, "target_type": target_type, "target_id": target_id},
)
```

SQLite accepts both styles, so a half-converted file still works.
Postgres (`psycopg3`) accepts the `:name` form natively.

### Sections still to do, in file order

These are the section dividers in [backend/db/queries.py](backend/db/queries.py)
(from `grep -n "^# ---" backend/db/queries.py` and `^def`):

- [ ] **Play count / scrobble** — lines ~232–254. Two functions: `play_count`, `get_playcount_by_user`. Note `play_count` uses `ON CONFLICT … DO UPDATE SET ... excluded.last_played`; placeholder conversion only, no syntax rewrite needed.
- [ ] **Playlists** — lines ~255–444. Seven functions: `list_playlists`, `get_playlist`, `create_playlist`, `update_playlist`, `delete_playlist`, `add_tracks_to_playlist`, `replace_playlist_tracks`, `remove_tracks_from_playlist`. Watch for the dynamic chunking in `delete_tracks` and friends (`",".join("?" * len(chunk))` style) — those need a small refactor since `:name` can't be repeated; use `:p0, :p1, ...` or convert just those queries to use IN with a constructed dict.
- [ ] **Play queue** — lines ~446–518. `get_play_queue`, `save_play_queue`. Note `save_play_queue` uses `INSERT OR REPLACE` — leave the SQL syntax as-is (that's a separate concern in any actual port).
- [ ] **Artists** — lines ~519–694. `upsert_artist`, `list_artists_indexed`, `get_artist`, `list_genre_count`, `list_artist_appearances`, `list_artist_albums`. `upsert_artist` uses `ON CONFLICT DO UPDATE … RETURNING id` — only the placeholders change.
- [ ] **Albums** — lines ~696–901. `upsert_album`, `get_album`, `list_albums`, `update_album_aggregates`, `update_artist_aggregates`, `set_album_cover_art`, `set_artist_image`, `list_artists_missing_image`.
- [ ] **Tracks** — lines ~903–1158. `list_random_songs`, `list_song_by_genre`, `upsert_track`, `get_track`, `list_album_tracks`, `get_existing_paths_for_folder`, `delete_tracks`, `cleanup_empty_albums_and_artists`. **`upsert_track` already uses named params** — leave it alone (it was the only function that already did). `delete_tracks` has the same dynamic `?`-repetition pattern as the playlist remove helpers; handle the same way.
- [ ] **Search** — lines ~1160–1244. `search3` only. Big multi-CTE query; convert carefully. The FTS5 `MATCH ?` clause becomes `MATCH :query`.
- [ ] **Users** — lines ~1245–end. `create_user`, `get_user_by_username`, `get_user_by_id`, `list_users`, `update_user`, `update_user_password`, `update_encrypted_password`, `set_user_disabled`, `set_user_admin`, `delete_user`, `delete_user_by_username`.

### Recommended cadence

One commit per section, exactly as commits 6 and 7. After each:

```bash
python -m pytest tests/ -k <section-keyword> --no-header -q
# e.g. -k "playlist", -k "user", -k "search"
```

Then a full suite run at the end:

```bash
python -m pytest tests/ --no-header -q
# expected: 225 passed, 6 xfailed
```

## Out of scope for this branch

- `INSERT OR IGNORE` / `INSERT OR REPLACE` rewrites — that's a real porting
  concern, not prep work. Three sites: [queries.py:71](backend/db/queries.py#L71),
  [queries.py:506](backend/db/queries.py#L506), [migrations.py:75](backend/db/migrations.py#L75).
- FTS5 — the `virt_fts5` table and three triggers stay SQLite-only.
- VACUUM and WAL checkpoint code in [backend/db/maintenance.py](backend/db/maintenance.py).
- `COLLATE NOCASE` audit.

## How to merge or abandon

This branch never touches `main`. If the prep work is good, merge it with
a regular PR. If priorities shift, the branch can sit indefinitely or be
deleted — main is untouched either way.

This file should be deleted in the merge commit, OR kept and updated as
the conversion progresses on this branch.
