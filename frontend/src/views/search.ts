// src/views/search.ts
//
// Search view — backed by /rest/search3.
// One big editorial input; results split into Artists / Albums / Songs.
// Each section has an independent "Load more" button backed by server-side offsets.

import { search3, coverArtUrl, type SubsonicArtist, type SubsonicAlbum, type SubsonicSong } from "../api";
import { player, fmtDuration } from "../player";
import { escapeHtml, albumPlaceholder, renderArtistLinks } from "./_util";

const SEARCH_PAGE = 20;

let lastQuery = "";

// Per-query offset state and accumulated song list (for the player queue).
// These reset whenever a new query is typed.
let artistOffset = 0;
let albumOffset = 0;
let songOffset = 0;
let accSongs: SubsonicSong[] = [];

export function renderSearch(host: HTMLElement, initialQuery?: string): void {
  // When the route carries a query (e.g. clicking a featured-artist link goes
  // to #/search/2WEI), seed the input and reset paging so we don't show stale
  // hits from the previous query.
  if (initialQuery !== undefined && initialQuery.trim() !== lastQuery) {
    lastQuery = initialQuery.trim();
    artistOffset = 0;
    albumOffset = 0;
    songOffset = 0;
    accSongs = [];
  }

  host.innerHTML = `
    <header class="page-head">
      <h1>Find <em>anything.</em></h1>
      <div class="meta">— search/3</div>
    </header>

    <div class="search-box">
      <span class="label">Q</span>
      <input data-q placeholder="title, artist, album…" autofocus value="${escapeHtml(lastQuery)}" />
    </div>

    <div data-results>
      <div class="empty">Type to search across your library.</div>
    </div>
  `;

  const input = host.querySelector<HTMLInputElement>("[data-q]")!;
  const results = host.querySelector<HTMLElement>("[data-results]")!;

  let timer: number | undefined;
  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      const q = input.value.trim();
      if (q !== lastQuery) {
        artistOffset = 0;
        albumOffset = 0;
        songOffset = 0;
        accSongs = [];
      }
      lastQuery = q;
      void run(results, q);
    }, 220);
  });

  if (lastQuery) void run(results, lastQuery);
}

