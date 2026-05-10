// src/api.ts
//
// Thin client over Muse's two API surfaces:
//   1. /api/*  — internal, JWT-bearer, used for login, scan progress, stats.
//   2. /rest/* — Subsonic-compatible. We use it for everything user-facing
//                (browse, search, stream, cover art) so the frontend exercises
//                the very same surface that mobile clients will hit. If a
//                Subsonic call works here, we know third-party clients will
//                work too.
//
// WHY we keep the user's plaintext password in localStorage (not just the JWT):
//   The Subsonic protocol authenticates every call with EITHER `p=password`
//   OR `t=md5(password+salt) & s=salt`. Both require the server to be able
//   to recover the user's password to verify. Our backend caches plaintext
//   in memory after a successful /api/auth/login, but if the user reloads
//   the SPA we need to be able to re-authenticate Subsonic calls without
//   forcing a fresh login. So we keep the password client-side under a key
//   that is wiped on signout. This is a known Subsonic constraint, not an
//   invention of ours; documented in README "security notes".

import { authState, signOut } from "./auth";

/* ---------- low-level helpers ---------- */

const SUBSONIC_CLIENT = "muse-web";
const SUBSONIC_VERSION = "1.16.1";

/** Build the auth query parameters required by every Subsonic request. */
function subsonicAuthParams(): URLSearchParams {
  const { username, password } = authState();
  if (!username || !password) {
    throw new Error("Not authenticated");
  }
  // We use plain `p=` rather than the token+salt scheme for simplicity.
  // Both are equally secure when the connection is HTTPS; on plain HTTP
  // neither is secure. See README "security notes".
  const params = new URLSearchParams({
    u: username,
    p: password,
    v: SUBSONIC_VERSION,
    c: SUBSONIC_CLIENT,
    f: "json",
  });
  return params;
}

/** Make a Subsonic-style request and unwrap the `subsonic-response` envelope. */
export async function subsonic<T = unknown>(
  endpoint: string,
  extra: Record<string, string | number | undefined> = {},
): Promise<T> {
  const params = subsonicAuthParams();
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined && v !== null) params.set(k, String(v));
  }

  const url = `/rest/${endpoint}.view?${params.toString()}`;
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    if (res.status === 401) signOut();
    throw new Error(`HTTP ${res.status} on ${endpoint}`);
  }
  const body = await res.json();
  const env = body["subsonic-response"];
  if (!env) throw new Error(`Malformed Subsonic response from ${endpoint}`);
  if (env.status === "failed") {
    if (env.error?.code === 40 || env.error?.code === 41) signOut();
    throw new Error(env.error?.message ?? `Subsonic error on ${endpoint}`);
  }
  return env as T;
}

/** Build a cover-art URL — used as a CSS background or img src. */
export function coverArtUrl(coverArtId: string | number | null | undefined, size = 300): string | null {
  if (!coverArtId) return null;
  const params = subsonicAuthParams();
  params.set("id", String(coverArtId));
  params.set("size", String(size));
  return `/rest/getCoverArt.view?${params.toString()}`;
}

/** Build the URL the <audio> element points at to play a track. */
export function streamUrl(
  trackId: string | number,
  opts: { format?: string; maxBitRate?: number } = {},
): string {
  const params = subsonicAuthParams();
  params.set("id", String(trackId));
  // If caller didn't pass explicit options, fall back to the user's
  // saved transcoding preferences (set in the Workshop view).
  const prefs = getTranscodingPrefs();
  const fmt = opts.format ?? prefs.format;
  const br = opts.maxBitRate ?? prefs.maxBitRate;
  if (fmt && fmt !== "auto") params.set("format", fmt);
  if (br) params.set("maxBitRate", String(br));
  return `/rest/stream.view?${params.toString()}`;
}

/* ---------- Transcoding preferences (client-side) ---------- */

export type TranscodingFormat = "auto" | "raw" | "mp3" | "opus" | "ogg";

export interface TranscodingPrefs {
  /** "auto" defers to the server's `default_transcode_format`. */
  format: TranscodingFormat;
  /** Cap on the requested bitrate. 0 / null means "no client-side cap". */
  maxBitRate: number | null;
}

const PREFS_KEY = "muse.transcoding";

/** Read the user's saved preferences. Defaults to "auto" with no cap. */
export function getTranscodingPrefs(): TranscodingPrefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { format: "auto", maxBitRate: null };
    const p = JSON.parse(raw);
    return {
      format: (p.format ?? "auto") as TranscodingFormat,
      maxBitRate: typeof p.maxBitRate === "number" ? p.maxBitRate : null,
    };
  } catch {
    return { format: "auto", maxBitRate: null };
  }
}

/** Persist the user's preferences. The next call to streamUrl() picks them up. */
export function setTranscodingPrefs(prefs: TranscodingPrefs): void {
  localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
}

export interface TranscodingPolicy {
  transcoding_enabled: boolean;
  default_format: string;
  default_bitrate: number;
  max_streaming_bitrate: number | null;
  presets: { format: string; bitrate: number; content_type: string }[];
}

