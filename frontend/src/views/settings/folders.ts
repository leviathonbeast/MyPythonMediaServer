// src/views/settings/folders.ts
//
// Music folders section — admin-only.
//
// Lists music folders with their track counts and lets the admin add or
// remove them. Removing a folder removes its tracks from the LIBRARY but
// NEVER touches files on disk — we make that explicit in the confirm
// dialog so a misclick is recoverable.
//
// When folders change, the totals on the stats section need to be
// refreshed. We can't reach into the stats section directly, so the
// composer (index.ts) hands us a `ctx.onLibraryChanged` callback to
// invoke after a successful mutation.

import {
  listFolders, addFolder, deleteFolder,
  type MusicFolder,
} from "../../api";
import { escapeHtml } from "../_util";

// Context the composer passes in. Callbacks are all optional — the
// section works fine standalone if the parent doesn't care about
// cross-section refreshes.
export interface FoldersCtx {
  onLibraryChanged?: () => void;
}

export interface FoldersSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderFoldersSection(
  host: HTMLElement,
  ctx: FoldersCtx = {},
): Promise<FoldersSection> {
  host.insertAdjacentHTML("beforeend", `
    <div class="section-head">
      <h2>Music folders</h2>
      <span class="rule"></span>
    </div>
    <div class="panel" data-folders>
      <div class="loading">Loading folders</div>
    </div>
  `);

  const panel = host.querySelector<HTMLElement>("[data-folders]")!;

  const refresh = async () => {
    let folders: MusicFolder[];
    try {
      folders = await listFolders();
    } catch (e) {
      panel.classList.remove("loading");
      panel.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
      return;
    }

    panel.classList.remove("loading");
    panel.innerHTML = buildPanelHtml(folders);
    wireEvents(folders);
  };

  // Pure render — returns the HTML for the current folder list + the
  // "add folder" form below.
  const buildPanelHtml = (folders: MusicFolder[]): string => `
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

  // Wire up the "Remove" buttons and the "Add" form. We re-bind on every
  // refresh because the table is re-rendered each time.
  const wireEvents = (folders: MusicFolder[]) => {
    panel.querySelectorAll<HTMLButtonElement>("[data-del]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = Number(btn.dataset.del);
        const folder = folders.find(f => f.id === id);
        if (!folder) return;

        // Two confirm copy variants — make the destructive bit clear
        // when there are tracks at stake.
        const msg = folder.track_count > 0
          ? `Remove "${folder.name}" and all ${folder.track_count.toLocaleString()} of its tracks from the library?\n\n(Files on disk will NOT be deleted.)`
          : `Remove "${folder.name}"?`;
        if (!confirm(msg)) return;

        btn.disabled = true;
        try {
          await deleteFolder(id);
          await refresh();
          // Stats counters drop too — let the composer know.
          ctx.onLibraryChanged?.();
        } catch (e) {
          alert((e as Error).message);
          btn.disabled = false;
        }
      });
    });

    const form   = panel.querySelector<HTMLFormElement>("[data-addform]");
    const addmsg = panel.querySelector<HTMLElement>("[data-addmsg]");
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
        await refresh();
        ctx.onLibraryChanged?.();
      } catch (ex) {
        addmsg.style.color = "var(--accent)";
        addmsg.textContent = (ex as Error).message;
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  };

  await refresh();

  return {
    refresh,
    cleanup: () => { /* listeners die with the DOM */ },
  };
}
