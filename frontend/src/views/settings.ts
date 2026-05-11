// src/views/settings.ts
//
// Small admin panel: shows library stats and lets the user kick off a
// background scan. Polls scan progress while a scan is running.

import {
  libraryStats, getScanProgress, startScan, cancelScan, runGc, runVacuum,
  listFolders, addFolder, deleteFolder,
  getTranscodingPolicy, getTranscodingPrefs, setTranscodingPrefs,
  type ScanProgress, type GcResult, type MusicFolder,
  type TranscodingPolicy, type TranscodingPrefs, type TranscodingFormat,
} from "../api";
import { authState } from "../auth";
import { fmtDuration } from "../player";
import { escapeHtml } from "./_util";

let pollHandle: number | undefined;
let visibilityListener: (() => void) | undefined;

// How often to poll /api/scan when a scan is running. 1.5s was too
// aggressive — at ~40 requests/minute over a 30-minute scan that's 1200
// access-log lines. 3s feels just as live and is gentler on logs and
// the server's request loop. We additionally pause polling entirely
// when the tab is hidden (document.hidden) so an open-but-inactive
// Workshop tab doesn't cost anything.
const SCAN_POLL_INTERVAL_MS = 3000;

export async function renderSettings(host: HTMLElement): Promise<void> {
  const isAdmin = authState().is_admin;

  // Non-admins see the library stats and their per-browser streaming
  // preferences, but not the folder management / scan / maintenance
  // controls — those are gated server-side anyway, so showing them
  // would only be visual noise that produces 403s on click.
  const adminSections = !isAdmin ? "" : `
    <div class="section-head">
      <h2>Music folders</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-folders>
      <div class="loading">Loading folders</div>
    </div>
  `;

  const adminTrailingSections = !isAdmin ? "" : `
    <div class="section-head">
      <h2>Scan</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-scan>
      <div data-scanstate>—</div>
      <div style="margin-top:1rem;display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn primary" data-rescan>▶ Start a fresh scan</button>
        <button class="btn ghost" data-cancelscan style="display:none">✕ Cancel scan</button>
      </div>
    </div>

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
        <button class="btn" data-gc>Tidy up</button>
        <button class="btn ghost" data-vacuum>Tidy + vacuum (slow)</button>
      </div>
    </div>
  `;

  host.innerHTML = `
    <header class="page-head">
      <h1>The <em>workshop</em></h1>
      <div class="meta">— ${isAdmin ? "Library admin" : "Library stats & playback"}</div>
    </header>

    <div class="section-head">
      <h2>Stats</h2>
      <span class="rule"></span>
    </div>
    <div data-stats class="loading">Loading stats</div>

    ${adminSections}

    <div class="section-head">
      <h2>Streaming</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-streaming>
      <div class="loading">Loading transcoding policy</div>
    </div>

    ${adminTrailingSections}
  `;

  await refreshStats(host);
  await refreshStreaming(host);
  if (isAdmin) {
    await refreshFolders(host);
    await refreshScan(host);
  }

  if (!isAdmin) {
    // Nothing else to wire up — non-admins don't see the scan/maintenance
    // controls so there are no buttons to bind.
    (host as any).__cleanup = () => { /* noop */ };
    return;
  }

  host.querySelector<HTMLButtonElement>("[data-rescan]")?.addEventListener("click", async () => {
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
    await refreshScan(host);
  });

  host.querySelector<HTMLButtonElement>("[data-cancelscan]")?.addEventListener("click", async () => {
    const btn = host.querySelector<HTMLButtonElement>("[data-cancelscan]");
    if (!btn) return;
    btn.disabled = true;
    try {
      await cancelScan();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      btn.disabled = false;
    }
    await refreshScan(host);
  });

  const gcBtn = host.querySelector<HTMLButtonElement>("[data-gc]");
  const vacBtn = host.querySelector<HTMLButtonElement>("[data-vacuum]");
  const gcState = host.querySelector<HTMLElement>("[data-gcstate]");

  const runMaintenance = async (kind: "gc" | "vacuum") => {
    if (!gcBtn || !vacBtn || !gcState) return;
    gcBtn.disabled = vacBtn.disabled = true;
    gcState.innerHTML = `<span class="label">— Running ${kind === "vacuum" ? "vacuum" : "tidy-up"}…</span>`;
    try {
      const r: GcResult = kind === "vacuum" ? await runVacuum() : await runGc();
      gcState.innerHTML = gcResultHtml(r);
      // Stats and DB size may have changed.
      await refreshStats(host);
      await refreshFolders(host);
    } catch (e) {
      gcState.innerHTML = `<span class="label" style="color:var(--accent)">— ${escapeHtml((e as Error).message)}</span>`;
    } finally {
      gcBtn.disabled = vacBtn.disabled = false;
    }
  };

  gcBtn?.addEventListener("click", () => void runMaintenance("gc"));
  vacBtn?.addEventListener("click", () => {
    if (confirm("Vacuum acquires an exclusive lock for several seconds (or longer on big libraries). Continue?")) {
      void runMaintenance("vacuum");
    }
  });

  // Pause polling while the tab is hidden. setInterval keeps firing when
  // the tab is in the background but throttled by the browser; either
  // way, it's pointless to send requests no one will look at.
  visibilityListener = () => {
    if (document.hidden) {
      if (pollHandle !== undefined) {
        window.clearInterval(pollHandle);
        pollHandle = undefined;
      }
    } else {
      // Refresh once immediately, then resume polling if a scan is running.
      void refreshScan(host);
    }
  };
  document.addEventListener("visibilitychange", visibilityListener);

  // Stop polling when this view is unmounted.
  (host as any).__cleanup = () => {
    if (pollHandle !== undefined) window.clearInterval(pollHandle);
    pollHandle = undefined;
    if (visibilityListener) {
      document.removeEventListener("visibilitychange", visibilityListener);
      visibilityListener = undefined;
    }
  };
}

