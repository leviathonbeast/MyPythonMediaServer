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
//   OR `t=md5(password+salt) & s=salt`. Both require knowing the password so
//   the server can verify. Our backend caches the plaintext in memory after a
//   successful /api/auth/login. We keep it client-side so we can silently
//   re-warm that cache after a server restart without forcing a manual re-login.
//   This is a known Subsonic constraint; documented in README "security notes".

import { authState, signOut } from "./auth";

/* ---------- MD5 (required by Subsonic token+salt auth) ----------
 *
 * The Subsonic spec requires t = md5(password + salt). The Web Crypto API
 * doesn't support MD5 (it's cryptographically broken), so we implement it
 * inline. This is the standard RFC 1321 algorithm — the constants are
 * derived as floor(abs(sin(i)) * 2^32) for i = 1..64.
 */
function _md5(str: string): string {
  // 32-bit addition that avoids JS float precision loss on large unsigned values.
  const add = (x: number, y: number): number => {
    const l = (x & 0xffff) + (y & 0xffff);
    return (((x >> 16) + (y >> 16) + (l >> 16)) << 16) | (l & 0xffff);
  };
  const rol = (n: number, s: number) => (n << s) | (n >>> (32 - s));
  // op(f, a, b, x, s, t) = b + rol(a + f + x + t, s)  — the core MD5 step.
  const op = (f: number, a: number, b: number, x: number, s: number, t: number) =>
    add(rol(add(add(a, f), add(x, t)), s), b);

  const bytes = new TextEncoder().encode(str);
  const len = bytes.length;
  const M = new Array<number>(((len + 72) >> 6) << 4).fill(0);
  for (let i = 0; i < len; i++) M[i >> 2] |= bytes[i] << ((i & 3) * 8);
  M[len >> 2] |= 0x80 << ((len & 3) * 8);
  M[M.length - 2] = len * 8;

  // RFC 1321 initial hash values.
  let a = 0x67452301, b = 0xefcdab89 | 0, c = 0x98badcfe | 0, d = 0x10325476;

  for (let i = 0; i < M.length; i += 16) {
    const [aa, bb, cc, dd] = [a, b, c, d];
    const w = (k: number) => M[i + k];

    // Each group of 4 lines cycles: update a, then d, then c, then b.
    // Round 1  F(b,c,d)=(b&c)|(~b&d)
    a = op((b & c) | (~b & d), a, b, w(0), 7, -680876936); d = op((a & b) | (~a & c), d, a, w(1), 12, -389564586);
    c = op((d & a) | (~d & b), c, d, w(2), 17, 606105819); b = op((c & d) | (~c & a), b, c, w(3), 22, -1044525330);
    a = op((b & c) | (~b & d), a, b, w(4), 7, -176418897); d = op((a & b) | (~a & c), d, a, w(5), 12, 1200080426);
    c = op((d & a) | (~d & b), c, d, w(6), 17, -1473231341); b = op((c & d) | (~c & a), b, c, w(7), 22, -45705983);
    a = op((b & c) | (~b & d), a, b, w(8), 7, 1770035416); d = op((a & b) | (~a & c), d, a, w(9), 12, -1958414417);
    c = op((d & a) | (~d & b), c, d, w(10), 17, -42063); b = op((c & d) | (~c & a), b, c, w(11), 22, -1990404162);
    a = op((b & c) | (~b & d), a, b, w(12), 7, 1804603682); d = op((a & b) | (~a & c), d, a, w(13), 12, -40341101);
    c = op((d & a) | (~d & b), c, d, w(14), 17, -1502002290); b = op((c & d) | (~c & a), b, c, w(15), 22, 1236535329);

    // Round 2  G(b,c,d)=(b&d)|(c&~d)
    a = op((b & d) | (c & ~d), a, b, w(1), 5, -165796510); d = op((a & c) | (b & ~c), d, a, w(6), 9, -1069501632);
    c = op((d & b) | (a & ~b), c, d, w(11), 14, 643717713); b = op((c & a) | (d & ~a), b, c, w(0), 20, -373897302);
    a = op((b & d) | (c & ~d), a, b, w(5), 5, -701558691); d = op((a & c) | (b & ~c), d, a, w(10), 9, 38016083);
    c = op((d & b) | (a & ~b), c, d, w(15), 14, -660478335); b = op((c & a) | (d & ~a), b, c, w(4), 20, -405537848);
    a = op((b & d) | (c & ~d), a, b, w(9), 5, 568446438); d = op((a & c) | (b & ~c), d, a, w(14), 9, -1019803690);
    c = op((d & b) | (a & ~b), c, d, w(3), 14, -187363961); b = op((c & a) | (d & ~a), b, c, w(8), 20, 1163531501);
    a = op((b & d) | (c & ~d), a, b, w(13), 5, -1444681467); d = op((a & c) | (b & ~c), d, a, w(2), 9, -51403784);
    c = op((d & b) | (a & ~b), c, d, w(7), 14, 1735328473); b = op((c & a) | (d & ~a), b, c, w(12), 20, -1926607734);

    // Round 3  H(b,c,d)=b^c^d
    a = op(b ^ c ^ d, a, b, w(5), 4, -378558); d = op(a ^ b ^ c, d, a, w(8), 11, -2022574463);
    c = op(d ^ a ^ b, c, d, w(11), 16, 1839030562); b = op(c ^ d ^ a, b, c, w(14), 23, -35309556);
    a = op(b ^ c ^ d, a, b, w(1), 4, -1530992060); d = op(a ^ b ^ c, d, a, w(4), 11, 1272893353);
    c = op(d ^ a ^ b, c, d, w(7), 16, -155497632); b = op(c ^ d ^ a, b, c, w(10), 23, -1094730640);
    a = op(b ^ c ^ d, a, b, w(13), 4, 681279174); d = op(a ^ b ^ c, d, a, w(0), 11, -358537222);
    c = op(d ^ a ^ b, c, d, w(3), 16, -722521979); b = op(c ^ d ^ a, b, c, w(6), 23, 76029189);
    a = op(b ^ c ^ d, a, b, w(9), 4, -640364487); d = op(a ^ b ^ c, d, a, w(12), 11, -421815835);
    c = op(d ^ a ^ b, c, d, w(15), 16, 530742520); b = op(c ^ d ^ a, b, c, w(2), 23, -995338651);

    // Round 4  I(b,c,d)=c^(b|~d)
    a = op(c ^ (b | ~d), a, b, w(0), 6, -198630844); d = op(b ^ (a | ~c), d, a, w(7), 10, 1126891415);
    c = op(a ^ (d | ~b), c, d, w(14), 15, -1416354905); b = op(d ^ (c | ~a), b, c, w(5), 21, -57434055);
    a = op(c ^ (b | ~d), a, b, w(12), 6, 1700485571); d = op(b ^ (a | ~c), d, a, w(3), 10, -1894986606);
    c = op(a ^ (d | ~b), c, d, w(10), 15, -1051523); b = op(d ^ (c | ~a), b, c, w(1), 21, -2054922799);
    a = op(c ^ (b | ~d), a, b, w(8), 6, 1873313359); d = op(b ^ (a | ~c), d, a, w(15), 10, -30611744);
    c = op(a ^ (d | ~b), c, d, w(6), 15, -1560198380); b = op(d ^ (c | ~a), b, c, w(13), 21, 1309151649);
    a = op(c ^ (b | ~d), a, b, w(4), 6, -145523070); d = op(b ^ (a | ~c), d, a, w(11), 10, -1120210379);
    c = op(a ^ (d | ~b), c, d, w(2), 15, 718787259); b = op(d ^ (c | ~a), b, c, w(9), 21, -343485551);

    [a, b, c, d] = [add(a, aa), add(b, bb), add(c, cc), add(d, dd)];
  }

  return [a, b, c, d]
    .map(v => [0, 8, 16, 24].map(s => ((v >> s) & 0xff).toString(16).padStart(2, "0")).join(""))
    .join("");
}

