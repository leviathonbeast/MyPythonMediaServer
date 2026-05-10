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

const PAGE_SIZE = 40;

let currentSort: SortMode = "newest";
let currentOffset = 0;

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
    currentOffset = 0;
    await load(grid, true);
  });

  currentOffset = 0;
  await load(grid, true);
}

async function load(grid: HTMLElement, replace: boolean): Promise<void> {
  if (replace) {
    grid.innerHTML = `<div class="loading">Loading albums</div>`;
  } else {
    const btn = grid.querySelector<HTMLButtonElement>("[data-load-more]");
    if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
  }

  let albums: SubsonicAlbum[];
  try {
    albums = await getAlbumList(currentSort, PAGE_SIZE, currentOffset);
  } catch (e) {
    if (replace) grid.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    else {
      const btn = grid.querySelector<HTMLButtonElement>("[data-load-more]");
      if (btn) { btn.disabled = false; btn.textContent = "Load more"; }
    }
    return;
  }

  if (replace && albums.length === 0) {
    grid.innerHTML = `<div class="empty">No albums yet — scan your library to populate it.</div>`;
    return;
  }

  currentOffset += albums.length;
  const hasMore = albums.length === PAGE_SIZE;

  if (replace) {
    grid.innerHTML = `<div class="album-grid stagger" data-cards></div>`;
  } else {
    grid.querySelector("[data-load-more]")?.remove();
  }

  const cards = grid.querySelector<HTMLElement>("[data-cards]")!;
  cards.insertAdjacentHTML("beforeend", albums.map(albumCardHtml).join(""));

  if (hasMore) {
    const btn = document.createElement("button");
    btn.className = "btn ghost";
    btn.dataset.loadMore = "";
    btn.style.cssText = "display:block;margin:2rem auto;padding:.6rem 2rem";
    btn.textContent = "Load more";
    btn.addEventListener("click", () => void load(grid, false));
    grid.appendChild(btn);
  }
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
