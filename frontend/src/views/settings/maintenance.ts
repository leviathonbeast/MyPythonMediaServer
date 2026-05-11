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

import { runGc, runVacuum, type GcResult } from "../../api";
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

  return {
    // No auto-refresh: results are shown on demand when the user clicks.
    refresh: async () => { /* noop */ },
    cleanup: () => { /* listeners die with the DOM */ },
  };
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