export async function getTranscodingPolicy(): Promise<TranscodingPolicy> {
  return apiGet<TranscodingPolicy>("/api/transcoding/policy");
}

/* ---------- /api/* (JWT) ---------- */

async function jwtFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { token } = authState();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) signOut();
  return res;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await jwtFetch(path, { method: "POST", body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
  return res.json();
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await jwtFetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
  return res.json();
}

/* ---------- typed wrappers around Subsonic responses ---------- */

export interface SubsonicArtist {
  id: string;
  name: string;
  albumCount?: number;
  coverArt?: string;
}
export interface SubsonicIndex {
  name: string;
  artist?: SubsonicArtist[];
}
export interface SubsonicAlbum {
  id: string;
  name: string;
  artist: string;
  artistId?: string;
  coverArt?: string;
  songCount?: number;
  duration?: number;
  year?: number;
  genre?: string;
  created?: string;
}
export interface SubsonicSong {
  id: string;
  parent?: string;
  title: string;
  album?: string;
  artist?: string;
  track?: number;
  year?: number;
  genre?: string;
  coverArt?: string;
  size?: number;
  contentType?: string;
  suffix?: string;
  duration?: number;
  bitRate?: number;
  path?: string;
  albumId?: string;
  artistId?: string;
  type?: string;
  discNumber?: number;
  lyrics?: string | null; //** Lyrics implementation */
}

export async function getIndexes(): Promise<{ index: SubsonicIndex[] }> {
  const env = await subsonic<{ indexes: { index: SubsonicIndex[] } }>("getIndexes");
  return env.indexes ?? { index: [] };
}

export async function getMusicDirectory(
  id: string,
): Promise<{ id: string; name: string; child: any[] }> {
  const env = await subsonic<{ directory: { id: string; name: string; child: any[] } }>(
    "getMusicDirectory",
    { id },
  );
  const dir = env.directory ?? { id, name: "", child: [] };
  // Some clients want to know whether children are albums or songs;
  // we leave that to consumers via the `isDir`/`type` field.
  return dir;
}

export async function getAlbumList(
  type: "newest" | "alphabeticalByName" | "byYear" | "byGenre" | "random" = "newest",
  size = 60,
  offset = 0,
  extra: Record<string, string | number> = {},
): Promise<SubsonicAlbum[]> {
  const env = await subsonic<{ albumList: { album?: SubsonicAlbum[] } }>("getAlbumList", {
    type,
    size,
    offset,
    ...extra,
  });
  return env.albumList?.album ?? [];
}

/** cleaner getAlbum */
type GetAlbumResponse = { album: SubsonicAlbum & { song?: SubsonicSong[] } };

export async function getAlbum(id: string): Promise<GetAlbumResponse> {
  return subsonic<GetAlbumResponse>("getAlbum", { id });
}


export async function getSong(id: string): Promise<SubsonicSong> {
  const result = await subsonic<{ song: SubsonicSong }>("getSong", { id });
  return result.song;
}

export async function search3(
  query: string,
  opts: {
    artistCount?: number;
    albumCount?: number;
    songCount?: number;
    artistOffset?: number;
    albumOffset?: number;
    songOffset?: number;
  } = {},
): Promise<{ artist: SubsonicArtist[]; album: SubsonicAlbum[]; song: SubsonicSong[] }> {
  const env = await subsonic<{
    searchResult3: { artist?: SubsonicArtist[]; album?: SubsonicAlbum[]; song?: SubsonicSong[] };
  }>("search3", {
    query,
    artistCount: opts.artistCount ?? 20,
    albumCount:  opts.albumCount  ?? 20,
    songCount:   opts.songCount   ?? 20,
    artistOffset: opts.artistOffset ?? 0,
    albumOffset:  opts.albumOffset  ?? 0,
    songOffset:   opts.songOffset   ?? 0,
  });
  const r = env.searchResult3 ?? {};
  return {
    artist: r.artist ?? [],
    album:  r.album  ?? [],
    song:   r.song   ?? [],
  };
}

/* ---------- Library admin (web-side) ---------- */

export interface LibraryStats {
  artists: number;
  albums: number;
  tracks: number;
  total_duration_seconds: number;
}

export async function libraryStats(): Promise<LibraryStats> {
  return apiGet<LibraryStats>("/api/stats");
}

export interface ScanProgress {
  running: boolean;
  started_at?: number;
  finished_at?: number;
  folders_total?: number;
  folders_done?: number;
  files_seen?: number;
  files_to_parse?: number;
  files_parsed?: number;
  files_added?: number;
  files_updated?: number;
  files_removed?: number;
  files_skipped?: number;
  errors?: number;
  current_folder?: string;
}

export async function getScanProgress(): Promise<ScanProgress> {
  return apiGet<ScanProgress>("/api/scan");
}

export async function startScan(): Promise<{ started: boolean; progress: ScanProgress }> {
  return apiPost<{ started: boolean; progress: ScanProgress }>("/api/scan", {});
}

/* ---------- Maintenance / GC ---------- */

export interface GcResult {
  started_at: number;
  finished_at: number;
  duration_seconds: number;
  empty_albums_removed: number;
  empty_artists_removed: number;
  dangling_starred_removed: number;
  orphan_artwork_files_removed: number;
  orphan_artwork_bytes_freed: number;
  wal_checkpointed: boolean;
  vacuumed: boolean;
  db_size_before_bytes: number;
  db_size_after_bytes: number;
}

export async function runGc(): Promise<GcResult> {
  return apiPost<GcResult>("/api/maintenance/gc", {});
}

export async function runVacuum(): Promise<GcResult> {
  return apiPost<GcResult>("/api/maintenance/vacuum", {});
}

/* ---------- Music folders ---------- */

export interface MusicFolder {
  id: number;
  name: string;
  path: string;
  track_count: number;
}

export async function listFolders(): Promise<MusicFolder[]> {
  return apiGet<MusicFolder[]>("/api/folders");
}

export async function addFolder(name: string, path: string): Promise<MusicFolder> {
  return apiPost<MusicFolder>("/api/folders", { name, path });
}

export async function deleteFolder(id: number): Promise<{ deleted: boolean; folder: MusicFolder }> {
  // Custom path because apiPost is for JSON-body POSTs; we want a DELETE.
  const { token } = authState();
  const res = await fetch(`/api/folders/${id}`, {
    method: "DELETE",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (res.status === 401) signOut();
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* swallow */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- Artist detail (custom endpoint) ---------- */

export interface ArtistAlbum {
  id: string;
  name: string;
  artist: string;
  artistId: string;
  year: number | null;
  genre: string | null;
  release_type: string | null;
  track_count: number | null;
  duration: number | null;
  coverArt: string | null;
}

export interface ArtistDetail {
  id: string;
  name: string;
  album_count: number | null;
  albums_grouped: {
    albums: ArtistAlbum[];
    eps: ArtistAlbum[];
    singles: ArtistAlbum[];
    compilations: ArtistAlbum[];
    other: ArtistAlbum[];
  };
  bio: {
    summary: string;
    content: string;
    url: string | null;
    image_url: string | null;
    tags: string[];
  } | null;
}

export async function getArtistDetail(artistId: string): Promise<ArtistDetail> {
  return apiGet<ArtistDetail>(`/api/artist/${encodeURIComponent(artistId)}`);
}

/* ---------- Stream-plan resolver (client-side mirror) ---------- */

export interface StreamPlan {
  /** True when the server will transcode; false when streaming the raw file. */
  transcoded: boolean;
  /** Format actually delivered (e.g. "mp3" or the source's suffix). */
  format: string;
  /** Bitrate in kbps. May be null when the source's bitrate is unknown. */
  bitrate: number | null;
  /** True when the request would land on the server's bitrate cap. */
  capped: boolean;
}

/**
 * Predict what the server will do for a given track, using the same rules
 * as backend/streaming/presets.py::resolve_preset (plus the cap from the
 * server's policy). Used to render the "ORIG FLAC" / "MP3 192" badge in
 * the player without an extra round-trip per track.
 *
 * Keep this in lock-step with the server logic; the unit-test surface for
 * this is "does the badge match what the player actually got" — if you
 * change resolve_preset, change this too.
 */
export function resolveStreamPlan(
  sourceFormat: string,
  sourceBitrate: number | null,
  prefs: TranscodingPrefs,
  policy: TranscodingPolicy | null,
): StreamPlan {
  const src = (sourceFormat || "").toLowerCase();
  // 1. Master kill-switch on the server forces raw.
  if (policy && !policy.transcoding_enabled) {
    return { transcoded: false, format: src, bitrate: sourceBitrate, capped: false };
  }
  // 2. "auto" defers to the server's default; "raw" is explicit no-transcode.
  let requestedFormat: string = prefs.format;
  if (requestedFormat === "auto") {
    requestedFormat = policy?.default_format ?? "raw";
  }
  let requestedBitrate: number | null = prefs.maxBitRate ?? null;

  // 3. Apply server cap.
  let capped = false;
  const cap = policy?.max_streaming_bitrate ?? null;
  if (cap !== null) {
    if (requestedBitrate === null || requestedBitrate > cap) {
      requestedBitrate = cap;
      capped = true;
    }
    if (
      (requestedFormat === "raw") &&
      sourceBitrate !== null && sourceBitrate > cap
    ) {
      requestedFormat = "mp3";  // forced re-encode by the cap
      capped = true;
    }
  }

  if (requestedFormat === "raw") {
    return { transcoded: false, format: src, bitrate: sourceBitrate, capped };
  }
  // Same format and we're not asking for a lower bitrate? No transcode.
  if (requestedFormat === src) {
    if (
      requestedBitrate === null ||
      (sourceBitrate !== null && requestedBitrate >= sourceBitrate)
    ) {
      return { transcoded: false, format: src, bitrate: sourceBitrate, capped };
    }
  }
  return {
    transcoded: true,
    format: requestedFormat,
    bitrate: requestedBitrate ?? policy?.default_bitrate ?? 192,
    capped,
  };
}