async function refreshFolders(host: HTMLElement): Promise<void> {
  const el = host.querySelector<HTMLElement>("[data-folders]");
  if (!el) return;
  let folders: MusicFolder[];
  try {
    folders = await listFolders();
  } catch (e) {
    el.classList.remove("loading");
    el.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }
  el.classList.remove("loading");
  el.innerHTML = `
    ${folders.length === 0
      ? `<div style="font-family:var(--font-display);font-style:italic;color:var(--muted);margin-bottom:1rem">No music folders yet. Add one below to start scanning.</div>`
      : `
        <table style="width:100%;border-collapse:collapse;margin-bottom:1.5rem">
          <thead>
            <tr>
              <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Name</th>
              <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Path</th>
              <th class="label" style="text-align:right;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Tracks</th>
              <th style="border-bottom:1px solid var(--rule)"></th>
            </tr>
          </thead>
          <tbody>
            ${folders.map(f => `
              <tr>
                <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-display);font-size:1rem;border-bottom:1px solid var(--rule)">${escapeHtml(f.name)}</td>
                <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-mono);font-size:var(--t-small);color:var(--muted);border-bottom:1px solid var(--rule);word-break:break-all">${escapeHtml(f.path)}</td>
                <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-mono);font-size:var(--t-small);text-align:right;font-variant-numeric:tabular-nums;border-bottom:1px solid var(--rule)">${(f.track_count ?? 0).toLocaleString()}</td>
                <td style="padding:.75rem 0;text-align:right;border-bottom:1px solid var(--rule)">
                  <button class="btn ghost" data-del="${f.id}" style="padding:.4rem .8rem;font-size:var(--t-micro)">Remove</button>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `
    }

    <details>
      <summary style="cursor:pointer;font-family:var(--font-mono);font-size:var(--t-small);letter-spacing:.1em;text-transform:uppercase;color:var(--accent)">
        + Add a folder
      </summary>
      <form data-addform style="margin-top:1rem;display:grid;grid-template-columns:1fr 2fr auto;gap:.75rem;align-items:end">
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">Name (optional)</label>
          <input name="name" placeholder="e.g. Albums"
            style="width:100%;background:transparent;border:0;border-bottom:1px solid var(--rule);padding:.4rem 0;font-family:var(--font-mono);color:var(--ink);outline:none" />
        </div>
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">Path</label>
          <input name="path" required placeholder="/mnt/music"
            style="width:100%;background:transparent;border:0;border-bottom:1px solid var(--rule);padding:.4rem 0;font-family:var(--font-mono);color:var(--ink);outline:none" />
        </div>
        <button type="submit" class="btn primary">Add →</button>
      </form>
      <div data-addmsg style="margin-top:.75rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.1em;text-transform:uppercase;min-height:1em"></div>
    </details>
  `;

  // Wire delete buttons
  el.querySelectorAll<HTMLButtonElement>("[data-del]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.del);
      const folder = folders.find(f => f.id === id);
      if (!folder) return;
      const msg = folder.track_count > 0
        ? `Remove "${folder.name}" and all ${folder.track_count.toLocaleString()} of its tracks from the library?\n\n(Files on disk will NOT be deleted.)`
        : `Remove "${folder.name}"?`;
      if (!confirm(msg)) return;
      btn.disabled = true;
      try {
        await deleteFolder(id);
        await refreshFolders(host);
        await refreshStats(host);
      } catch (e) {
        alert((e as Error).message);
        btn.disabled = false;
      }
    });
  });

  // Wire add form
  const form = el.querySelector<HTMLFormElement>("[data-addform]");
  const addmsg = el.querySelector<HTMLElement>("[data-addmsg]");
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!addmsg) return;
    const data = new FormData(form);
    const name = String(data.get("name") ?? "").trim();
    const path = String(data.get("path") ?? "").trim();
    if (!path) return;

    addmsg.style.color = "var(--muted)";
    addmsg.textContent = "Adding…";
    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]");
    if (submit) submit.disabled = true;
    try {
      await addFolder(name, path);
      addmsg.style.color = "var(--accent)";
      addmsg.textContent = "Added. Click 'Start a fresh scan' to populate it.";
      form.reset();
      await refreshFolders(host);
    } catch (ex) {
      addmsg.style.color = "var(--accent)";
      addmsg.textContent = (ex as Error).message;
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

async function refreshStreaming(host: HTMLElement): Promise<void> {
  const el = host.querySelector<HTMLElement>("[data-streaming]");
  if (!el) return;

  let policy: TranscodingPolicy;
  try {
    policy = await getTranscodingPolicy();
  } catch (e) {
    el.classList.remove("loading");
    el.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  const prefs = getTranscodingPrefs();

  // Build the bitrate options from the server's preset list — that way
  // adding a preset on the backend automatically appears in the UI
  // without a corresponding frontend change.
  const bitrateOptions = Array.from(new Set(policy.presets.map(p => p.bitrate)))
    .sort((a, b) => b - a);

  const formatOptions: { value: TranscodingFormat; label: string }[] = [
    { value: "auto", label: `Auto (server default: ${policy.default_format})` },
    { value: "raw",  label: "Original — no transcoding" },
    ...Array.from(new Set(policy.presets.map(p => p.format))).map(f => ({
      value: f as TranscodingFormat,
      label: f.toUpperCase(),
    })),
  ];

  el.classList.remove("loading");
  el.innerHTML = `
    <div style="font-family:var(--font-display);font-style:italic;color:var(--muted);max-width:60ch;line-height:1.5;margin-bottom:1.25rem">
      How tracks are streamed to this browser. The player shows whether the
      current song is original or transcoded. These preferences are saved to
      this browser only — other clients (mobile apps, other browsers) keep
      their own settings.
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1.5rem 2rem;margin-bottom:1.25rem">
      <div>
        <span class="label">— Format</span>
        <select data-tcformat
          style="width:100%;margin-top:.5rem;background:transparent;color:var(--ink);border:1px solid var(--rule);padding:.5rem;font-family:var(--font-mono);font-size:var(--t-small)">
          ${formatOptions.map(o => `
            <option value="${escapeHtml(o.value)}" ${o.value === prefs.format ? "selected" : ""}>${escapeHtml(o.label)}</option>
          `).join("")}
        </select>
      </div>

      <div>
        <span class="label">— Max bitrate</span>
        <select data-tcbitrate
          style="width:100%;margin-top:.5rem;background:transparent;color:var(--ink);border:1px solid var(--rule);padding:.5rem;font-family:var(--font-mono);font-size:var(--t-small)">
            <option value="0" ${!prefs.maxBitRate ? "selected" : ""}>No client cap</option>
            ${bitrateOptions.map(b => `
              <option value="${b}" ${prefs.maxBitRate === b ? "selected" : ""}>${b} kbps</option>
            `).join("")}
        </select>
      </div>
    </div>

    <div style="font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--muted);line-height:1.7">
      <div>— SERVER DEFAULT FORMAT &nbsp; ${escapeHtml(policy.default_format)}</div>
      <div>— SERVER DEFAULT BITRATE &nbsp; ${policy.default_bitrate} kbps</div>
      <div>— SERVER MAX BITRATE &nbsp; ${policy.max_streaming_bitrate ? policy.max_streaming_bitrate + " kbps" : "uncapped"}</div>
      <div>— TRANSCODING ENABLED &nbsp; ${policy.transcoding_enabled ? "yes" : "no (forced raw)"}</div>
    </div>

    <div data-tcsaved style="margin-top:.75rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;color:var(--accent);min-height:1em"></div>
  `;

  const fmtSel = el.querySelector<HTMLSelectElement>("[data-tcformat]")!;
  const brSel  = el.querySelector<HTMLSelectElement>("[data-tcbitrate]")!;
  const saved  = el.querySelector<HTMLElement>("[data-tcsaved]")!;

  const onChange = () => {
    const next: TranscodingPrefs = {
      format: fmtSel.value as TranscodingFormat,
      maxBitRate: Number(brSel.value) > 0 ? Number(brSel.value) : null,
    };
    setTranscodingPrefs(next);
    // Tell the player to recompute its badge from the new prefs.
    window.dispatchEvent(new CustomEvent("muse:transcoding-prefs-changed"));
    saved.textContent = "— Saved";
    window.setTimeout(() => { saved.textContent = ""; }, 1500);
  };
  fmtSel.addEventListener("change", onChange);
  brSel.addEventListener("change", onChange);
}

function gcResultHtml(r: GcResult): string {
  const dbDelta = r.db_size_before_bytes - r.db_size_after_bytes;
  const lines = [
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
          <td style="color:var(--muted);text-transform:uppercase;letter-spacing:.1em;font-size:var(--t-micro);padding:.3rem 1rem .3rem 0">${escapeHtml(k as string)}</td>
          <td style="font-variant-numeric:tabular-nums">${escapeHtml(v as string)}</td>
        </tr>
      `).join("")}
    </table>
  `;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

async function refreshStats(host: HTMLElement): Promise<void> {
  const el = host.querySelector<HTMLElement>("[data-stats]");
  if (!el) return;
  try {
    const s = await libraryStats();
    el.classList.remove("loading");
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1.5rem">
        ${statBlock("Artists", String(s.artists))}
        ${statBlock("Albums", String(s.albums))}
        ${statBlock("Tracks", String(s.tracks))}
        ${statBlock("Total runtime", fmtDuration(s.total_duration_seconds))}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty">Stats unavailable: ${escapeHtml((e as Error).message)}</div>`;
  }
}