async function run(host: HTMLElement, q: string): Promise<void> {
  if (!q) {
    host.innerHTML = `<div class="empty">Type to search across your library.</div>`;
    return;
  }
  host.innerHTML = `<div class="loading">Searching</div>`;
  try {
    const r = await search3(q, {
      artistCount: SEARCH_PAGE,
      albumCount: SEARCH_PAGE,
      songCount: SEARCH_PAGE,
    });

    artistOffset = r.artist.length;
    albumOffset = r.album.length;
    songOffset = r.song.length;
    accSongs = [...r.song];

    const empty = r.artist.length === 0 && r.album.length === 0 && r.song.length === 0;
    if (empty) {
      host.innerHTML = `<div class="empty">No matches for "${escapeHtml(q)}".</div>`;
      return;
    }

    const sections: string[] = [];
    if (r.artist.length > 0) sections.push(artistSectionHtml(r.artist, r.artist.length === SEARCH_PAGE));
    if (r.album.length > 0)  sections.push(albumSectionHtml(r.album, r.album.length === SEARCH_PAGE));
    if (r.song.length > 0)   sections.push(songSectionHtml(r.song, 0, r.song.length === SEARCH_PAGE));

    host.innerHTML = `<div class="stagger">${sections.join("")}</div>`;

    wireSongClicks(host);
    wireLoadMore(host, q);
  } catch (e) {
    host.innerHTML = `<div class="empty">Search failed: ${escapeHtml((e as Error).message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// HTML builders
// ---------------------------------------------------------------------------

function artistSectionHtml(artists: SubsonicArtist[], hasMore: boolean): string {
  return `
    <div data-artist-section>
      <div class="section-head">
        <h2>Artists</h2>
        <span class="rule"></span>
        <span class="count" data-artist-count>${artists.length}</span>
      </div>
      <ul class="index-bucket" data-artist-list style="list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.25rem 1.5rem">
        ${artists.map(artistItemHtml).join("")}
      </ul>
      ${hasMore ? loadMoreBtn("artists") : ""}
    </div>
  `;
}

function artistItemHtml(a: SubsonicArtist): string {
  return `
    <li><a href="#/artist/${encodeURIComponent(a.id)}" style="display:flex;justify-content:space-between;padding:.4rem 0;border-bottom:1px solid var(--rule);font-family:var(--font-display)">
      <span>${escapeHtml(a.name)}</span>
      <span style="font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted)">${a.albumCount ?? ""}</span>
    </a></li>
  `;
}

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

function loadMoreBtn(section: "artists" | "albums" | "songs"): string {
  return `<button class="btn ghost" data-load-more-${section} style="display:block;margin:1.5rem auto;padding:.6rem 2rem">Load more ${section}</button>`;
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function wireSongClicks(host: HTMLElement): void {
  const tbody = host.querySelector<HTMLTableSectionElement>("[data-songs] tbody");
  if (!tbody) return;
  // accSongs is module-level so it stays current as more pages load.
  tbody.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    // Let artist/album links navigate naturally; don't also start playback.
    if (target.closest("a")) return;
    const tr = target.closest("tr");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    if (Number.isFinite(idx)) player.playQueue(accSongs, idx);
  });
}

function wireLoadMore(host: HTMLElement, q: string): void {
  const artistBtn = host.querySelector<HTMLButtonElement>("[data-load-more-artists]");
  artistBtn?.addEventListener("click", async () => {
    artistBtn.disabled = true;
    artistBtn.textContent = "Loading…";
    try {
      const r = await search3(q, { artistCount: SEARCH_PAGE, albumCount: 0, songCount: 0, artistOffset });
      artistOffset += r.artist.length;
      host.querySelector("[data-artist-list]")!
        .insertAdjacentHTML("beforeend", r.artist.map(artistItemHtml).join(""));
      const countEl = host.querySelector("[data-artist-count]");
      if (countEl) countEl.textContent = String(artistOffset);
      if (r.artist.length < SEARCH_PAGE) artistBtn.remove();
      else { artistBtn.disabled = false; artistBtn.textContent = "Load more artists"; }
    } catch {
      artistBtn.disabled = false;
      artistBtn.textContent = "Load more artists";
    }
  });

  const albumBtn = host.querySelector<HTMLButtonElement>("[data-load-more-albums]");
  albumBtn?.addEventListener("click", async () => {
    albumBtn.disabled = true;
    albumBtn.textContent = "Loading…";
    try {
      const r = await search3(q, { artistCount: 0, albumCount: SEARCH_PAGE, songCount: 0, albumOffset });
      albumOffset += r.album.length;
      host.querySelector("[data-album-list]")!
        .insertAdjacentHTML("beforeend", r.album.map(albumCardHtml).join(""));
      const countEl = host.querySelector("[data-album-count]");
      if (countEl) countEl.textContent = String(albumOffset);
      if (r.album.length < SEARCH_PAGE) albumBtn.remove();
      else { albumBtn.disabled = false; albumBtn.textContent = "Load more albums"; }
    } catch {
      albumBtn.disabled = false;
      albumBtn.textContent = "Load more albums";
    }
  });

  const songBtn = host.querySelector<HTMLButtonElement>("[data-load-more-songs]");
  songBtn?.addEventListener("click", async () => {
    songBtn.disabled = true;
    songBtn.textContent = "Loading…";
    try {
      const r = await search3(q, { artistCount: 0, albumCount: 0, songCount: SEARCH_PAGE, songOffset });
      const startIdx = accSongs.length;
      accSongs.push(...r.song);
      songOffset += r.song.length;
      host.querySelector<HTMLTableSectionElement>("[data-songs] tbody")!
        .insertAdjacentHTML("beforeend", r.song.map((s, i) => songRowHtml(s, startIdx + i)).join(""));
      const countEl = host.querySelector("[data-song-count]");
      if (countEl) countEl.textContent = String(songOffset);
      if (r.song.length < SEARCH_PAGE) songBtn.remove();
      else { songBtn.disabled = false; songBtn.textContent = "Load more tracks"; }
    } catch {
      songBtn.disabled = false;
      songBtn.textContent = "Load more tracks";
    }
  });
}
