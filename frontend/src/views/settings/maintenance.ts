// src/views/settings/maintenance.ts
//
// Maintenance section — admin-only.
//
// Two buttons:
//   - "Tidy up"          → /api/maintenance/gc      (cheap: prune orphans)
//   - "Tidy + vacuum"    → /api/maintenance/vacuum  (slow: rewrites the DB file)
//
// Both invalidate library counters (empty albums get removed, etc.), so
// on success we call `ctx.onLibraryChanged` to refresh the stats /
// folders sections.
//
// Unlike scan, there's no polling — these are one-shot operations that
// either complete or fail; we just disable the buttons while running.

import {
  runGc,
  runVacuum,
  startRecoverArtwork,
  getRecoverArtworkProgress,
  cancelRecoverArtwork,
  type GcResult,
  type RecoverArtworkProgress,
} from "../../api";
import { escapeHtml } from "../_util";

export interface MaintenanceCtx {
  onLibraryChanged?: () => void;
}

export interface MaintenanceSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderMaintenanceSection(
  host: HTMLElement,
  ctx: MaintenanceCtx = {},
): Promise<MaintenanceSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Maintenance</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-maintenance>
      <div style="font-family:var(--font-display);font-style:italic;color:var(--muted);max-width:60ch;line-height:1.5;margin-bottom:1rem">
        Cleans up empty albums, dangling favourites and orphan cover-art files.
        Runs automatically after every scan; this button is for when you want
        to nudge it manually. Vacuum additionally rewrites the database file
        compactly — slow, but reclaims disk after big deletions.
      </div>
      <div data-gcstate></div>
      <div style="margin-top:1rem;display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn"       data-gc>Tidy up</button>
        <button class="btn ghost" data-vacuum>Tidy + vacuum (slow)</button>
      </div>
    </div>

    <div class="section-head" style="margin-top:2rem">
      <h2>Cover art recovery</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-recover>
      <div style="font-family:var(--font-display);font-style:italic;color:var(--muted);max-width:60ch;line-height:1.5;margin-bottom:1rem">
        Re-extracts cover art for every album that doesn't currently have one.
        Reads embedded tags first, then folder.jpg etc, then asks Deezer as a
        last resort. Safe to interrupt; pick up where it left off by running again.
      </div>
      <div data-recoverstate></div>
      <div style="margin-top:1rem;display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn"       data-recover-start>Recover missing artwork</button>
        <button class="btn ghost" data-recover-cancel style="display:none">Cancel</button>
      </div>
    </div>
  `);

  const gcBtn   = host.querySelector<HTMLButtonElement>("[data-gc]")!;
  const vacBtn  = host.querySelector<HTMLButtonElement>("[data-vacuum]")!;
  const gcState = host.querySelector<HTMLElement>("[data-gcstate]")!;

  // Single code path for both buttons — the only differences are which
  // API method to call and the label shown while running.
  const runMaintenance = async (kind: "gc" | "vacuum") => {
    gcBtn.disabled = vacBtn.disabled = true;
    gcState.innerHTML = `<span class="label">— Running ${kind === "vacuum" ? "vacuum" : "tidy-up"}…</span>`;
    try {
      const result: GcResult = kind === "vacuum" ? await runVacuum() : await runGc();
      gcState.innerHTML = gcResultHtml(result);
      // Counters elsewhere on the page are now stale.
      ctx.onLibraryChanged?.();
    } catch (e) {
      gcState.innerHTML = `<span class="label" style="color:var(--accent)">— ${escapeHtml((e as Error).message)}</span>`;
    } finally {
      gcBtn.disabled = vacBtn.disabled = false;
    }
  };

  gcBtn.addEventListener("click", () => void runMaintenance("gc"));
  vacBtn.addEventListener("click", () => {
    // Vacuum holds an exclusive lock — warn before kicking it off so a
    // misclick doesn't freeze the server during peak listening time.
    if (confirm("Vacuum acquires an exclusive lock for several seconds (or longer on big libraries). Continue?")) {
      void runMaintenance("vacuum");
    }
  });

  // ── Cover art recovery ────────────────────────────────────────────────
  // Long-running background task on the server (it walks every album with
  // a NULL cover_art_id, re-extracts art from embedded tags / folder.jpg /
  // Deezer). The endpoint returns immediately; we poll for progress and
  // re-render the section while it runs.
  const recStart  = host.querySelector<HTMLButtonElement>("[data-recover-start]")!;
  const recCancel = host.querySelector<HTMLButtonElement>("[data-recover-cancel]")!;
  const recState  = host.querySelector<HTMLElement>("[data-recoverstate]")!;

  let recoverPoller: number | null = null;

  const renderRecover = (p: RecoverArtworkProgress): void => {
    recState.innerHTML = recoverHtml(p);
    recStart.disabled = p.running;
    recCancel.style.display = p.running ? "" : "none";
    ctx.onLibraryChanged?.();
  };

  const stopPolling = (): void => {
    if (recoverPoller !== null) {
      window.clearInterval(recoverPoller);
      recoverPoller = null;
    }
  };

  const pollOnce = async (): Promise<void> => {
    try {
      const p = await getRecoverArtworkProgress();
      renderRecover(p);
      // Stop the poller once the server reports the task finished.
      if (!p.running) stopPolling();
    } catch (e) {
      recState.innerHTML = `<span class="label" style="color:var(--accent)">— ${escapeHtml((e as Error).message)}</span>`;
      stopPolling();
      recStart.disabled = false;
      recCancel.style.display = "none";
    }
  };

  recStart.addEventListener("click", async () => {
    recStart.disabled = true;
    try {
      const { progress } = await startRecoverArtwork();
      renderRecover(progress);
      // Poll every 2s — long enough not to hammer the API, short enough
      // that the progress bar feels live during a multi-thousand-album run.
      stopPolling();
      recoverPoller = window.setInterval(() => void pollOnce(), 2000);
    } catch (e) {
      recState.innerHTML = `<span class="label" style="color:var(--accent)">— ${escapeHtml((e as Error).message)}</span>`;
      recStart.disabled = false;
    }
  });

  recCancel.addEventListener("click", async () => {
    recCancel.disabled = true;
    try {
      const { progress } = await cancelRecoverArtwork();
      renderRecover(progress);
    } catch (e) {
      recState.innerHTML = `<span class="label" style="color:var(--accent)">— ${escapeHtml((e as Error).message)}</span>`;
    } finally {
      recCancel.disabled = false;
    }
  });

  // Pick up the existing state on first render — if a recovery is already
  // running (page reload, second admin tab) the UI should reflect it.
  void (async () => {
    try {
      const p = await getRecoverArtworkProgress();
      if (p.running || p.albums_total > 0) {
        renderRecover(p);
        if (p.running) {
          stopPolling();
          recoverPoller = window.setInterval(() => void pollOnce(), 2000);
        }
      }
    } catch { /* swallow */ }
  })();

  return {
    refresh: async () => { /* noop */ },
    cleanup: () => { stopPolling(); },
  };
}

function recoverHtml(p: RecoverArtworkProgress): string {
  // Phase-aware progress label so the user knows what's happening when the
  // task spans album cover art and then artist photos.
  const phaseLabel = (() => {
    if (!p.running) {
      if (p.albums_total === 0 && p.artists_total === 0) return "— Idle";
      return `— Last run — ${p.artwork_recovered.toLocaleString()} albums, ${p.artist_images_recovered.toLocaleString()} artists`;
    }
    if (p.phase === "albums") {
      const pct = p.albums_total > 0
        ? Math.floor((p.albums_done / p.albums_total) * 100)
        : 0;
      return `— Albums — ${p.albums_done.toLocaleString()} / ${p.albums_total.toLocaleString()} (${pct}%)`;
    }
    if (p.phase === "artists") {
      const pct = p.artists_total > 0
        ? Math.floor((p.artists_done / p.artists_total) * 100)
        : 0;
      return `— Artists — ${p.artists_done.toLocaleString()} / ${p.artists_total.toLocaleString()} (${pct}%)`;
    }
    return `— Running`;
  })();
  return `
    <span class="label">${escapeHtml(phaseLabel)}</span>
    <table style="margin-top:.5rem;border-collapse:collapse;font-family:var(--font-mono);font-size:var(--t-small);width:100%;max-width:32rem">
      ${[
        ["Albums processed",       `${p.albums_done.toLocaleString()} / ${p.albums_total.toLocaleString()}`],
        ["Cover art recovered",    p.artwork_recovered.toLocaleString()],
        ["…via Deezer fallback",   p.recovered_via_deezer.toLocaleString()],
        ["Artists processed",      `${p.artists_done.toLocaleString()} / ${p.artists_total.toLocaleString()}`],
        ["Artist images recovered", p.artist_images_recovered.toLocaleString()],
        ["Errors",                 p.errors.toLocaleString()],
      ].map(([k, v]) => `
        <tr>
          <td style="color:var(--muted);text-transform:uppercase;letter-spacing:.1em;font-size:var(--t-micro);padding:.3rem 1rem .3rem 0">${escapeHtml(k)}</td>
          <td style="font-variant-numeric:tabular-nums">${escapeHtml(v)}</td>
        </tr>
      `).join("")}
    </table>
  `;
}

// ─── pure render helpers ───────────────────────────────────────────────

function gcResultHtml(r: GcResult): string {
  const dbDelta = r.db_size_before_bytes - r.db_size_after_bytes;
  // Two-column table of (label, value). Built as a tuple list so adding
  // a new metric is a one-liner.
  const lines: Array<[string, string]> = [
    ["Empty albums removed",   r.empty_albums_removed.toLocaleString()],
    ["Empty artists removed",  r.empty_artists_removed.toLocaleString()],
    ["Dangling favourites",    r.dangling_starred_removed.toLocaleString()],
    ["Orphan artwork files",   r.orphan_artwork_files_removed.toLocaleString()],
    ["Artwork bytes freed",    fmtBytes(r.orphan_artwork_bytes_freed)],
    ["Stale art references",   r.missing_artwork_refs_cleared.toLocaleString()],
    ["WAL checkpointed",       r.wal_checkpointed ? "yes" : "no"],
    ["Vacuumed",               r.vacuumed ? "yes" : "no"],
    ["Database size",          `${fmtBytes(r.db_size_before_bytes)} → ${fmtBytes(r.db_size_after_bytes)}` +
                                 (dbDelta > 0 ? ` (${fmtBytes(dbDelta)} freed)` : "")],
    ["Took",                   `${r.duration_seconds.toFixed(2)} s`],
  ];
  return `
    <span class="label">— Last run</span>
    <table style="margin-top:.5rem;border-collapse:collapse;font-family:var(--font-mono);font-size:var(--t-small)">
      ${lines.map(([k, v]) => `
        <tr>
          <td style="color:var(--muted);text-transform:uppercase;letter-spacing:.1em;font-size:var(--t-micro);padding:.3rem 1rem .3rem 0">${escapeHtml(k)}</td>
          <td style="font-variant-numeric:tabular-nums">${escapeHtml(v)}</td>
        </tr>
      `).join("")}
    </table>
  `;
}

function fmtBytes(n: number): string {
  if (n < 1024)        return `${n} B`;
  if (n < 1024 ** 2)   return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3)   return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}
