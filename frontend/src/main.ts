// src/main.ts
//
// Entry point.
//
// Responsibilities:
//   1. Decide whether to render the auth flow (login) or the main shell.
//   2. Run a tiny hash-based router for the main shell so the page never
//      reloads while browsing — back/forward still work because each
//      navigation is a real history entry via `location.hash = ...`.
//   3. Mount the persistent player dock once and keep it mounted across
//      view changes. (Reconstructing the <audio> element on every
//      navigation would cut off playback, which is unacceptable.)
//
// We keep this file intentionally short — most logic lives in the views.

import { isAuthenticated, signOut, authState } from "./auth";
import { mountPlayerDock } from "./player";
import { renderLogin } from "./views/login";
import { renderLibrary } from "./views/library";
import { renderAlbums } from "./views/albums";
import { renderAlbum } from "./views/album";
import { renderArtist } from "./views/artist";
import { renderSearch } from "./views/search";
import { renderSettings } from "./views/settings";
import { renderTrack} from "./views/track";
import { renderProfile } from "./views/profile";
import { renderAdmin } from "./views/admin";
import { renderPlaylists } from "./views/playlists";
import { renderPlaylist } from "./views/playlist";
import { renderGenre } from "./views/genre";

const root = document.getElementById("app")!;

// ─── shell DOM ───────────────────────────────────────────────────────
// The shell is built once on first authenticated render and then re-used.
let shell: {
  el: HTMLElement;
  main: HTMLElement;
  player: HTMLElement;
  navLinks: HTMLAnchorElement[];
  detachPlayer: () => void;
} | null = null;

function buildShell(): NonNullable<typeof shell> {
  const el = document.createElement("div");
  el.className = "shell";
  el.innerHTML = `
    <aside class="sidebar">
      <div class="brand">
        <span class="mark">Muse.</span>
        <span class="tag">vol. 01</span>
      </div>

      <nav class="nav">
        <a href="#/"          data-route="/"        ><span>Albums</span><span class="num">A·01</span></a>
        <a href="#/library"   data-route="/library" ><span>Library</span><span class="num">A·02</span></a>
        <a href="#/playlists" data-route="/playlist"><span>Playlists</span><span class="num">A·03</span></a>
        <a href="#/search"    data-route="/search"  ><span>Search</span><span class="num">A·04</span></a>
        <a href="#/settings"  data-route="/settings"><span>Settings</span><span class="num">A·05</span></a>
        <a href="#/profile"   data-route="/profile" ><span>Profile</span><span class="num">A·06</span></a>
        <a href="#/admin"     data-route="/admin"   style="display:none" data-admin-link><span>Admin</span><span class="num">A·07</span></a>
      </nav>

      <div class="meta">
        Signed in as <strong><a href="#/profile" style="color:inherit;text-decoration:none" data-username>—</a></strong><br/>
        <button class="signout" data-signout>Sign out</button>
      </div>
    </aside>

    <main class="main" data-main>
      <div class="loading">Loading</div>
    </main>

    <footer class="player" data-player></footer>
  `;
  const main = el.querySelector<HTMLElement>("[data-main]")!;
  const playerEl = el.querySelector<HTMLElement>("[data-player]")!;
  const navLinks = Array.from(el.querySelectorAll<HTMLAnchorElement>(".nav a"));

  // Username + signout + conditional admin link
  const { username: u, is_admin } = authState();
  el.querySelector<HTMLElement>("[data-username]")!.textContent = u ?? "—";
  el.querySelector<HTMLButtonElement>("[data-signout]")?.addEventListener("click", signOut);
  if (is_admin) {
    (el.querySelector<HTMLElement>("[data-admin-link]") as HTMLElement).style.display = "";
  }

  const detachPlayer = mountPlayerDock(playerEl);

  return { el, main, player: playerEl, navLinks, detachPlayer };
}

// ─── routing ─────────────────────────────────────────────────────────

interface Route {
  match: RegExp;
  handle: (host: HTMLElement, params: string[]) => void | Promise<void>;
}