function statBlock(label: string, value: string): string {
  return `
    <div>
      <div class="label">— ${escapeHtml(label)}</div>
      <div class="folio" style="font-size:clamp(2.5rem,5vw,4rem)">${escapeHtml(value)}</div>
    </div>
  `;
}

async function refreshScan(host: HTMLElement): Promise<void> {
  const stateEl = host.querySelector<HTMLElement>("[data-scanstate]");
  if (!stateEl) return;
  let progress: ScanProgress;
  try {
    progress = await getScanProgress();
  } catch (e) {
    stateEl.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }
  stateEl.innerHTML = scanHtml(progress);

  const startBtn = host.querySelector<HTMLButtonElement>("[data-rescan]");
  const cancelBtn = host.querySelector<HTMLButtonElement>("[data-cancelscan]");
  if (startBtn) startBtn.disabled = progress.running;
  if (cancelBtn) cancelBtn.style.display = progress.running ? "" : "none";

  // Keep polling while a scan is running so the UI updates live, but
  // pause when the tab isn't visible (no point sending requests no
  // one's looking at) and tear down as soon as the scan finishes.
  if (progress.running) {
    if (pollHandle === undefined && !document.hidden) {
      pollHandle = window.setInterval(() => refreshScan(host), SCAN_POLL_INTERVAL_MS);
    }
  } else if (pollHandle !== undefined) {
    window.clearInterval(pollHandle);
    pollHandle = undefined;
    // After a scan finishes, stats / folder counts are stale.
    void refreshStats(host);
    void refreshFolders(host);
  }
}

function scanHtml(p: ScanProgress): string {
  if (!p.running && !p.started_at) {
    return `<span class="label">— No scan has run yet.</span>`;
  }
  if (p.running) {
    // Two stages of progress:
    //   - Phase 1 (walking): files_to_parse is still 0; show files_seen
    //     ticking up. Percentage is "unknown".
    //   - Phase 2 (parsing+writing): files_to_parse is set; the percentage
    //     reflects parse completion within the current folder.
    const toParse = p.files_to_parse ?? 0;
    const parsed = p.files_parsed ?? 0;
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
        ${p.errors ? `<span style="color:var(--accent)">${p.errors} errors</span>` : ""}
      </div>
      ${p.current_folder ? `<div style="margin-top:.5rem;font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.current_folder)}</div>` : ""}
    `;
  }
  // Finished
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
