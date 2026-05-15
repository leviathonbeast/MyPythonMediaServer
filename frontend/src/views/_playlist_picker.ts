// src/views/_playlist_picker.ts
//
// A tiny modal that asks the user which playlist to add a song (or set of
// songs) to. Returns a Promise that resolves once the picker is closed.
//
// UX summary:
//   - List the current user's OWN playlists (we can't add tracks to someone
//     else's public playlist — the server would reject it).
//   - "Create new playlist with these tracks" as a second option, with an
//     inline name input so a brand-new playlist can be made in one round.
//   - Esc / Cancel closes without doing anything; the promise resolves to
//     a status flag the caller can use to show a toast or refresh state.
//
// Implementation notes:
//   - Built on the native <dialog> element so we get focus trapping,
//     ESC-to-close, and the backdrop "for free" without pulling in a modal
//     library.
//   - We add new tracks to an existing playlist via updatePlaylist with
//     songIdToAdd, which accepts the Subsonic prefixed-id form ("tr-5")
//     directly. For the create-and-add path we do it in two steps —
//     createPlaylist(name, []) then updatePlaylist(newId, { songIdToAdd }) —
//     because the server's createPlaylist songId param is typed list[int]
//     and would 422 on a prefixed id.

import {
  getPlaylists,
  updatePlaylist,
  createPlaylist,
  type SubsonicPlaylist,
} from "../api";
import { authState } from "../auth";
import { escapeHtml } from "./_util";

export interface PickerResult {
  /** True if a playlist was modified/created. */
  added: boolean;
  /** Friendly label suitable for a toast. Empty when nothing happened. */
  message: string;
}

/**
 * Open the picker for one or more songs. Returns once the dialog closes.
 * The caller owns user-facing feedback (toast / alert) and can read the
 * message field for a ready-made one-liner.
 */
export async function pickPlaylistAndAdd(songIds: string[]): Promise<PickerResult> {
  if (songIds.length === 0) return { added: false, message: "" };

  const { username } = authState();
  if (username === null) {
    return { added: false, message: "Not signed in" };
  }

  // Fetch playlists fresh every time the picker opens so a playlist that
  // was just created in another tab shows up immediately.
  let playlists: SubsonicPlaylist[];
  try {
    playlists = await getPlaylists();
  } catch (e) {
    return { added: false, message: `Could not load playlists: ${(e as Error).message}` };
  }
  const owned = playlists.filter(p => p.owner === username);

  return new Promise<PickerResult>((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "playlist-picker";
    dlg.style.cssText = [
      "padding:0",
      "border:1px solid var(--rule)",
      "background:var(--bg)",
      "color:var(--ink)",
      "max-width:420px",
      "width:90vw",
      "border-radius:4px",
    ].join(";");

    const songLabel = songIds.length === 1 ? "1 song" : `${songIds.length} songs`;

    dlg.innerHTML = `
      <form method="dialog" style="margin:0">
        <div style="padding:1rem 1.25rem;border-bottom:1px solid var(--rule)">
          <strong>Add ${escapeHtml(songLabel)} to a playlist</strong>
        </div>

        <div style="padding:.5rem 0;max-height:50vh;overflow-y:auto" data-owned>
          ${owned.length === 0
              ? `<div class="meta" style="padding:.5rem 1.25rem">You don't own any playlists yet.</div>`
              : owned.map(ownedRowHtml).join("")
          }
        </div>

        <div style="border-top:1px solid var(--rule);padding:.75rem 1.25rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <input
            type="text"
            data-new-name
            placeholder="…or new playlist name"
            style="flex:1 1 12rem;min-width:0;background:transparent;border:1px solid var(--rule);color:var(--ink);padding:.4rem .6rem"
          />
          <button type="button" class="btn primary" data-create>Create</button>
        </div>

        <div style="padding:.5rem 1.25rem;text-align:right;border-top:1px solid var(--rule)">
          <button type="button" class="btn ghost" data-cancel>Cancel</button>
        </div>
      </form>
    `;

    document.body.appendChild(dlg);

    // The dialog can close via three paths (cancel button, backdrop click,
    // ESC keydown -> "close" event) AND our success paths. cleanup() must be
    // idempotent because calling dlg.close() synchronously re-fires the
    // close handler. The `done` flag is the gate.
    let done = false;
    const cleanup = (result: PickerResult): void => {
      if (done) return;
      done = true;
      if (dlg.open) dlg.close();
      dlg.remove();
      resolve(result);
    };

    dlg.querySelector<HTMLButtonElement>("[data-cancel]")
        ?.addEventListener("click", () => cleanup({ added: false, message: "" }));

    // Clicking the backdrop (outside the form) cancels.
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) cleanup({ added: false, message: "" });
    });
    // ESC fires "close" on <dialog>. Treat it as a cancel.
    dlg.addEventListener("close", () => cleanup({ added: false, message: "" }));

    // Wire each existing-playlist button.
    dlg.querySelectorAll<HTMLButtonElement>("[data-pl-id]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const plId = btn.dataset.plId!;
        const plName = btn.dataset.plName ?? "playlist";
        btn.disabled = true;
        try {
          await updatePlaylist(plId, { songIdToAdd: songIds });
          cleanup({ added: true, message: `Added ${songLabel} to "${plName}"` });
        } catch (e) {
          btn.disabled = false;
          cleanup({ added: false, message: `Failed: ${(e as Error).message}` });
        }
      });
    });

    // "Create new playlist with these tracks" button.
    const nameInput = dlg.querySelector<HTMLInputElement>("[data-new-name]")!;
    const createBtn = dlg.querySelector<HTMLButtonElement>("[data-create]")!;
    const doCreate = async (): Promise<void> => {
      const name = nameInput.value.trim();
      if (!name) { nameInput.focus(); return; }
      createBtn.disabled = true;
      try {
        const fresh = await createPlaylist(name, []);
        // Two-step add because createPlaylist's songId param wants raw ints
        // on this server, while updatePlaylist's songIdToAdd accepts the
        // prefixed Subsonic form we have on the client. Doing it via update
        // sidesteps the type mismatch.
        await updatePlaylist(fresh.id, { songIdToAdd: songIds });
        cleanup({ added: true, message: `Created "${name}" with ${songLabel}` });
      } catch (e) {
        createBtn.disabled = false;
        cleanup({ added: false, message: `Failed: ${(e as Error).message}` });
      }
    };
    createBtn.addEventListener("click", () => void doCreate());
    nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); void doCreate(); }
    });

    dlg.showModal();
    // Focus the input by default so a power user can type-and-Enter without
    // touching the mouse.
    nameInput.focus();
  });
}

function ownedRowHtml(p: SubsonicPlaylist): string {
  return `
    <button
      type="button"
      data-pl-id="${escapeHtml(String(p.id))}"
      data-pl-name="${escapeHtml(p.name)}"
      style="display:block;width:100%;text-align:left;padding:.5rem 1.25rem;background:transparent;border:none;color:var(--ink);cursor:pointer;font:inherit"
      onmouseover="this.style.background='var(--rule)'"
      onmouseout="this.style.background='transparent'"
    >
      ${escapeHtml(p.name)}
      <span class="meta" style="font-size:.85em;margin-left:.5rem">
        ${p.songCount ?? 0} song${(p.songCount ?? 0) === 1 ? "" : "s"} ·
        ${p.public ? "public" : "private"}
      </span>
    </button>
  `;
}
