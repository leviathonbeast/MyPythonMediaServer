// src/views/settings/scan.ts
//
// Scan section — admin-only.
//
// Triggers and cancels library scans, polling /api/scan every 3 seconds
// while one is running so the progress display stays live. Polling pauses
// when the tab is hidden (no point sending requests no one's looking at)
// and stops entirely when the scan finishes.
//
// When a scan finishes, library counters change. We notify the composer
// via `ctx.onLibraryChanged` so stats and folder track-counts refresh.

import {
  getScanProgress, startScan, cancelScan,
  type ScanProgress,
} from "../../api";
import { escapeHtml } from "../_util";

// 1.5s was too aggressive — at ~40 req/min over a 30-minute scan that's
// 1200 access-log lines. 3s feels just as live and is gentler on logs.
const SCAN_POLL_INTERVAL_MS = 3000;

export interface ScanCtx {
  onLibraryChanged?: () => void;
}

export interface ScanSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderScanSection(
  host: HTMLElement,
  ctx: ScanCtx = {},
): Promise<ScanSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Scan</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-scan>
      <div data-scanstate>—</div>
      <div style="margin-top:1rem;display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn primary" data-rescan>▶ Start a fresh scan</button>
        <button class="btn ghost"  data-cancelscan style="display:none">✕ Cancel scan</button>
      </div>
    </div>
  `);

  // We grab references to the static elements once and just update their
  // text/content during refresh — the buttons themselves never get
  // re-rendered, which means their click handlers (bound below) survive.
  const stateEl   = host.querySelector<HTMLElement>("[data-scanstate]")!;
  const startBtn  = host.querySelector<HTMLButtonElement>("[data-rescan]")!;
  const cancelBtn = host.querySelector<HTMLButtonElement>("[data-cancelscan]")!;

  // Local state for the poll timer. Stays scoped to this section — the
  // old code put it at module level, which broke if the view was ever
  // mounted twice without unmounting cleanly.
  let pollHandle: number | undefined;

  // Tracks whether the most recent refresh saw a running scan. Used to
  // detect the "was running → done" transition so we fire onLibraryChanged
  // exactly once when a scan finishes (not on every poll after).
  let wasRunning = false;

  const refresh = async () => {
    let progress: ScanProgress;
    try {
      progress = await getScanProgress();
    } catch (e) {
      stateEl.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    stateEl.innerHTML = scanHtml(progress);
    startBtn.disabled = progress.running;
    cancelBtn.style.display = progress.running ? "" : "none";

    if (progress.running) {
      wasRunning = true;
      // Start polling lazily — only if we don't already have a timer and
      // the tab is currently visible.
      if (pollHandle === undefined && !document.hidden) {
        pollHandle = window.setInterval(() => void refresh(), SCAN_POLL_INTERVAL_MS);
      }
    } else if (pollHandle !== undefined) {
      // Scan stopped (finished or cancelled). Tear the timer down.
      window.clearInterval(pollHandle);
      pollHandle = undefined;
    }

    // Edge-trigger: only fire onLibraryChanged on the transition from
    // running → not-running. Otherwise we'd spam stats/folders with
    // refreshes on every poll cycle after the scan completes.
    if (wasRunning && !progress.running) {
      wasRunning = false;
      ctx.onLibraryChanged?.();
    }
  };

  // Button handlers. Bound once; the buttons themselves are never
  // re-rendered, so we don't need to re-bind on each refresh.
  startBtn.addEventListener("click", async () => {
    try {
      const result = await startScan();
      if (!result.started) {
        alert("A scan is already in progress. Wait for it to finish or cancel it first.");
        return;
      }
    } catch (e) {
      alert((e as Error).message);
      return;
    }
    await refresh();
  });

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    try {
      await cancelScan();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      cancelBtn.disabled = false;
    }
    await refresh();
  });

  // Pause polling while the tab is hidden — browsers throttle background
  // timers anyway, but this is cheaper and more predictable.
  const visibilityListener = () => {
    if (document.hidden) {
      if (pollHandle !== undefined) {
        window.clearInterval(pollHandle);
        pollHandle = undefined;
      }
    } else {
      // Coming back into view — fetch once immediately so the UI catches
      // up to whatever happened while hidden.
      void refresh();
    }
  };
  document.addEventListener("visibilitychange", visibilityListener);

  await refresh();

  return {
    refresh,
    cleanup: () => {
      // Stop the poll timer AND remove the visibility listener — neither
      // would die on its own when the view is unmounted.
      if (pollHandle !== undefined) window.clearInterval(pollHandle);
      pollHandle = undefined;
      document.removeEventListener("visibilitychange", visibilityListener);
    },
  };
}

// ─── pure render helper ────────────────────────────────────────────────

function scanHtml(p: ScanProgress): string {
  if (!p.running && !p.started_at) {
    return `<span class="label">— No scan has run yet.</span>`;
  }
  if (p.running) {
    // Two stages of progress:
    //   - Phase 1 (walking): files_to_parse is still 0; show files_seen
    //     ticking up. Percentage is "unknown".
    //   - Phase 2 (parsing+writing): files_to_parse is set; the percentage
    //     reflects parse completion.
    const toParse = p.files_to_parse ?? 0;
    const parsed  = p.files_parsed ?? 0;
    let pct = 0;
    let stage = "Walking";
    if (toParse > 0) {
      stage = "Parsing";
      pct = Math.min(100, Math.round((parsed / toParse) * 100));
    }
    return `
      <span class="label">— ${escapeHtml(stage)}</span>
      <div class="folio" style="font-size:clamp(3rem,8vw,6rem)">${pct}%</div>
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--muted)">
        <span>folder ${(p.folders_done ?? 0) + 1}/${p.folders_total ?? 1}</span>
        <span>${(p.files_seen ?? 0).toLocaleString()} files found</span>
        ${toParse > 0 ? `<span>${parsed.toLocaleString()} / ${toParse.toLocaleString()} parsed</span>` : ""}
        <span>+${(p.files_added ?? 0).toLocaleString()} added</span>
        ${p.files_updated ? `<span>~${(p.files_updated).toLocaleString()} updated</span>` : ""}
        ${p.files_removed ? `<span>−${(p.files_removed).toLocaleString()} removed</span>` : ""}
        ${p.errors        ? `<span style="color:var(--accent)">${p.errors} errors</span>` : ""}
      </div>
      ${p.current_folder ? `<div style="margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.current_folder)}</div>` : ""}
    `;
  }
  // Finished view — last completed scan stats.
  const finished = p.finished_at ? new Date(p.finished_at * 1000).toLocaleString() : "—";
  return `
    <span class="label">— Last scan completed</span>
    <div style="margin-top:.5rem;font-family:var(--font-display);font-size:1.25rem">${escapeHtml(finished)}</div>
    <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--muted)">
      <span>${(p.files_seen ?? 0).toLocaleString()} files seen</span>
      <span>+${(p.files_added ?? 0).toLocaleString()} added</span>
      <span>~${(p.files_updated ?? 0).toLocaleString()} updated</span>
      <span>−${(p.files_removed ?? 0).toLocaleString()} removed</span>
      ${p.errors ? `<span style="color:var(--accent)">${p.errors} errors</span>` : ""}
    </div>
  `;
}
