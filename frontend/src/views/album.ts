// src/views/album.ts
//
// Single-album view: hero header with cover art + tracklist below.
// Clicking a track plays the album from that point.

import { getAlbum, coverArtUrl, type SubsonicSong } from "../api";
import { player, fmtDuration } from "../player";
import { escapeHtml, albumPlaceholder, renderArtistLinks } from "./_util";
import { pickPlaylistAndAdd } from "./_playlist_picker";

export async function renderAlbum(host: HTMLElement, id: string): Promise<void> {
  host.innerHTML = `<div class="loading">Loading album</div>`;
  try {
    const { album } = await getAlbum(id);
    const songs: SubsonicSong[] = (album as any).song ?? [];

    const art = coverArtUrl(album.coverArt, 600);
    const placeholder = albumPlaceholder(album.name);

    const totalDuration = songs.reduce((acc, s) => acc + (s.duration ?? 0), 0);

    // Group by disc number if the album spans more than one disc.
    const discNums = [...new Set(songs.map(s => s.discNumber ?? 1))].sort((a, b) => a - b);
    const isMultiDisc = discNums.length > 1;

    let trackRows: string;
    if (isMultiDisc) {
      trackRows = discNums.map(disc => {
        const header = `<tr class="disc-header"><td colspan="4">Disc ${disc}</td></tr>`;
        const rows = songs
          .map((s, i) => ({ s, i }))
          .filter(({ s }) => (s.discNumber ?? 1) === disc)
          .map(({ s, i }) => trackRowHtml(s, i))
          .join("");
        return header + rows;
      }).join("");
    } else {
      trackRows = songs.map((s, i) => trackRowHtml(s, i)).join("");
    }

    host.innerHTML = `
      <div class="album-head stagger">
        <div class="art" ${art ? `style="background-image:url('${art}')"` : `style="display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-size:6rem;color:var(--muted-2)"`}>
          ${art ? "" : escapeHtml(placeholder)}
        </div>
        <div class="info">
          <span class="label">— Album</span>
          <h1>${escapeHtml(album.name)}</h1>
          <div class="by">by ${renderArtistLinks(album.artist ?? "Unknown", album.artistId)}</div>
          <div class="stats">
            <span><strong>${songs.length}</strong> tracks</span>
            ${isMultiDisc ? `<span><strong>${discNums.length}</strong> discs</span>` : ""}
            <span><strong>${fmtDuration(totalDuration)}</strong> runtime</span>
            ${album.year ? `<span><strong>${album.year}</strong> released</span>` : ""}
            ${album.genre ? `<span><strong><a href="#/genre/${encodeURIComponent(album.genre)}">${escapeHtml(album.genre)}</a></strong> genre</span>` : ""}
          </div>
          <div class="actions">
            <button class="btn primary" data-play-all>▶ Play album</button>
            <button class="btn ghost" data-queue-all>+ Queue</button>
            <button class="btn ghost" data-add-album-to-playlist>+ Playlist</button>
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
          ${trackRows}
        </tbody>
      </table>
    `;

    // Wire interactions
    host.querySelector<HTMLButtonElement>("[data-play-all]")
        ?.addEventListener("click", () => player.playQueue(songs, 0));
    host.querySelector<HTMLButtonElement>("[data-queue-all]")
        ?.addEventListener("click", () => player.enqueue(songs));
    host.querySelector<HTMLButtonElement>("[data-add-album-to-playlist]")
        ?.addEventListener("click", async () => {
          if (songs.length === 0) return;
          const r = await pickPlaylistAndAdd(songs.map(s => String(s.id)));
          if (!r.added && r.message) window.alert(r.message);
        });

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
  const artistCell = renderArtistLinks(s.artist, s.artistId);
  // Subtle marker on tracks that carry lyrics (getAlbum sets hasLyrics).
  const lyricFlag = s.hasLyrics
    ? ` <span class="lyric-tag" title="Lyrics available">lyrics</span>`
    : "";
  return `
    <tr data-idx="${idx}" data-tid="${escapeHtml(String(s.id))}" style="cursor:pointer">
      <td class="num">${trackNum}</td>
      <td class="title"><a href="#/track/${encodeURIComponent(s.id)}">${escapeHtml(s.title)}</a>${lyricFlag}</td>
      <td>${artistCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
    </tr>
  `;
}
