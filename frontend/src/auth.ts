// src/auth.ts
//
// Authentication state for the SPA.
//
// We keep three pieces of state in localStorage:
//   - jwt token (used for /api/* internal endpoints)
//   - username  (used as `u=` for Subsonic calls)
//   - password  (used as `p=` for Subsonic calls)
//
// WHY persist the password client-side:
//   The Subsonic protocol authenticates EVERY call with the user's password
//   (either as `p=` in plaintext, or as `t=md5(password+salt) & s=salt`).
//   The server can't authenticate Subsonic calls from a JWT alone — it
//   needs the password to verify against its bcrypt hash. After a fresh
//   /api/auth/login the backend caches the plaintext in memory, but if
//   the user reloads the SPA (clearing the backend's per-process cache
//   from their POV is irrelevant; their need to re-auth is immediate)
//   the only way to keep working without forcing another login is to
//   also persist the password client-side and re-send it on each call.
//
//   This is a known property of Subsonic-compatible servers (Navidrome,
//   Airsonic, etc. all face the same issue). Mitigations: (a) only run
//   over HTTPS, (b) prefer the token+salt scheme on untrusted networks,
//   (c) treat localStorage as roughly equivalent to a session cookie in
//   threat-model terms.

const KEY_TOKEN = "muse.jwt";
const KEY_USER  = "muse.username";
const KEY_PASS  = "muse.password";
const KEY_ADMIN = "muse.is_admin";

export interface AuthState {
  token: string | null;
  username: string | null;
  password: string | null;
  is_admin: boolean;
}

export function authState(): AuthState {
  return {
    token:    localStorage.getItem(KEY_TOKEN),
    username: localStorage.getItem(KEY_USER),
    password: localStorage.getItem(KEY_PASS),
    is_admin: localStorage.getItem(KEY_ADMIN) === "1",
  };
}

export function isAuthenticated(): boolean {
  const s = authState();
  return Boolean(s.token && s.username && s.password);
}

export async function login(username: string, password: string): Promise<void> {
  // We hit the internal /api/auth/login endpoint which returns a JWT and
  // also (server-side) caches the plaintext password so subsequent Subsonic
  // calls authenticate without an extra round-trip.
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let detail = "Invalid credentials";
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch { /* swallow */ }
    throw new Error(detail);
  }
  const data = (await res.json()) as { token: string; is_admin: boolean };
  localStorage.setItem(KEY_TOKEN, data.token);
  localStorage.setItem(KEY_USER, username);
  localStorage.setItem(KEY_PASS, password);
  localStorage.setItem(KEY_ADMIN, data.is_admin ? "1" : "0");
}

export function updateStoredPassword(newPassword: string): void {
  localStorage.setItem(KEY_PASS, newPassword);
}

export function signOut(): void {
  localStorage.removeItem(KEY_TOKEN);
  localStorage.removeItem(KEY_USER);
  localStorage.removeItem(KEY_PASS);
  localStorage.removeItem(KEY_ADMIN);
  // Force a hard reload to clear any in-memory state held by views/player.
  // We only do this if we're not already on the login page to avoid loops.
  if (location.hash !== "#/login") {
    location.hash = "#/login";
    location.reload();
  }
}
