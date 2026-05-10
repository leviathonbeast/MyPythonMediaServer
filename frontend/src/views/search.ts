// src/views/search.ts
//
// Search view — backed by /rest/search3.
// One big editorial input; results split into Artists / Albums / Songs.

import { search3, coverArtUrl } from "../api";
import { player, fmtDuration } from "../player";
import { escapeHtml, albumPlaceholder } from "./_util";

let lastQuery = "";

export function renderSearch(host: HTMLElement): void {
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

  // Debounce-ish: we only fire on a 220ms idle, to avoid hammering the
  // backend during fast typing on big libraries.
  let timer: number | undefined;
  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      lastQuery = input.value.trim();
      void run(results, lastQuery);
    }, 220);
  });

  // If returning with a previous query, run it immediately.
  if (lastQuery) void run(results, lastQuery);
}

async function run(host: HTMLElement, q: string): Promise<void> {
  if (!q) {
    host.innerHTML = `<div class="empty">Type to search across your library.</div>`;
    return;
  }
  host.innerHTML = `<div class="loading">Searching</div>`;
  try {
    const r = await search3(q);
    const empty = r.artist.length === 0 && r.album.length === 0 && r.song.length === 0;
    if (empty) {
      host.innerHTML = `<div class="empty">No matches for “${escapeHtml(q)}”.</div>`;
      return;
    }
    const html: string[] = [];

    if (r.artist.length > 0) {
      html.push(`
        <div class="section-head">
          <h2>Artists</h2>
          <span class="rule"></span>
          <span class="count">${r.artist.length}</span>
        </div>
        <ul class="index-bucket" style="list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.25rem 1.5rem">
          ${r.artist.map(a => `
            <li><a href="#/artist/${encodeURIComponent(a.id)}" style="display:flex;justify-content:space-between;padding:.4rem 0;border-bottom:1px solid var(--rule);font-family:var(--font-display)">
              <span>${escapeHtml(a.name)}</span>
              <span style="font-family:var(--font-mono);font-size:var(--t-micro);color:var(--muted)">${a.albumCount ?? ""}</span>
            </a></li>
          `).join("")}
        </ul>
      `);
    }

    if (r.album.length > 0) {
      html.push(`
        <div class="section-head">
          <h2>Albums</h2>
          <span class="rule"></span>
          <span class="count">${r.album.length}</span>
        </div>
        <div class="album-grid">
          ${r.album.map(a => {
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
          }).join("")}
        </div>
      `);
    }

    if (r.song.length > 0) {
      html.push(`
        <div class="section-head">
          <h2>Tracks</h2>
          <span class="rule"></span>
          <span class="count">${r.song.length}</span>
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
            ${r.song.map((s, i) => `
              <tr data-idx="${i}" style="cursor:pointer">
                <td class="num">${i + 1}</td>
                <td class="title">${escapeHtml(s.title)}</td>
                <td>${escapeHtml(s.artist ?? "")}</td>
                <td>${escapeHtml(s.album ?? "")}</td>
                <td class="duration">${fmtDuration(s.duration)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `);
    }

    host.innerHTML = `<div class="stagger">${html.join("")}</div>`;

    // Wire song clicks: replace queue with these search results, start at clicked index.
    const tbody = host.querySelector<HTMLTableSectionElement>("[data-songs] tbody");
    if (tbody) {
      tbody.addEventListener("click", (e) => {
        const tr = (e.target as HTMLElement).closest("tr");
        if (!tr) return;
        const idx = Number(tr.dataset.idx);
        if (Number.isFinite(idx)) player.playQueue(r.song, idx);
      });
    }
  } catch (e) {
    host.innerHTML = `<div class="empty">Search failed: ${escapeHtml((e as Error).message)}</div>`;
  }
}
