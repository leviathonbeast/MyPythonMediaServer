// src/views/settings/streaming.ts
//
// Streaming preferences section — visible to every user.
//
// Lets the user pick a preferred format and a max client-side bitrate for
// THIS browser. Preferences are stored in localStorage (see api.ts:
// getTranscodingPrefs / setTranscodingPrefs), so they apply only to this
// browser/device — mobile apps and other browsers keep their own settings.
//
// Also shows the server's own defaults and hard caps as read-only info,
// so the user can see what the SERVER will accept regardless of their
// browser preference.

import {
  getTranscodingPolicy, getTranscodingPrefs, setTranscodingPrefs,
  type TranscodingPolicy, type TranscodingPrefs, type TranscodingFormat,
} from "../../api";
import { escapeHtml } from "../_util";

export interface StreamingSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderStreamingSection(host: HTMLElement): Promise<StreamingSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Streaming</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-streaming>
      <div class="loading">Loading transcoding policy</div>
    </div>
  `);

  const panel = host.querySelector<HTMLElement>("[data-streaming]")!;

  const refresh = async () => {
    let policy: TranscodingPolicy;
    try {
      policy = await getTranscodingPolicy();
    } catch (e) {
      panel.classList.remove("loading");
      panel.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    // Read the user's saved per-browser prefs.
    const prefs = getTranscodingPrefs();

    // Build the bitrate dropdown from whatever presets the server reports.
    // Adding a preset on the backend automatically appears here — no
    // frontend change needed. Sort high → low because that's how users
    // think about "max" caps.
    const bitrateOptions = Array.from(new Set(policy.presets.map(p => p.bitrate)))
      .sort((a, b) => b - a);

    // Format dropdown: "auto" (defer to server default), "raw" (no
    // transcoding), plus whichever formats the server has presets for.
    const formatOptions: { value: TranscodingFormat; label: string }[] = [
      { value: "auto", label: `Auto (server default: ${policy.default_format})` },
      { value: "raw",  label: "Original — no transcoding" },
      ...Array.from(new Set(policy.presets.map(p => p.format))).map(f => ({
        value: f as TranscodingFormat,
        label: f.toUpperCase(),
      })),
    ];

    panel.classList.remove("loading");
    panel.innerHTML = `
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

    // Re-attach listeners after the innerHTML repaint. (Re-rendering
    // wipes the old elements — and their listeners — and creates new
    // ones, so we re-bind every time.)
    const fmtSel = panel.querySelector<HTMLSelectElement>("[data-tcformat]")!;
    const brSel  = panel.querySelector<HTMLSelectElement>("[data-tcbitrate]")!;
    const saved  = panel.querySelector<HTMLElement>("[data-tcsaved]")!;

    const onChange = () => {
      const next: TranscodingPrefs = {
        format: fmtSel.value as TranscodingFormat,
        maxBitRate: Number(brSel.value) > 0 ? Number(brSel.value) : null,
      };
      setTranscodingPrefs(next);
      // Notify the player so it recomputes its "original / transcoded"
      // badge from the new prefs.
      window.dispatchEvent(new CustomEvent("muse:transcoding-prefs-changed"));
      saved.textContent = "— Saved";
      window.setTimeout(() => { saved.textContent = ""; }, 1500);
    };
    fmtSel.addEventListener("change", onChange);
    brSel.addEventListener("change", onChange);
  };

  await refresh();

  return {
    refresh,
    // Listeners are attached to elements inside the panel. They'll be
    // garbage-collected when the DOM is detached on view-unmount.
    cleanup: () => { /* noop */ },
  };
}
