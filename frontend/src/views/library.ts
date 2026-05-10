// src/views/library.ts
//
// The browse landing — an A–Z artist index built from /rest/getIndexes.
// This is what you see when you click "Library" in the nav.

import { getIndexes, type SubsonicIndex, type SubsonicArtist } from "../api";
import { escapeHtml } from "./_util";

const BUCKET_LIMIT = 5;

export async function renderLibrary(host: HTMLElement): Promise<void> {
  host.innerHTML = `
    <header class="page-head">
      <h1>The <em>library</em></h1>
      <div class="meta">
        — Index of artists<br/>
        Sorted A–Z
      </div>
    </header>
    <div data-content><div class="loading">Loading index</div></div>
  `;

  const content = host.querySelector<HTMLElement>("[data-content]")!;

  let indexes: { index: SubsonicIndex[] };
  try {
    indexes = await getIndexes();
  } catch (e) {
    content.innerHTML = `<div class="empty">Couldn't load library: ${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  const buckets = indexes.index ?? [];
  if (buckets.length === 0) {
    content.innerHTML = `
      <div class="empty">
        Your library is empty.<br/>
        Add music to the configured folder and run a scan from the sidebar.
      </div>
    `;
    return;
  }

  const grid = document.createElement("div");
  grid.className = "index-grid stagger";

  for (const bucket of buckets) {
    const artists = bucket.artist ?? [];
    grid.appendChild(buildBucket(bucket.name, artists));
  }

  content.innerHTML = "";
  content.appendChild(grid);
}

function artistRowHtml(a: SubsonicArtist): string {
  return `<li><a href="#/artist/${encodeURIComponent(a.id)}">
    <span>${escapeHtml(a.name)}</span>
    <span class="albums">${a.albumCount ?? ""}</span>
  </a></li>`;
}

function buildBucket(letter: string, artists: SubsonicArtist[]): HTMLElement {
  const section = document.createElement("section");
  section.className = "index-bucket";

  const head = document.createElement("div");
  head.className = "head";
  head.innerHTML = `
    <span class="letter">${escapeHtml(letter)}</span>
    <span class="count">${artists.length} artist${artists.length === 1 ? "" : "s"}</span>
  `;
  section.appendChild(head);

  const ul = document.createElement("ul");
  ul.innerHTML = artists.slice(0, BUCKET_LIMIT).map(artistRowHtml).join("");
  section.appendChild(ul);

  if (artists.length <= BUCKET_LIMIT) return section;

  // Overflow list — hidden until the toggle is clicked.
  const overflow = document.createElement("ul");
  overflow.innerHTML = artists.slice(BUCKET_LIMIT).map(artistRowHtml).join("");
  overflow.style.display = "none";
  section.appendChild(overflow);

  const remaining = artists.length - BUCKET_LIMIT;
  const btn = document.createElement("button");
  btn.className = "btn ghost bucket-more";
  btn.textContent = `+${remaining} more`;

  btn.addEventListener("click", () => {
    const expanded = overflow.style.display !== "none";
    overflow.style.display = expanded ? "none" : "";
    btn.textContent = expanded ? `+${remaining} more` : "Show less";
  });

  section.appendChild(btn);
  return section;
}