/* ---------- low-level helpers ---------- */

const SUBSONIC_CLIENT = "muse-web";
const SUBSONIC_VERSION = "1.16.1";

/**
 * Build the auth query parameters required by every Subsonic request.
 * Uses token+salt (t= / s=) rather than plaintext p= so the password
 * never appears in URLs (browser history, server access logs, image src).
 */
function subsonicAuthParams(): URLSearchParams {
  const { username, password } = authState();
  if (!username || !password) throw new Error("Not authenticated");

  // Random 16-hex-char salt. Each request gets a different salt so the
  // token also differs — replaying a captured URL doesn't work.
  const salt = Array.from(crypto.getRandomValues(new Uint8Array(8)))
    .map(b => b.toString(16).padStart(2, "0"))
    .join("");
  const token = _md5(password + salt);

  return new URLSearchParams({
    u: username,
    t: token,
    s: salt,
    v: SUBSONIC_VERSION,
    c: SUBSONIC_CLIENT,
    f: "json",
  });
}

/**
 * Re-warm the server's in-memory plaintext cache by posting credentials via
 * the JSON /api/auth/login endpoint (POST body, never a URL query param).
 * Returns true on success. Called automatically when token+salt auth fails,
 * which happens after a server restart clears the cache.
 */
async function _rewarmCache(): Promise<boolean> {
  const { username, password } = authState();
  if (!username || !password) return false;
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Make a Subsonic-style request and unwrap the `subsonic-response` envelope. */
export async function subsonic<T = unknown>(
  endpoint: string,
  extra: Record<string, string | number | undefined> = {},
  _retry = true,
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
    // Error 40 = wrong credentials. With token+salt auth this can happen if
    // the server restarted and lost its in-memory plaintext cache. Try to
    // re-warm the cache via a POST (password in body, never in a URL) and
    // retry once. Only sign out if the retry also fails.
    if ((env.error?.code === 40 || env.error?.code === 41) && _retry) {
      const warmed = await _rewarmCache();
      if (warmed) return subsonic<T>(endpoint, extra, false);
      signOut();
    } else if (env.error?.code === 40 || env.error?.code === 41) {
      signOut();
    }
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
  // saved transcoding preferences (set in the Settings view).
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

// Preferences are namespaced by username so users sharing a browser don't
// inherit each other's settings on login. The legacy un-namespaced key is
// kept around for a single one-time migration; see _prefsKey() below.
const PREFS_KEY_PREFIX = "muse.transcoding.";
const PREFS_LEGACY_KEY = "muse.transcoding";

/**
 * Compute the localStorage key for the currently-authenticated user.
 *
 * Returns null if no user is signed in (in which case prefs read returns
 * defaults and prefs write is a no-op — we don't want to bind one user's
 * preferences to the empty/anonymous slot).
 */
function _prefsKey(): string | null {
  const { username } = authState();
  if (!username) return null;
  return PREFS_KEY_PREFIX + username;
}

/** Read the user's saved preferences. Defaults to "auto" with no cap. */
export function getTranscodingPrefs(): TranscodingPrefs {
  const key = _prefsKey();
  if (!key) return { format: "auto", maxBitRate: null };

  // One-time migration: pre-namespace installs stored prefs under
  // "muse.transcoding". If we find that and the current user has no
  // namespaced entry, copy the legacy value over and remove the legacy
  // key so subsequent users on the same browser don't inherit it.
  if (localStorage.getItem(key) === null) {
    const legacy = localStorage.getItem(PREFS_LEGACY_KEY);
    if (legacy !== null) {
      localStorage.setItem(key, legacy);
      localStorage.removeItem(PREFS_LEGACY_KEY);
    }
  }

  try {
    const raw = localStorage.getItem(key);
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
  const key = _prefsKey();
  if (!key) return;  // not signed in — nothing to bind the prefs to
  localStorage.setItem(key, JSON.stringify(prefs));
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
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* swallow */ }
    throw new Error(msg);
  }
  return res.json();
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await jwtFetch(path);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* swallow */ }
    throw new Error(msg);
  }
  return res.json();
}

