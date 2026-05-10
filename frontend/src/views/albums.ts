// src/views/albums.ts
//
// The "newest" / "alphabetical" album grid.
// Acts as the homepage when you first land in the app — gives the user
// something visually rich to chew on instead of an empty index.

import { getAlbumList, coverArtUrl, type SubsonicAlbum } from "../api";
import { escapeHtml, albumPlaceholder } from "./_util";

type SortMode = "newest" | "alphabeticalByName" | "byYear" | "random";

const SORT_LABELS: Record<SortMode, string> = {
  newest: "Newest additions",
  alphabeticalByName: "A — Z",
  byYear: "By year",
  random: "Shuffle the shelf",
};

let currentSort: SortMode = "newest";

export async function renderAlbums(host: HTMLElement): Promise<void> {
  host.innerHTML = `
    <header class="page-head">
      <h1>Albums <em>at large</em></h1>
      <div class="meta" data-stats>—</div>
    </header>

    <div class="section-head">
      <h2 data-sortlabel>${SORT_LABELS[currentSort]}</h2>
      <span class="rule"></span>
      <span class="count">SORT</span>
      <select data-sort class="btn ghost" style="background:transparent;border:1px solid var(--rule);color:var(--ink);padding:.4rem .6rem">
        ${(Object.keys(SORT_LABELS) as SortMode[]).map(k => `
          <option value="${k}" ${k === currentSort ? "selected" : ""}>${SORT_LABELS[k]}</option>
        `).join("")}
      </select>
    </div>

    <div data-grid><div class="loading">Loading albums</div></div>
  `;

  const sortEl = host.querySelector<HTMLSelectElement>("[data-sort]")!;
  const sortLabel = host.querySelector<HTMLElement>("[data-sortlabel]")!;
  const grid = host.querySelector<HTMLElement>("[data-grid]")!;

  sortEl.addEventListener("change", async () => {
    currentSort = sortEl.value as SortMode;
    sortLabel.textContent = SORT_LABELS[currentSort];
    await load(grid);
  });

  await load(grid);
}

async function load(grid: HTMLElement): Promise<void> {
  grid.innerHTML = `<div class="loading">Loading albums</div>`;
  let albums: SubsonicAlbum[];
  try {
    albums = await getAlbumList(currentSort, 60);
  } catch (e) {
    grid.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }
  if (albums.length === 0) {
    grid.innerHTML = `<div class="empty">No albums yet — scan your library to populate it.</div>`;
    return;
  }
  grid.innerHTML = `
    <div class="album-grid stagger">
      ${albums.map(a => albumCardHtml(a)).join("")}
    </div>
  `;
}

export function albumCardHtml(a: SubsonicAlbum): string {
  const art = coverArtUrl(a.coverArt, 320);
  const placeholder = albumPlaceholder(a.name);
  return `
    <a class="album-card" href="#/album/${encodeURIComponent(a.id)}">
      <div class="art" ${art ? `style="background-image:url('${art}')"` : ""}>
        ${art ? "" : `<div class="placeholder">${escapeHtml(placeholder)}</div>`}
      </div>
      <div class="title">${escapeHtml(a.name)}${a.year ? ` <span class="year">${a.year}</span>` : ""}</div>
      <div class="artist">${escapeHtml(a.artist ?? "Unknown artist")}</div>
    </a>
  `;
}