// Order matters: most specific first.
const routes: Route[] = [
  { match: /^\/track\/(.+)$/,    handle: (h, p) => renderTrack(h, decodeURIComponent(p[0]))},
  { match: /^\/album\/(.+)$/,    handle: (h, p) => renderAlbum(h, decodeURIComponent(p[0])) },
  { match: /^\/artist\/(.+)$/,   handle: (h, p) => renderArtist(h, decodeURIComponent(p[0])) },
  { match: /^\/playlist\/(.+)$/, handle: (h, p) => renderPlaylist(h, decodeURIComponent(p[0])) },
  { match: /^\/playlists$/,      handle: (h)    => renderPlaylists(h) },
  { match: /^\/genre\/(.+)$/,    handle: (h, p) => renderGenre(h, decodeURIComponent(p[0])) },
  { match: /^\/library$/,        handle: (h)    => renderLibrary(h) },
  { match: /^\/search(?:\/(.+))?$/, handle: (h, p) => renderSearch(h, p[0] ? decodeURIComponent(p[0]) : undefined) },
  { match: /^\/settings$/,       handle: (h)    => renderSettings(h) },
  { match: /^\/profile$/,        handle: (h)    => renderProfile(h) },
  { match: /^\/admin$/,          handle: (h)    => renderAdmin(h) },
  { match: /^\/?$/,              handle: (h)    => renderAlbums(h) },
];

function currentPath(): string {
  const hash = location.hash || "#/";
  return hash.startsWith("#") ? hash.slice(1) : hash;
}

function setActive(navLinks: HTMLAnchorElement[]): void {
  const path = currentPath();
  for (const a of navLinks) {
    const route = a.dataset.route ?? "/";
    // The Albums page is the root; the "Library" page is /library, etc.
    // We mark active on prefix match but only the most specific link.
    let active = false;
    if (route === "/" && (path === "/" || path === "")) active = true;
    else if (route !== "/" && path.startsWith(route)) active = true;
    a.classList.toggle("active", active);
  }
}

async function navigate(): Promise<void> {
  if (!isAuthenticated() || currentPath() === "/login") {
    // Tear down shell if needed and show login.
    if (shell) {
      shell.detachPlayer();
      shell = null;
    }
    root.innerHTML = "";
    renderLogin(root);
    return;
  }

  // Build shell on first authenticated navigation.
  if (!shell) {
    shell = buildShell();
    root.innerHTML = "";
    root.appendChild(shell.el);
  }

  // Run any pending cleanup the previous view registered (e.g. unsubscribe
  // from player events, stop polling).
  const prev = (shell.main as any).__cleanup;
  if (typeof prev === "function") {
    try { prev(); } catch { /* ignore */ }
    (shell.main as any).__cleanup = undefined;
  }

  setActive(shell.navLinks);

  // Find a matching route. If none, fall back to root.
  const path = currentPath();
  for (const r of routes) {
    const m = path.match(r.match);
    if (m) {
      shell.main.scrollTo?.(0, 0);
      window.scrollTo?.(0, 0);
      await r.handle(shell.main, m.slice(1));
      return;
    }
  }
  // 404 → home
  location.hash = "#/";
}

// Last.fm OAuth-return interception. Last.fm strips URL fragments from
// its cb param, so even when we ask for `…/web/#/settings` it redirects
// to `…/web/?token=…` at the root. If a pending Last.fm token is in
// sessionStorage when the SPA boots, force the user to the settings
// page — the lastfm section's refresh() then completes the exchange.
// Without this, the SPA would land on the default route and the
// pending token would sit unused until the user manually navigated.
if (sessionStorage.getItem("muse.lastfm.pending-token") &&
    !location.hash.startsWith("#/settings")) {
  location.hash = "#/settings";
}

window.addEventListener("hashchange", () => { void navigate(); });
window.addEventListener("DOMContentLoaded", () => { void navigate(); });
// In case the script ran after DOMContentLoaded (Vite HMR / ESM):
if (document.readyState !== "loading") void navigate();
