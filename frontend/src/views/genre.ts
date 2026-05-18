// src/views/genre.ts
//
// Browse-by-genre view. Reached via #/genre/<name>, used when the user
// clicks a genre tag on an album or track page.
//
// Two sections — albums and tracks — each with its own "Load more" pager.
// Modelled after views/search.ts since the shape (paged sections + click-to-play)
// is the same.

import {
  getAlbumList, getSongsByGenre, coverArtUrl,
  type SubsonicAlbum, type SubsonicSong,
} from "../api";
import { player, fmtDuration } from "../player";
import { escapeHtml, albumPlaceholder, renderArtistLinks, attachLazyLoad } from "./_util";

const PAGE = 40;

// Per-genre paging state. Reset whenever the genre name changes.
let activeGenre = "";
let albumOffset = 0;
let songOffset = 0;
let accSongs: SubsonicSong[] = [];

export async function renderGenre(host: HTMLElement, name: string): Promise<void> {
  const genre = name.trim();
  if (genre !== activeGenre) {
    activeGenre = genre;
    albumOffset = 0;
    songOffset = 0;
    accSongs = [];
  }

  host.innerHTML = `
    <header class="page-head">
      <h1>Genre <em>${escapeHtml(genre)}</em></h1>
      <div class="meta">— browse</div>
    </header>
    <div data-results><div class="loading">Loading</div></div>
  `;

  const results = host.querySelector<HTMLElement>("[data-results]")!;
  await load(results, genre);
}

async function load(host: HTMLElement, genre: string): Promise<void> {
  try {
    const [albums, songs] = await Promise.all([
      getAlbumList("byGenre", PAGE, 0, { genre }),
      getSongsByGenre(genre, PAGE, 0),
    ]);
    albumOffset = albums.length;
    songOffset = songs.length;
    accSongs = [...songs];

    if (albums.length === 0 && songs.length === 0) {
      host.innerHTML = `<div class="empty">No albums or tracks tagged "${escapeHtml(genre)}".</div>`;
      return;
    }

    const sections: string[] = [];
    if (albums.length > 0) sections.push(albumSectionHtml(albums, albums.length === PAGE));
    if (songs.length > 0)  sections.push(songSectionHtml(songs, 0, songs.length === PAGE));
    host.innerHTML = `<div class="stagger">${sections.join("")}</div>`;

    wireSongClicks(host);
    wireLoadMore(host, genre);
  } catch (e) {
    host.innerHTML = `<div class="empty">Failed to load genre: ${escapeHtml((e as Error).message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// HTML builders
// ---------------------------------------------------------------------------

function albumSectionHtml(albums: SubsonicAlbum[], hasMore: boolean): string {
  return `
    <div data-album-section>
      <div class="section-head">
        <h2>Albums</h2>
        <span class="rule"></span>
        <span class="count" data-album-count>${albums.length}</span>
      </div>
      <div class="album-grid" data-album-list>
        ${albums.map(albumCardHtml).join("")}
      </div>
      ${hasMore ? loadMoreBtn("albums") : ""}
    </div>
  `;
}

function albumCardHtml(a: SubsonicAlbum): string {
  const art = coverArtUrl(a.coverArt, 320);
  const ph = albumPlaceholder(a.name);
  return `
    <a class="album-card" href="#/album/${encodeURIComponent(a.id)}">
      <div class="art" ${art ? `style="background-image:url('${art}')"` : ""}>
        ${art ? "" : `<div class="placeholder">${escapeHtml(ph)}</div>`}
      </div>
      <div class="title">${escapeHtml(a.name)}</div>
      <div class="artist">${escapeHtml(a.artist ?? "")}</div>
    </a>
  `;
}

function songSectionHtml(songs: SubsonicSong[], startIdx: number, hasMore: boolean): string {
  return `
    <div data-song-section>
      <div class="section-head">
        <h2>Tracks</h2>
        <span class="rule"></span>
        <span class="count" data-song-count>${songs.length}</span>
      </div>
      <table class="tracklist" data-songs>
        <thead>
          <tr>
            <th class="num">#</th>
            <th>Title</th>
            <th>Artist</th>
            <th>Album</th>
            <th class="duration">Time</th>
          </tr>
        </thead>
        <tbody>
          ${songs.map((s, i) => songRowHtml(s, startIdx + i)).join("")}
        </tbody>
      </table>
      ${hasMore ? loadMoreBtn("songs") : ""}
    </div>
  `;
}

function songRowHtml(s: SubsonicSong, idx: number): string {
  const artistCell = renderArtistLinks(s.artist, s.artistId);
  const albumCell = s.albumId
    ? `<a href="#/album/${encodeURIComponent(s.albumId)}">${escapeHtml(s.album ?? "")}</a>`
    : escapeHtml(s.album ?? "");
  return `
    <tr data-idx="${idx}" style="cursor:pointer">
      <td class="num">${idx + 1}</td>
      <td class="title">${escapeHtml(s.title)}</td>
      <td>${artistCell}</td>
      <td>${albumCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
    </tr>
  `;
}

function loadMoreBtn(section: "albums" | "songs"): string {
  return `<button class="btn ghost" data-load-more-${section} style="display:block;margin:1.5rem auto;padding:.6rem 2rem">Load more ${section}</button>`;
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function wireSongClicks(host: HTMLElement): void {
  const tbody = host.querySelector<HTMLTableSectionElement>("[data-songs] tbody");
  if (!tbody) return;
  tbody.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    if (target.closest("a")) return;
    const tr = target.closest("tr");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    if (Number.isFinite(idx)) player.playQueue(accSongs, idx);
  });
}

function wireLoadMore(host: HTMLElement, genre: string): void {
  const albumBtn = host.querySelector<HTMLButtonElement>("[data-load-more-albums]");
  if (albumBtn) {
    albumBtn.addEventListener("click", async () => {
      albumBtn.disabled = true;
      albumBtn.textContent = "Loading…";
      try {
        const more = await getAlbumList("byGenre", PAGE, albumOffset, { genre });
        albumOffset += more.length;
        host.querySelector("[data-album-list]")!
          .insertAdjacentHTML("beforeend", more.map(albumCardHtml).join(""));
        const countEl = host.querySelector("[data-album-count]");
        if (countEl) countEl.textContent = String(albumOffset);
        if (more.length < PAGE) albumBtn.remove();
        else { albumBtn.disabled = false; albumBtn.textContent = "Load more albums"; }
      } catch {
        albumBtn.disabled = false;
        albumBtn.textContent = "Load more albums";
      }
    });
    attachLazyLoad(albumBtn);
  }

  const songBtn = host.querySelector<HTMLButtonElement>("[data-load-more-songs]");
  if (songBtn) {
    songBtn.addEventListener("click", async () => {
      songBtn.disabled = true;
      songBtn.textContent = "Loading…";
      try {
        const more = await getSongsByGenre(genre, PAGE, songOffset);
        const startIdx = accSongs.length;
        accSongs.push(...more);
        songOffset += more.length;
        host.querySelector<HTMLTableSectionElement>("[data-songs] tbody")!
          .insertAdjacentHTML("beforeend", more.map((s, i) => songRowHtml(s, startIdx + i)).join(""));
        const countEl = host.querySelector("[data-song-count]");
        if (countEl) countEl.textContent = String(songOffset);
        if (more.length < PAGE) songBtn.remove();
        else { songBtn.disabled = false; songBtn.textContent = "Load more tracks"; }
      } catch {
        songBtn.disabled = false;
        songBtn.textContent = "Load more tracks";
      }
    });
    attachLazyLoad(songBtn);
  }
}
