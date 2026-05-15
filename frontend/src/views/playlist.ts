// src/views/playlist.ts
//
// Single playlist view — header with name/owner/stats, then the tracklist.
// Clicking a track plays the playlist from that point.
//
// Owner-only affordances (rename, remove track, delete playlist) are gated
// on `p.owner === currentUsername`. The server enforces the same rule, but
// hiding the controls keeps the UI from advertising actions that will 401.

import {
  getPlaylist,
  updatePlaylist,
  deletePlaylist,
  type SubsonicPlaylist,
  type SubsonicSong,
} from "../api";
import { authState } from "../auth";
import { player, fmtDuration } from "../player";
import { escapeHtml, renderArtistLinks } from "./_util";

export async function renderPlaylist(host: HTMLElement, id: string): Promise<void> {
  host.innerHTML = `<div class="loading">Loading playlist</div>`;

  let playlist: SubsonicPlaylist;
  try {
    playlist = await getPlaylist(id);
  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load playlist: ${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  const songs: SubsonicSong[] = playlist.entry ?? [];
  const { username } = authState();
  const isOwner = username !== null && playlist.owner === username;
  const totalDuration = songs.reduce((acc, s) => acc + (s.duration ?? 0), 0);

  host.innerHTML = `
    <header class="page-head stagger">
      <h1>${escapeHtml(playlist.name)}</h1>
      <div class="meta">
        by <strong>${escapeHtml(playlist.owner)}</strong> ·
        ${songs.length} track${songs.length === 1 ? "" : "s"} ·
        ${fmtDuration(totalDuration)} ·
        ${playlist.public ? "public" : "private"}
      </div>
    </header>

    <div class="section-head" style="flex-wrap:wrap;gap:.5rem">
      <button class="btn primary" data-play-all ${songs.length === 0 ? "disabled" : ""}>▶ Play playlist</button>
      <button class="btn ghost" data-queue-all ${songs.length === 0 ? "disabled" : ""}>+ Queue</button>
      ${isOwner ? `<button class="btn ghost" data-toggle-public>${playlist.public ? "Make private" : "Make public"}</button>` : ""}
      ${isOwner ? `<button class="btn ghost" data-rename>Rename</button>` : ""}
      ${isOwner ? `<button class="btn ghost" data-delete-pl style="color:#d66">Delete playlist</button>` : ""}
    </div>

    ${songs.length === 0
      ? `<div class="empty">This playlist is empty.</div>`
      : `
        <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
          <table class="tracklist stagger" data-tracklist style="min-width:32rem">
            <thead>
              <tr>
                <th class="num">#</th>
                <th>Title</th>
                <th>Artist</th>
                <th>Album</th>
                <th class="duration">Time</th>
                ${isOwner ? `<th></th>` : ""}
              </tr>
            </thead>
            <tbody>
              ${songs.map((s, i) => rowHtml(s, i, isOwner)).join("")}
            </tbody>
          </table>
        </div>
      `}
  `;

  // Wire actions
  host.querySelector<HTMLButtonElement>("[data-play-all]")
      ?.addEventListener("click", () => player.playQueue(songs, 0));
  host.querySelector<HTMLButtonElement>("[data-queue-all]")
      ?.addEventListener("click", () => player.enqueue(songs));

  if (isOwner) {
    host.querySelector<HTMLButtonElement>("[data-rename]")
        ?.addEventListener("click", () => void onRename(host, playlist));
    host.querySelector<HTMLButtonElement>("[data-delete-pl]")
        ?.addEventListener("click", () => void onDeletePlaylist(playlist));
    host.querySelector<HTMLButtonElement>("[data-toggle-public]")
        ?.addEventListener("click", () => void onTogglePublic(host, playlist));
  }

  const tbody = host.querySelector<HTMLTableSectionElement>("[data-tracklist] tbody");
  if (tbody) {
    tbody.addEventListener("click", async (e) => {
      const target = e.target as HTMLElement;

      // Remove-from-playlist button: stop the row click from playing the track.
      const removeBtn = target.closest<HTMLButtonElement>("[data-remove-idx]");
      if (removeBtn) {
        e.stopPropagation();
        const idx = Number(removeBtn.dataset.removeIdx);
        if (Number.isFinite(idx)) await onRemoveTrack(host, playlist.id, idx);
        return;
      }

      // Let title/artist/album links navigate naturally.
      if (target.closest("a")) return;

      const tr = target.closest("tr");
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      if (Number.isFinite(idx)) player.playQueue(songs, idx);
    });

    // Highlight the currently playing row.
    const unsub = player.subscribe((state) => {
      tbody.querySelectorAll("tr").forEach(tr => tr.classList.remove("playing"));
      if (!state.current) return;
      const row = tbody.querySelector(`tr[data-tid="${CSS.escape(String(state.current.id))}"]`);
      row?.classList.add("playing");
    });
    (host as any).__cleanup = unsub;
  }
}

function rowHtml(s: SubsonicSong, idx: number, isOwner: boolean): string {
  const artistCell = renderArtistLinks(s.artist, s.artistId);
  const albumCell = s.albumId
    ? `<a href="#/album/${encodeURIComponent(s.albumId)}">${escapeHtml(s.album ?? "")}</a>`
    : escapeHtml(s.album ?? "");
  return `
    <tr data-idx="${idx}" data-tid="${escapeHtml(String(s.id))}" style="cursor:pointer">
      <td class="num">${idx + 1}</td>
      <td class="title"><a href="#/track/${encodeURIComponent(String(s.id))}">${escapeHtml(s.title)}</a></td>
      <td>${artistCell}</td>
      <td>${albumCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
      ${isOwner ? `<td><button class="btn ghost" data-remove-idx="${idx}" title="Remove from playlist" style="padding:.1rem .5rem">×</button></td>` : ""}
    </tr>
  `;
}

async function onRename(host: HTMLElement, playlist: SubsonicPlaylist): Promise<void> {
  const next = window.prompt("New name?", playlist.name);
  if (!next || !next.trim() || next.trim() === playlist.name) return;
  try {
    await updatePlaylist(playlist.id, { name: next.trim() });
  } catch (e) {
    window.alert(`Could not rename: ${(e as Error).message}`);
    return;
  }
  // Re-render with the new name.
  await renderPlaylist(host, String(playlist.id));
}

async function onTogglePublic(host: HTMLElement, playlist: SubsonicPlaylist): Promise<void> {
  // Optimistic UX: server roundtrip is fast and the only failure mode is a
  // permission denial which the page will surface on re-render anyway. Keep
  // it simple — no inline spinner.
  const next = !playlist.public;
  try {
    await updatePlaylist(playlist.id, { public: next });
  } catch (e) {
    window.alert(`Could not change visibility: ${(e as Error).message}`);
    return;
  }
  await renderPlaylist(host, String(playlist.id));
}

async function onDeletePlaylist(playlist: SubsonicPlaylist): Promise<void> {
  if (!window.confirm(`Delete playlist "${playlist.name}"? This cannot be undone.`)) return;
  try {
    await deletePlaylist(playlist.id);
  } catch (e) {
    window.alert(`Could not delete: ${(e as Error).message}`);
    return;
  }
  location.hash = "#/playlists";
}

async function onRemoveTrack(
  host: HTMLElement,
  playlistId: string | number,
  index: number,
): Promise<void> {
  try {
    // The Subsonic spec removes by *track index within the playlist*, not by
    // track id — handy because it lets you remove a duplicate without nuking
    // the other copy.
    await updatePlaylist(playlistId, { songIndexToRemove: [index] });
  } catch (e) {
    window.alert(`Could not remove track: ${(e as Error).message}`);
    return;
  }
  // Re-render so indices stay accurate after removal.
  await renderPlaylist(host, String(playlistId));
}
