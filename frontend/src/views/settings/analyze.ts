// src/views/settings/analyze.ts
//
// Sonic analysis section — admin-only.
//
// Triggers the librosa feature-extraction pass that populates track_features,
// which backs the sonicSimilarity endpoints (getSonicSimilarTracks /
// findSonicPath). Until this has run, those endpoints return nothing.
//
// Modeled on the Scan section: poll /api/analyze every few seconds while a
// pass is running so progress stays live, pause polling when the tab is
// hidden, and stop when the pass finishes. Analysis is much slower than a
// file scan (~1-5s/track), so this is a deliberately separate, opt-in job.

import {
  getAnalyzeProgress, startAnalyze, cancelAnalyze,
  type AnalyzeProgress,
} from "../../api";
import { escapeHtml } from "../_util";

const ANALYZE_POLL_INTERVAL_MS = 3000;

export interface AnalyzeSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderAnalyzeSection(host: HTMLElement): Promise<AnalyzeSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Sonic analysis</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-analyze>
      <div data-analyzestate>—</div>
      <div style="margin-top:1rem;display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn primary" data-analyze-start>▶ Analyze new tracks</button>
        <button class="btn ghost"  data-analyze-force>⟳ Re-analyze everything</button>
        <button class="btn ghost"  data-analyze-cancel style="display:none">✕ Cancel</button>
      </div>
      <div style="margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted)">
        Extracts an audio fingerprint per track so "sonically similar" search
        works. Slow (~1-5s/track) but runs in the background. "Analyze new
        tracks" only processes tracks not yet analyzed; re-analyze everything
        is for after an upgrade that changes the fingerprint.
      </div>
    </div>
  `);

  const stateEl    = host.querySelector<HTMLElement>("[data-analyzestate]")!;
  const startBtn   = host.querySelector<HTMLButtonElement>("[data-analyze-start]")!;
  const forceBtn   = host.querySelector<HTMLButtonElement>("[data-analyze-force]")!;
  const cancelBtn  = host.querySelector<HTMLButtonElement>("[data-analyze-cancel]")!;

  let pollHandle: number | undefined;

  const refresh = async () => {
    let progress: AnalyzeProgress;
    try {
      progress = await getAnalyzeProgress();
    } catch (e) {
      stateEl.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    stateEl.innerHTML = analyzeHtml(progress);
    startBtn.disabled = progress.running;
    forceBtn.disabled = progress.running;
    cancelBtn.style.display = progress.running ? "" : "none";

    if (progress.running) {
      if (pollHandle === undefined && !document.hidden) {
        pollHandle = window.setInterval(() => void refresh(), ANALYZE_POLL_INTERVAL_MS);
      }
    } else if (pollHandle !== undefined) {
      window.clearInterval(pollHandle);
      pollHandle = undefined;
    }
  };

  // Shared by both start buttons; only the `force` flag (and a confirm on the
  // expensive full pass) differ.
  const run = async (force: boolean) => {
    try {
      const result = await startAnalyze(force);
      if (!result.started) {
        alert("Analysis is already running. Wait for it to finish or cancel it first.");
        return;
      }
    } catch (e) {
      alert((e as Error).message);
      return;
    }
    await refresh();
  };

  startBtn.addEventListener("click", () => void run(false));

  forceBtn.addEventListener("click", () => {
    if (!confirm(
      "Re-analyze every track? This re-reads and fingerprints the whole "
      + "library and can take a long time. Continue?"
    )) return;
    void run(true);
  });

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    try {
      await cancelAnalyze();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      cancelBtn.disabled = false;
    }
    await refresh();
  });

  const visibilityListener = () => {
    if (document.hidden) {
      if (pollHandle !== undefined) {
        window.clearInterval(pollHandle);
        pollHandle = undefined;
      }
    } else {
      void refresh();
    }
  };
  document.addEventListener("visibilitychange", visibilityListener);

  await refresh();

  return {
    refresh,
    cleanup: () => {
      if (pollHandle !== undefined) window.clearInterval(pollHandle);
      pollHandle = undefined;
      document.removeEventListener("visibilitychange", visibilityListener);
    },
  };
}

// ─── pure render helper ────────────────────────────────────────────────

function analyzeHtml(p: AnalyzeProgress): string {
  if (!p.running && !p.started_at) {
    return `<span class="label">— No analysis has run yet.</span>`;
  }
  if (p.running) {
    const total = p.total ?? 0;
    const done = (p.analyzed ?? 0) + (p.failed ?? 0);
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    return `
      <span class="label">— Analyzing</span>
      <div class="folio" style="font-size:clamp(3rem,8vw,6rem)">${pct}%</div>
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--muted)">
        <span>${(p.analyzed ?? 0).toLocaleString()} / ${total.toLocaleString()} done</span>
        ${p.failed ? `<span style="color:var(--accent)">${p.failed} failed</span>` : ""}
      </div>
      ${p.current ? `<div style="margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.current)}</div>` : ""}
    `;
  }
  const finished = p.finished_at ? new Date(p.finished_at * 1000).toLocaleString() : "—";
  return `
    <span class="label">— Last analysis completed</span>
    <div style="margin-top:.5rem;font-family:var(--font-display);font-size:1.25rem">${escapeHtml(finished)}</div>
    <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--muted)">
      <span>${(p.analyzed ?? 0).toLocaleString()} analyzed</span>
      ${p.failed ? `<span style="color:var(--accent)">${p.failed} failed</span>` : ""}
    </div>
  `;
}
