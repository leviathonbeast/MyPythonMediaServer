// src/views/album.ts
//
// Single-album view: hero header with cover art + tracklist below.
// Clicking a track plays the album from that point.

import { getAlbum, coverArtUrl, type SubsonicSong } from "../api";
import { player, fmtDuration } from "../player";
import { escapeHtml, albumPlaceholder } from "./_util";

export async function renderAlbum(host: HTMLElement, id: string): Promise<void> {
  host.innerHTML = `<div class="loading">Loading album</div>`;
  try {
    const { album } = await getAlbum(id);
    const songs: SubsonicSong[] = (album as any).song ?? [];

    const art = coverArtUrl(album.coverArt, 600);
    const placeholder = albumPlaceholder(album.name);

    const totalDuration = songs.reduce((acc, s) => acc + (s.duration ?? 0), 0);

    host.innerHTML = `
      <div class="album-head stagger">
        <div class="art" ${art ? `style="background-image:url('${art}')"` : `style="display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-size:6rem;color:var(--muted-2)"`}>
          ${art ? "" : escapeHtml(placeholder)}
        </div>
        <div class="info">
          <span class="label">— Album</span>
          <h1>${escapeHtml(album.name)}</h1>
          <div class="by">by ${album.artistId
              ? `<a href="#/artist/${encodeURIComponent(album.artistId)}">${escapeHtml(album.artist ?? "Unknown")}</a>`
              : escapeHtml(album.artist ?? "Unknown")}</div>
          <div class="stats">
            <span><strong>${songs.length}</strong> tracks</span>
            <span><strong>${fmtDuration(totalDuration)}</strong> runtime</span>
            ${album.year ? `<span><strong>${album.year}</strong> released</span>` : ""}
            ${album.genre ? `<span><strong>${escapeHtml(album.genre)}</strong> genre</span>` : ""}
          </div>
          <div class="actions">
            <button class="btn primary" data-play-all>▶ Play album</button>
            <button class="btn ghost" data-queue-all>+ Queue</button>
          </div>
        </div>
      </div>

      <table class="tracklist stagger" data-tracklist>
        <thead>
          <tr>
            <th class="num">#</th>
            <th>Title</th>
            <th>Artist</th>
            <th class="duration">Time</th>
          </tr>
        </thead>
        <tbody>
          ${songs.map((s, i) => trackRowHtml(s, i)).join("")}
        </tbody>
      </table>
    `;

    // Wire interactions
    host.querySelector<HTMLButtonElement>("[data-play-all]")
        ?.addEventListener("click", () => player.playQueue(songs, 0));
    host.querySelector<HTMLButtonElement>("[data-queue-all]")
        ?.addEventListener("click", () => player.enqueue(songs));

    const tbody = host.querySelector<HTMLTableSectionElement>("[data-tracklist] tbody")!;
    tbody.addEventListener("click", (e) => {
      // Let title/artist links navigate naturally; play only on bare row clicks.
      if ((e.target as HTMLElement).closest("a")) return;
      const tr = (e.target as HTMLElement).closest("tr");
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      if (Number.isFinite(idx)) player.playQueue(songs, idx);
    });

    // Highlight currently playing row when state changes
    const unsub = player.subscribe((state) => {
      tbody.querySelectorAll("tr").forEach(tr => tr.classList.remove("playing"));
      if (!state.current) return;
      // Find a row whose track id matches and highlight it
      const row = tbody.querySelector(`tr[data-tid="${CSS.escape(String(state.current.id))}"]`);
      row?.classList.add("playing");
    });
    // If the user navigates away the host element is replaced; we attach
    // the unsubscriber to a custom property so the router can call it.
    (host as any).__cleanup = unsub;
  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load album: ${escapeHtml((e as Error).message)}</div>`;
  }
}

function trackRowHtml(s: SubsonicSong, idx: number): string {
  const trackNum = s.track ?? idx + 1;
  const artistCell = s.artistId
    ? `<a href="#/artist/${encodeURIComponent(s.artistId)}">${escapeHtml(s.artist ?? "")}</a>`
    : escapeHtml(s.artist ?? "");
  return `
    <tr data-idx="${idx}" data-tid="${escapeHtml(String(s.id))}" style="cursor:pointer">
      <td class="num">${trackNum}</td>
      <td class="title"><a href="#/track/${encodeURIComponent(s.id)}">${escapeHtml(s.title)}</a></td>
      <td>${artistCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
    </tr>
  `;
}
