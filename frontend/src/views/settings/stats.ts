// src/views/settings/stats.ts
//
// Stats section — visible to every user.
//
// Displays the high-level library counters (artists, albums, tracks, total
// runtime). Read-only: no buttons, no listeners.
//
// Other sections call our refresh() when something they did invalidates
// the numbers we're showing — e.g. a scan finished or a GC removed empty
// albums. We don't poll on a timer; refresh is event-driven.

import { libraryStats } from "../../api";
import { fmtDuration } from "../../player";
import { escapeHtml } from "../_util";

export interface StatsSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderStatsSection(host: HTMLElement): Promise<StatsSection> {
  // Append this section's DOM directly into `host`. Using
  // insertAdjacentHTML (rather than wrapping in an extra <div>) keeps
  // the DOM flat — every section's `section-head` and `panel` are
  // direct children of host, matching the page's original layout.
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Stats</h2>
      <span class="rule"></span>
    </div>
    <div data-stats class="loading">Loading stats</div>
  `);

  // Keep a reference to the panel so refresh() can repaint just that
  // node without disturbing the rest of the page.
  const panel = host.querySelector<HTMLElement>("[data-stats]")!;

  const refresh = async () => {
    try {
      const s = await libraryStats();
      panel.classList.remove("loading");
      panel.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1.5rem">
          ${statBlock("Artists",        String(s.artists))}
          ${statBlock("Albums",         String(s.albums))}
          ${statBlock("Tracks",         String(s.tracks))}
          ${statBlock("Total runtime",  fmtDuration(s.total_duration_seconds))}
        </div>
      `;
    } catch (e) {
      panel.classList.remove("loading");
      panel.innerHTML = `<div class="empty">Stats unavailable: ${escapeHtml((e as Error).message)}</div>`;
    }
  };

  await refresh();

  return {
    refresh,
    // No listeners or timers to dispose — the DOM itself will be detached
    // by the router on view-unmount, taking everything with it.
    cleanup: () => { /* noop */ },
  };
}

function statBlock(label: string, value: string): string {
  return `
    <div>
      <div class="label">— ${escapeHtml(label)}</div>
      <div class="folio" style="font-size:clamp(2.5rem,5vw,4rem)">${escapeHtml(value)}</div>
    </div>
  `;
}
