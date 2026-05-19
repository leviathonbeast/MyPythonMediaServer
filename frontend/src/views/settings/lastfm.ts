// src/views/settings/lastfm.ts
//
// Last.fm scrobbling — per-user account linking.
//
// Flow:
//   1. User clicks Connect → server returns { auth_url, token }
//   2. We stash the token in sessionStorage and redirect to last.fm
//      (appending &cb=<this-page> so last.fm sends the user back here).
//   3. User approves on last.fm, then last.fm redirects to our cb URL
//   4. The section runs refresh() on mount, sees the pending token in
//      sessionStorage, exchanges it for a session via /complete, and
//      then the status flips to "Linked as <name>".
//
// Why sessionStorage rather than reading ?token= from the URL:
//   We use hash-based routing, so a cb like ".../web/#/settings" would
//   end up with last.fm appending ?token=… AFTER the hash, which is
//   awkward to parse and exposes the token in the URL bar.
//   sessionStorage survives a cross-origin redirect within the same
//   tab and keeps the token out of the address bar entirely.

import {
  getLastfmStatus, lastfmConnect, lastfmComplete, lastfmDisconnect,
  type LastfmStatus,
} from "../../api";
import { escapeHtml } from "../_util";

// Key under which we stash the in-flight token. Namespaced so it doesn't
// collide with anything else the SPA might add later.
const PENDING_TOKEN_KEY = "muse.lastfm.pending-token";

export interface LastfmSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderLastfmSection(host: HTMLElement): Promise<LastfmSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Last.fm scrobbling</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-lastfm>
      <div class="loading">Loading</div>
    </div>
  `);

  const panel = host.querySelector<HTMLElement>("[data-lastfm]")!;

  const refresh = async () => {
    // If we just returned from last.fm approval, finish the exchange
    // before we ask the server for the current status — otherwise
    // we'd render "Not connected" and then immediately re-render.
    const pending = sessionStorage.getItem(PENDING_TOKEN_KEY);
    if (pending) {
      sessionStorage.removeItem(PENDING_TOKEN_KEY);
      try {
        await lastfmComplete(pending);
      } catch (e) {
        panel.classList.remove("loading");
        panel.innerHTML =
          `<div class="empty">Last.fm linking failed: ${escapeHtml((e as Error).message)}</div>`;
        return;
      }
      // last.fm dumped its `?token=…` into the URL bar; tidy it.
      // history.replaceState keeps the SPA on the same hash route
      // (settings) without triggering a full reload.
      try {
        history.replaceState(null, "", "#/settings");
      } catch { /* not fatal if the browser is grumpy */ }
    }

    let status: LastfmStatus;
    try {
      status = await getLastfmStatus();
    } catch (e) {
      panel.classList.remove("loading");
      panel.innerHTML =
        `<div class="empty">Couldn't load Last.fm status: ${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    panel.classList.remove("loading");
    panel.innerHTML = renderHtml(status);
    wireEvents();
  };

  const renderHtml = (s: LastfmStatus): string => {
    if (s.linked) {
      return `
        <div style="margin-bottom:1rem">
          Linked as <strong>${escapeHtml(s.username ?? "")}</strong>
        </div>
        <button class="btn ghost" data-disconnect>Disconnect</button>
      `;
    }
    return `
      <div style="margin-bottom:1rem;color:var(--muted)">
        Not connected. Link your Last.fm account to scrobble plays automatically.
      </div>
      <button class="btn primary" data-connect>Connect Last.fm</button>
    `;
  };

  const wireEvents = () => {
    panel.querySelector<HTMLButtonElement>("[data-connect]")
      ?.addEventListener("click", async () => {
        try {
          const { auth_url, token } = await lastfmConnect();
          sessionStorage.setItem(PENDING_TOKEN_KEY, token);
          // Where last.fm should send the user after approval. The
          // section's refresh() picks up the pending token from
          // sessionStorage on mount and finishes the flow.
          const cb = `${window.location.origin}/web/#/settings`;
          window.location.href = `${auth_url}&cb=${encodeURIComponent(cb)}`;
        } catch (e) {
          window.alert(`Couldn't start Last.fm flow: ${(e as Error).message}`);
        }
      });

    panel.querySelector<HTMLButtonElement>("[data-disconnect]")
      ?.addEventListener("click", async () => {
        if (!window.confirm("Disconnect your Last.fm account from Muse?")) return;
        try {
          await lastfmDisconnect();
        } catch (e) {
          window.alert(`Couldn't disconnect: ${(e as Error).message}`);
          return;
        }
        await refresh();
      });
  };

  await refresh();

  // No subscriptions or timers held, so cleanup is a no-op. Kept for
  // shape-parity with the other sections so the composer's aggregation
  // works uniformly.
  return { refresh, cleanup: () => {} };
}