async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const res = await jwtFetch(path, { method: "PATCH", body: JSON.stringify(body) });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* swallow */ }
    throw new Error(msg);
  }
  return res.json();
}

async function apiDelete<T>(path: string): Promise<T> {
  const res = await jwtFetch(path, { method: "DELETE" });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* swallow */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- typed wrappers around Subsonic responses ---------- */

export interface SubsonicArtist {
  id: string;
  name: string;
  albumCount?: number;
  coverArt?: string;
  starred?: string;
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
  starred?: string;
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
  playCount?: number;
  path?: string;
  albumId?: string;
  artistId?: string;
  type?: string;
  discNumber?: number;
  starred?: string;
  lyrics?: string | null; //** Lyrics implementation */
  /** Web-UI hint: track has lyrics (set by getAlbum / getSong). Omitted by
   * queries that don't select lyrics info, so treat undefined as "unknown". */
  hasLyrics?: boolean;
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

export async function getSongsByGenre(
  genre: string,
  count = 50,
  offset = 0,
): Promise<SubsonicSong[]> {
  const env = await subsonic<{ songsByGenre: { song?: SubsonicSong[] } }>("getSongsByGenre", {
    genre, count, offset,
  });
  return env.songsByGenre?.song ?? [];
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

/**
 * Tracks sonically similar to `id` (OpenSubsonic sonicSimilarity extension).
 * Returns the song objects, most-similar first. Empty when the track has no
 * feature vector yet — run the analysis pass from Settings to populate them.
 *
 * The response is the flat `sonicMatch: [{entry, similarity}]` shape; we unwrap
 * to just the song entries (the similarity score isn't surfaced in the UI).
 */
export async function getSonicSimilarTracks(
  id: string,
  count = 12,
): Promise<SubsonicSong[]> {
  const env = await subsonic<{
    sonicMatch?: Array<{ entry: SubsonicSong; similarity: number }>;
  }>("getSonicSimilarTracks", { id, count });
  return (env.sonicMatch ?? []).map(m => m.entry);
}

/**
 * Core Subsonic getSimilarSongs — an "artist radio" seeded from `id` (an artist,
 * album, or song id). The server picks a seed track and returns it followed by
 * its sonic neighbours. Empty when the seed has no feature vector yet (run the
 * analysis pass from Settings).
 *
 * Unlike getSonicSimilarTracks (flat sonicMatch), this is the standard nested
 * `similarSongs: { song: [...] }` envelope, so we unwrap to the song array.
 */
export async function getSimilarSongs(
  id: string,
  count = 50,
): Promise<SubsonicSong[]> {
  const env = await subsonic<{ similarSongs?: { song?: SubsonicSong[] } }>(
    "getSimilarSongs", { id, count });
  return env.similarSongs?.song ?? [];
}

/**
 * getSimilarSongs2 — the ID3 form of getSimilarSongs. `id` is always an artist
 * id. Same seed-and-neighbours behaviour; the response key is `similarSongs2`.
 */
export async function getSimilarSongs2(
  id: string,
  count = 50,
): Promise<SubsonicSong[]> {
  const env = await subsonic<{ similarSongs2?: { song?: SubsonicSong[] } }>(
    "getSimilarSongs2", { id, count });
  return env.similarSongs2?.song ?? [];
}

// ---- Lyrics ---------------------------------------------------------------

/** One lyric line. `time` is seconds into the song, or -1 for an untimed
 * (plain-text) line. `text` may be empty (LRC instrumental breaks). */
export interface LyricLine {
  time: number;
  text: string;
}

/** Normalised lyrics for a track. `synced` = the lines carry real timestamps
 * (LRC) and can drive karaoke-style highlighting. */
export interface SongLyrics {
  synced: boolean;
  lines: LyricLine[];
}

/**
 * getLyricsBySongId (OpenSubsonic songLyrics extension). The server returns
 * structuredLyrics with per-line millisecond `start` offsets when the track
 * has LRC lyrics, or plain lines otherwise. We flatten the first block into
 * the {synced, lines} shape both the track page and the player panel consume,
 * converting `start` (ms) → `time` (seconds). Empty lines when there are none.
 */
export async function getLyricsBySongId(id: string): Promise<SongLyrics> {
  const env = await subsonic<{
    lyricsList?: {
      structuredLyrics?: Array<{
        synced?: boolean;
        line?: Array<{ start?: number; value?: string }>;
      }>;
    };
  }>("getLyricsBySongId", { id });

  const block = env.lyricsList?.structuredLyrics?.[0];
  if (!block?.line?.length) return { synced: false, lines: [] };

  const synced = block.synced === true;
  const lines: LyricLine[] = block.line.map((l) => ({
    time: synced && typeof l.start === "number" ? l.start / 1000 : -1,
    text: l.value ?? "",
  }));
  return { synced, lines };
}

export async function getStarred2(): Promise<{
  artist: SubsonicArtist[];
  album: SubsonicAlbum[];
  song: SubsonicSong[];
}> {
  const env = await subsonic<{
    starred2: { artist?: SubsonicArtist[]; album?: SubsonicAlbum[]; song?: SubsonicSong[] };
  }>("getStarred2");
  const r = env.starred2 ?? {};
  return {
    artist: r.artist ?? [],
    album: r.album ?? [],
    song: r.song ?? [],
  };
}

export async function starItems(
  opts: {
    id?: string[];
    albumId?: string[];
    artistId?: string[];
  }): Promise<void> {
  // subsonic() uses URLSearchParams.set() which collapses duplicates,
  // so build the query ourselves to send repeated id/albumId/artistId.
  const params = subsonicAuthParams();
  for (const x of opts.id ?? []) params.append("id", x);
  for (const x of opts.albumId ?? []) params.append("albumId", x);
  for (const x of opts.artistId ?? []) params.append("artistId", x);
  const res = await fetch(`/rest/star.view?${params.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) {
    if (res.status === 401) signOut();
    throw new Error(`HTTP ${res.status} on starItems`);
  }
  const body = await res.json();
  const env = body["subsonic-response"];
  if (!env || env.status === "failed") {
    throw new Error(env?.error?.message ?? "starItems failed");
  }
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
    albumCount: opts.albumCount ?? 20,
    songCount: opts.songCount ?? 20,
    artistOffset: opts.artistOffset ?? 0,
    albumOffset: opts.albumOffset ?? 0,
    songOffset: opts.songOffset ?? 0,
  });
  const r = env.searchResult3 ?? {};
  return {
    artist: r.artist ?? [],
    album: r.album ?? [],
    song: r.song ?? [],
  };
}

/* ---------- Playlists ---------- */

export interface SubsonicPlaylist {
  id: string | number;
  name: string;
  owner: string;
  comment?: string | null;
  public?: boolean;
  created?: string;
  changed?: string;
  songCount?: number;
  duration?: number;
  entry?: SubsonicSong[];
}

export async function getPlaylists(): Promise<SubsonicPlaylist[]> {
  const env = await subsonic<{ playlists: { playlist?: SubsonicPlaylist[] } }>(
    "getPlaylists",
  );
  return env.playlists?.playlist ?? [];
}

export async function getPlaylist(id: string | number): Promise<SubsonicPlaylist> {
  const env = await subsonic<{ playlist: SubsonicPlaylist }>("getPlaylist", {
    id: String(id),
  });
  return env.playlist;
}

/**
 * Create a playlist. `songIds` are RAW track ids (the numeric form the server
 * stores). The Subsonic spec passes them as repeated `songId=` params.
 */
export async function createPlaylist(
  name: string,
  songIds: (string | number)[] = [],
): Promise<SubsonicPlaylist> {
  // subsonic() collapses keys via URLSearchParams; for the repeated-key form
  // we need to build the query ourselves.
  const params = subsonicAuthParams();
  params.set("name", name);
  for (const sid of songIds) params.append("songId", String(sid));
  const res = await fetch(`/rest/createPlaylist.view?${params.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) {
    if (res.status === 401) signOut();
    throw new Error(`HTTP ${res.status} on createPlaylist`);
  }
  const body = await res.json();
  const env = body["subsonic-response"];
  if (!env || env.status === "failed") {
    throw new Error(env?.error?.message ?? "createPlaylist failed");
  }
  return env.playlist as SubsonicPlaylist;
}

/**
 * Update a playlist. Like createPlaylist, songIdToAdd / songIndexToRemove are
 * repeated query params, so we build the URL by hand.
 */
export async function updatePlaylist(
  playlistId: string | number,
  opts: {
    name?: string;
    comment?: string;
    public?: boolean;
    songIdToAdd?: (string | number)[];
    songIndexToRemove?: number[];
  } = {},
): Promise<void> {
  const params = subsonicAuthParams();
  params.set("playlistId", String(playlistId));
  if (opts.name !== undefined) params.set("name", opts.name);
  if (opts.comment !== undefined) params.set("comment", opts.comment);
  if (opts.public !== undefined) params.set("public", String(opts.public));
  for (const sid of opts.songIdToAdd ?? []) params.append("songIdToAdd", String(sid));
  for (const idx of opts.songIndexToRemove ?? []) params.append("songIndexToRemove", String(idx));
  const res = await fetch(`/rest/updatePlaylist.view?${params.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) {
    if (res.status === 401) signOut();
    throw new Error(`HTTP ${res.status} on updatePlaylist`);
  }
  const body = await res.json();
  const env = body["subsonic-response"];
  if (!env || env.status === "failed") {
    throw new Error(env?.error?.message ?? "updatePlaylist failed");
  }
}

export async function deletePlaylist(playlistId: string | number): Promise<void> {
  // Server uses snake_case `playlist_id` query name (not the spec's `id`).
  await subsonic<unknown>("deletePlaylist", { playlist_id: String(playlistId) });
}

/* ---------- Play queue (OpenSubsonic byIndex variants) ---------- */

// We use the OpenSubsonic byIndex pair because the legacy savePlayQueue
// passes the "currently playing" pointer as a track id, which can't
// disambiguate duplicates: a queue [A, B, A] with the second A playing
// round-trips as "currently A" and resumes on the first A. The byIndex
// pair carries the integer queue position instead, which is unambiguous.
//
// The legacy SubsonicPlayQueue type is kept around in case anything else
// in the codebase wants it; player.ts only uses the byIndex flow.

export interface SubsonicPlayQueue {
  current?: string;        // current track id (e.g. "tr-123"); legacy shape
  position?: number;       // playback position in milliseconds
  username?: string;
  changed?: string;        // ISO timestamp
  changedBy?: string;      // client name
  entry?: SubsonicSong[];
}

export interface SubsonicPlayQueueByIndex {
  currentIndex?: number;   // 0-based index into entry[]
  position?: number;       // playback position in milliseconds
  username?: string;
  changed?: string;        // ISO timestamp
  changedBy?: string;      // client name
  entry?: SubsonicSong[];
}

export async function getPlayQueueByIndex(): Promise<SubsonicPlayQueueByIndex | null> {
  const env = await subsonic<{ playQueueByIndex?: SubsonicPlayQueueByIndex }>(
    "getPlayQueueByIndex",
  );
  return env.playQueueByIndex ?? null;
}

/**
 * Save the user's play queue. Track ids are sent as repeated `id=` params, so
 * like createPlaylist we build the URL by hand rather than going through subsonic().
 *
 * `currentIndex` must be in range [0, trackIds.length); the server returns
 * error code 10 otherwise. Pass null when clearing the queue (empty trackIds).
 */
export async function savePlayQueueByIndex(
  trackIds: string[],
  currentIndex: number | null,
  positionMs: number,
): Promise<void> {
  const params = subsonicAuthParams();
  for (const tid of trackIds) params.append("id", tid);
  if (currentIndex !== null) params.set("currentIndex", String(currentIndex));
  params.set("position", String(Math.max(0, Math.floor(positionMs))));
  const res = await fetch(`/rest/savePlayQueueByIndex.view?${params.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) {
    if (res.status === 401) signOut();
    throw new Error(`HTTP ${res.status} on savePlayQueueByIndex`);
  }
  const body = await res.json();
  const env = body["subsonic-response"];
  if (env?.status === "failed") {
    throw new Error(env.error?.message ?? "savePlayQueueByIndex failed");
  }
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

// `force` re-parses every file even if unchanged (slower) — used to backfill
// metadata columns added after the library was first scanned, which a normal
// scan would skip. Maps to POST /api/scan?force=true.
export async function startScan(
  force = false,
): Promise<{ started: boolean; progress: ScanProgress }> {
  const path = force ? "/api/scan?force=true" : "/api/scan";
  return apiPost<{ started: boolean; progress: ScanProgress }>(path, {});
}

export async function cancelScan(): Promise<{ cancelled: boolean; progress: ScanProgress }> {
  return apiPost<{ cancelled: boolean; progress: ScanProgress }>("/api/scan/cancel", {});
}

/* ---------- Sonic analysis (populates track_features for sonicSimilarity) ---------- */

export interface AnalyzeProgress {
  running: boolean;
  started_at?: number;
  finished_at?: number;
  total?: number;     // tracks selected for this run
  analyzed?: number;  // extracted + stored
  failed?: number;    // undecodable / extraction returned nothing
  current?: string;   // path being processed
}

export async function getAnalyzeProgress(): Promise<AnalyzeProgress> {
  return apiGet<AnalyzeProgress>("/api/analyze");
}

// `force` re-analyses every track, not just those missing a current feature
// row — needed after a feature-layout change. Maps to POST /api/analyze?force=true.
export async function startAnalyze(
  force = false,
): Promise<{ started: boolean; progress: AnalyzeProgress }> {
  const path = force ? "/api/analyze?force=true" : "/api/analyze";
  return apiPost<{ started: boolean; progress: AnalyzeProgress }>(path, {});
}

export async function cancelAnalyze(): Promise<{ cancelled: boolean; progress: AnalyzeProgress }> {
  return apiPost<{ cancelled: boolean; progress: AnalyzeProgress }>("/api/analyze/cancel", {});
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
  missing_artwork_refs_cleared: number;
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

export interface RecoverArtworkProgress {
  running: boolean;
  started_at?: number;
  finished_at?: number;
  // Phase A — album cover art
  albums_total: number;
  albums_done: number;
  artwork_recovered: number;
  recovered_via_deezer: number;
  // Phase B — artist photos
  artists_total: number;
  artists_done: number;
  artist_images_recovered: number;
  errors: number;
  /** "albums" | "artists" | "" (idle/finished). */
  phase: string;
}

export async function startRecoverArtwork(): Promise<{ started: boolean; progress: RecoverArtworkProgress }> {
  return apiPost<{ started: boolean; progress: RecoverArtworkProgress }>(
    "/api/maintenance/recover-artwork",
    {},
  );
}

export async function getRecoverArtworkProgress(): Promise<RecoverArtworkProgress> {
  return apiGet<RecoverArtworkProgress>("/api/maintenance/recover-artwork");
}

export async function cancelRecoverArtwork(): Promise<{ cancelled: boolean; progress: RecoverArtworkProgress }> {
  return apiPost<{ cancelled: boolean; progress: RecoverArtworkProgress }>(
    "/api/maintenance/recover-artwork/cancel",
    {},
  );
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

/* ---------- Scrobble threshold (per-browser, per-user) ---------- */
//
// Fraction of the track that must play before we fire `scrobble?submission=true`.
// 0 disables permanent scrobbling entirely (now-playing pings still fire).
// Stored per-user so two accounts on the same browser don't share a knob.

const SCROBBLE_PREFS_KEY_PREFIX = "muse.scrobble.threshold.";

function _scrobbleKey(): string | null {
  const { username } = authState();
  if (!username) return null;
  return SCROBBLE_PREFS_KEY_PREFIX + username;
}

/** Read the user's scrobble threshold. Default 0.5 (Subsonic/Last.fm convention). */
export function getScrobbleThreshold(): number {
  const key = _scrobbleKey();
  if (!key) return 0.5;
  const raw = localStorage.getItem(key);
  if (raw === null) return 0.5;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 && n <= 1 ? n : 0.5;
}

/** Persist the user's scrobble threshold. Value clamped to [0, 1]. */
export function setScrobbleThreshold(value: number): void {
  const key = _scrobbleKey();
  if (!key) return;
  const clamped = Math.max(0, Math.min(1, value));
  localStorage.setItem(key, String(clamped));
}


/* ---------- Last.fm per-user linking ---------- */
//
// Three-step OAuth-like flow:
//   1. lastfmConnect()  → returns { auth_url, token }
//   2. User approves on last.fm; last.fm redirects back to our cb URL
//   3. lastfmComplete(token) exchanges the (now-approved) token for a
//      permanent session key, which the server persists per user.
//
// The token survives the cross-origin round-trip via sessionStorage,
// not via URL params — see views/settings/lastfm.ts.

export interface LastfmStatus {
  linked: boolean;
  username?: string;
}

export async function getLastfmStatus(): Promise<LastfmStatus> {
  return apiGet<LastfmStatus>("/api/me/lastfm");
}

export async function lastfmConnect(): Promise<{ auth_url: string; token: string }> {
  // Backend doesn't require a body; we send {} to satisfy apiPost's signature.
  return apiPost<{ auth_url: string; token: string }>("/api/me/lastfm/connect", {});
}

export async function lastfmComplete(token: string): Promise<LastfmStatus> {
  return apiPost<LastfmStatus>("/api/me/lastfm/complete", { token });
}

export async function lastfmDisconnect(): Promise<void> {
  await apiDelete<unknown>("/api/me/lastfm");
}


/* ---------- ListenBrainz per-user linking ---------- */
//
// No OAuth dance here (unlike Last.fm): the user pastes a personal token
// from https://listenbrainz.org/settings/, we validate it server-side, and
// store it. The same token then drives scrobbling and playlist import.

export interface ListenBrainzStatus {
  linked: boolean;
  username?: string;
}

// One of the recommendation playlists ListenBrainz generates for the user
// (Weekly Jams, Weekly Exploration, Daily Jams, …).
export interface ListenBrainzPlaylist {
  mbid: string;
  title: string;
  description: string;
  track_count: number | null;
}

// Result of importing one playlist: how many of its tracks we could match
// to the local library, plus the labels we couldn't find.
export interface ListenBrainzImportResult {
  playlist_id: number;
  name: string;
  matched: number;
  total: number;
  unmatched: string[];
}

export async function getListenBrainzStatus(): Promise<ListenBrainzStatus> {
  return apiGet<ListenBrainzStatus>("/api/me/listenbrainz");
}

export async function listenBrainzConnect(token: string): Promise<ListenBrainzStatus> {
  return apiPost<ListenBrainzStatus>("/api/me/listenbrainz/connect", { token });
}

export async function listenBrainzDisconnect(): Promise<void> {
  await apiDelete<unknown>("/api/me/listenbrainz");
}

export async function getListenBrainzPlaylists(): Promise<ListenBrainzPlaylist[]> {
  const res = await apiGet<{ playlists: ListenBrainzPlaylist[] }>(
    "/api/me/listenbrainz/playlists",
  );
  return res.playlists;
}

export async function importListenBrainzPlaylist(
  playlist_mbid: string,
  name?: string,
): Promise<ListenBrainzImportResult> {
  return apiPost<ListenBrainzImportResult>(
    "/api/me/listenbrainz/playlists/import",
    { playlist_mbid, name },
  );
}


/* ---------- User management ---------- */

export interface MeInfo {
  sub: string;
  username: string;
  is_admin: boolean;
  iat: number;
  exp: number;
  created_at: number | null;
  password_changed_at: number | null;
}

export async function getMe(): Promise<MeInfo> {
  return apiGet<MeInfo>("/api/me");
}

export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  await apiPost<{ updated: boolean }>("/api/me/password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}

export interface UserRecord {
  id: number;
  username: string;
  is_admin: boolean;
  disabled: boolean;
  created_at: number;
  password_changed_at: number | null;
}

export async function listUsers(): Promise<UserRecord[]> {
  return apiGet<UserRecord[]>("/api/users");
}

export async function createUser(
  username: string,
  password: string,
  is_admin: boolean,
): Promise<UserRecord> {
  return apiPost<UserRecord>("/api/users", { username, password, is_admin });
}

export async function patchUser(
  id: number,
  patch: { is_admin?: boolean; password?: string; disabled?: boolean },
): Promise<UserRecord> {
  return apiPatch<UserRecord>(`/api/users/${id}`, patch);
}

export async function deleteUser(id: number): Promise<void> {
  await apiDelete<unknown>(`/api/users/${id}`);
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
  /**
   * Tracks where this artist is credited but the album-artist is someone
   * else — compilations, soundtracks, guest features. Lets the artist
   * page show contributions for artists who never appear as album-artist.
   */
  appearances: SubsonicSong[];
  bio: {
    summary: string;
    content: string;
    url: string | null;
    image_url: string | null;
    tags: string[];
  } | null;
  /**
   * Best available artist photo for cards / hero. Prefers our local cache
   * (served via getCoverArt with one-year immutable cache headers), then
   * a live Deezer CDN URL as a fallback, then bio.image_url. When set to
   * a getCoverArt URL the path has no auth params — pair with
   * `image_cover_art_id` and `coverArtUrl()` to get a fully-authenticated
   * URL the browser will cache.
   */
  image_url: string | null;
  /** Hash id of the locally-cached artist photo, or null if not yet cached. */
  image_cover_art_id: string | null;
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
