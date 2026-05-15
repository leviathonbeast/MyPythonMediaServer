// src/views/playlists.ts
//
// The playlists index — shows every playlist the user can see (their own
// plus public ones from other users) and lets them create new ones.
//
// Read shape comes from getPlaylists() which calls /rest/getPlaylists. The
// detail (with tracks) is fetched separately when the user opens one, so
// this list call stays cheap even with hundreds of playlists.

import { getPlaylists, createPlaylist, type SubsonicPlaylist } from "../api";
import { authState } from "../auth";
import { escapeHtml } from "./_util";
import { fmtDuration } from "../player";

export async function renderPlaylists(host: HTMLElement): Promise<void> {
  host.innerHTML = `
    <header class="page-head">
      <h1>Playlists <em>by hand</em></h1>
      <div class="meta" data-stats>—</div>
    </header>

    <div class="section-head" style="flex-wrap:wrap;gap:.5rem">
      <h2>Yours and shared</h2>
      <span class="rule"></span>
      <button class="btn primary" data-new>+ New playlist</button>
    </div>

    <div data-list><div class="loading">Loading playlists</div></div>
  `;

  host.querySelector<HTMLButtonElement>("[data-new]")
      ?.addEventListener("click", () => void onCreate(host));

  await load(host);
}

async function load(host: HTMLElement): Promise<void> {
  const list = host.querySelector<HTMLElement>("[data-list]")!;
  const stats = host.querySelector<HTMLElement>("[data-stats]")!;
  list.innerHTML = `<div class="loading">Loading playlists</div>`;

  let playlists: SubsonicPlaylist[];
  try {
    playlists = await getPlaylists();
  } catch (e) {
    list.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  stats.textContent = `${playlists.length} playlist${playlists.length === 1 ? "" : "s"}`;

  if (playlists.length === 0) {
    list.innerHTML = `<div class="empty">No playlists yet — hit “New playlist” to start one.</div>`;
    return;
  }

  // Split by visibility and sort each side by most-recently-changed first.
  // Private playlists are necessarily the user's own (the server only returns
  // your own privates + everyone's publics), so the "Private" section is
  // effectively "Just for you".
  const byChanged = (a: SubsonicPlaylist, b: SubsonicPlaylist): number => {
    const ta = a.changed ? Date.parse(a.changed) : 0;
    const tb = b.changed ? Date.parse(b.changed) : 0;
    if (ta !== tb) return tb - ta;
    return a.name.localeCompare(b.name);
  };
  const privatePls = playlists.filter(p => !p.public).sort(byChanged);
  const publicPls  = playlists.filter(p =>  p.public).sort(byChanged);

  const { username } = authState();

  list.innerHTML = `
    ${sectionHtml("Private", "Just for you", privatePls, username)}
    ${sectionHtml("Public",  "Shared with everyone", publicPls, username)}
  `;
}

/**
 * Render one of the two visibility-grouped sections. We hide a section
 * entirely when it has no rows rather than showing an empty table — the
 * empty hint at the top of the page covers the "no playlists at all" case.
 */
function sectionHtml(
  title: string,
  subtitle: string,
  playlists: SubsonicPlaylist[],
  currentUsername: string | null,
): string {
  if (playlists.length === 0) return "";
  return `
    <div class="section-head" style="margin-top:1.5rem">
      <h2>${escapeHtml(title)}</h2>
      <span class="rule"></span>
      <span class="count">${playlists.length}</span>
    </div>
    <div class="meta" style="margin-bottom:.5rem">${escapeHtml(subtitle)}</div>
    <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
      <table class="tracklist stagger" style="min-width:28rem">
        <thead>
          <tr>
            <th>Name</th>
            <th>Owner</th>
            <th class="num">Songs</th>
            <th class="duration">Time</th>
          </tr>
        </thead>
        <tbody>
          ${playlists.map(p => rowHtml(p, currentUsername)).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function rowHtml(p: SubsonicPlaylist, currentUsername: string | null): string {
  // Show "You" instead of the username when the current user owns the row,
  // so the table reads naturally at a glance.
  const ownerLabel = currentUsername !== null && p.owner === currentUsername
    ? "You"
    : escapeHtml(p.owner);
  return `
    <tr data-tid="${escapeHtml(String(p.id))}" style="cursor:pointer">
      <td class="title">
        <a href="#/playlist/${encodeURIComponent(String(p.id))}">${escapeHtml(p.name)}</a>
      </td>
      <td>${ownerLabel}</td>
      <td class="num">${p.songCount ?? 0}</td>
      <td class="duration">${fmtDuration(p.duration ?? 0)}</td>
    </tr>
  `;
}

async function onCreate(host: HTMLElement): Promise<void> {
  const name = window.prompt("Name for the new playlist?");
  if (!name || !name.trim()) return;
  try {
    await createPlaylist(name.trim());
  } catch (e) {
    window.alert(`Could not create playlist: ${(e as Error).message}`);
    return;
  }
  await load(host);
}
