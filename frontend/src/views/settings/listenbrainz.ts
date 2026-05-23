// src/views/settings/listenbrainz.ts
//
// ListenBrainz — per-user account linking, scrobbling, and importing the
// recommendation playlists ListenBrainz generates ("created for you").
//
// Why this is simpler than the Last.fm section:
//   Last.fm needs a redirect handshake (connect → approve on last.fm →
//   complete), so views/settings/lastfm.ts juggles sessionStorage and a
//   return trip. ListenBrainz uses a single personal token the user pastes
//   from https://listenbrainz.org/settings/ — we just POST it once. No
//   redirect, no sessionStorage, no in-flight race to guard against.

import {
  getListenBrainzStatus, listenBrainzConnect, listenBrainzDisconnect,
  getListenBrainzPlaylists, importListenBrainzPlaylist,
  type ListenBrainzStatus, type ListenBrainzPlaylist,
} from "../../api";
import { escapeHtml } from "../_util";

// Where the user finds their token — surfaced in the connect prompt so they
// don't have to go hunting for it.
const TOKEN_HELP_URL = "https://listenbrainz.org/settings/";

export interface ListenBrainzSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderListenBrainzSection(
  host: HTMLElement,
): Promise<ListenBrainzSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>ListenBrainz</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-listenbrainz>
      <div class="loading">Loading</div>
    </div>
  `);

  const panel = host.querySelector<HTMLElement>("[data-listenbrainz]")!;

  const refresh = async () => {
    let status: ListenBrainzStatus;
    try {
      status = await getListenBrainzStatus();
    } catch (e) {
      panel.classList.remove("loading");
      panel.innerHTML =
        `<div class="empty">Couldn't load ListenBrainz status: ${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    panel.classList.remove("loading");
    panel.innerHTML = renderHtml(status);
    wireEvents(status);
  };

  const renderHtml = (s: ListenBrainzStatus): string => {
    if (!s.linked) {
      // Token entry. The token is secret-ish, so use a password field; the
      // help link opens ListenBrainz settings in a new tab.
      return `
        <div style="margin-bottom:1rem;color:var(--muted)">
          Not connected. Paste your ListenBrainz <strong>user token</strong> to
          scrobble plays and import your generated playlists.
        </div>
        <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
          <input type="password" data-lb-token placeholder="ListenBrainz user token"
                 autocomplete="off" spellcheck="false"
                 style="flex:1;min-width:16rem;padding:.5rem .6rem;background:transparent;border:1px solid var(--rule);color:var(--ink);font-family:var(--font-mono)" />
          <button class="btn primary" data-lb-connect>Connect</button>
        </div>
        <div style="font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted);margin-top:.5rem">
          Find your token on your
          <a href="${TOKEN_HELP_URL}" target="_blank" rel="noopener noreferrer">ListenBrainz settings page</a>.
        </div>
      `;
    }

    // Linked: show identity + disconnect, then the generated-playlists
    // importer (loaded lazily on demand so we don't hit ListenBrainz on
    // every settings-page open).
    return `
      <div style="margin-bottom:1rem">
        Linked as <strong>${escapeHtml(s.username ?? "")}</strong>
      </div>
      <button class="btn ghost" data-lb-disconnect>Disconnect</button>
      <hr style="margin:1.5rem 0;border:none;border-top:1px solid var(--rule)" />
      <div style="display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap">
        <div style="font-family:var(--font-display)">Generated playlists</div>
        <button class="btn ghost" data-lb-load-playlists>Load playlists</button>
      </div>
      <div style="font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted);margin:.5rem 0">
        Import ListenBrainz's recommendation playlists (Weekly Jams, Weekly
        Exploration, …) as local playlists. Only tracks already in your library
        are added.
      </div>
      <div data-lb-playlists></div>
    `;
  };

  const wireEvents = (s: ListenBrainzStatus) => {
    // --- Connect (paste token) ---
    const connectBtn = panel.querySelector<HTMLButtonElement>("[data-lb-connect]");
    const tokenInput = panel.querySelector<HTMLInputElement>("[data-lb-token]");
    if (connectBtn && tokenInput) {
      const doConnect = async () => {
        const token = tokenInput.value.trim();
        if (!token) { tokenInput.focus(); return; }
        connectBtn.disabled = true;
        connectBtn.textContent = "Connecting…";
        try {
          await listenBrainzConnect(token);
        } catch (e) {
          connectBtn.disabled = false;
          connectBtn.textContent = "Connect";
          window.alert(`Couldn't link ListenBrainz: ${(e as Error).message}`);
          return;
        }
        await refresh();
      };
      connectBtn.addEventListener("click", doConnect);
      // Enter in the token field submits, matching native form ergonomics.
      tokenInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); void doConnect(); }
      });
    }

    // --- Disconnect ---
    panel.querySelector<HTMLButtonElement>("[data-lb-disconnect]")
      ?.addEventListener("click", async () => {
        if (!window.confirm("Disconnect your ListenBrainz account from Muse?")) return;
        try {
          await listenBrainzDisconnect();
        } catch (e) {
          window.alert(`Couldn't disconnect: ${(e as Error).message}`);
          return;
        }
        await refresh();
      });

    // --- Load + import generated playlists (linked only) ---
    if (s.linked) {
      const loadBtn = panel.querySelector<HTMLButtonElement>("[data-lb-load-playlists]");
      const list = panel.querySelector<HTMLElement>("[data-lb-playlists]");
      loadBtn?.addEventListener("click", async () => {
        if (!list) return;
        loadBtn.disabled = true;
        loadBtn.textContent = "Loading…";
        list.innerHTML = `<div class="loading">Loading</div>`;
        let playlists: ListenBrainzPlaylist[];
        try {
          playlists = await getListenBrainzPlaylists();
        } catch (e) {
          list.innerHTML =
            `<div class="empty">Couldn't load playlists: ${escapeHtml((e as Error).message)}</div>`;
          loadBtn.disabled = false;
          loadBtn.textContent = "Reload playlists";
          return;
        }
        loadBtn.disabled = false;
        loadBtn.textContent = "Reload playlists";
        renderPlaylistList(list, playlists);
      });
    }
  };

  // Render the fetched playlists with a per-row Import button.
  const renderPlaylistList = (
    list: HTMLElement,
    playlists: ListenBrainzPlaylist[],
  ) => {
    if (playlists.length === 0) {
      list.innerHTML =
        `<div class="empty">No generated playlists found for your account yet.</div>`;
      return;
    }
    list.innerHTML = playlists.map((p) => `
      <div class="lb-playlist-row" data-mbid="${escapeHtml(p.mbid)}"
           style="display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.6rem 0;border-top:1px solid var(--rule)">
        <div style="min-width:0">
          <div style="font-family:var(--font-display);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.title)}</div>
          ${p.description
            ? `<div style="font-size:var(--t-micro);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.description)}</div>`
            : ""}
        </div>
        <div style="display:flex;align-items:center;gap:.6rem;flex-shrink:0">
          <span class="lb-import-status" style="font-size:var(--t-micro);color:var(--muted)"></span>
          <button class="btn ghost" data-lb-import>Import</button>
        </div>
      </div>
    `).join("");

    list.querySelectorAll<HTMLButtonElement>("[data-lb-import]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const row = btn.closest<HTMLElement>(".lb-playlist-row");
        const mbid = row?.dataset.mbid;
        const statusEl = row?.querySelector<HTMLElement>(".lb-import-status");
        if (!mbid) return;
        btn.disabled = true;
        btn.textContent = "Importing…";
        try {
          const res = await importListenBrainzPlaylist(mbid);
          btn.textContent = "Imported";
          if (statusEl) {
            statusEl.textContent = `${res.matched}/${res.total} tracks`;
            statusEl.style.color = "var(--ink)";
          }
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Import";
          if (statusEl) {
            statusEl.textContent = (e as Error).message;
            statusEl.style.color = "var(--danger, #c0392b)";
          }
        }
      });
    });
  };

  await refresh();

  // No timers or subscriptions held — cleanup is a no-op, kept for shape
  // parity with the other settings sections.
  return { refresh, cleanup: () => {} };
}
