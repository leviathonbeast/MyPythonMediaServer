// src/views/library.ts
//
// The browse landing — an A–Z artist index built from /rest/getIndexes.
// This is what you see when you click "Library" in the nav.

import { getIndexes, type SubsonicIndex } from "../api";
import { escapeHtml } from "./_util";

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

  // Build buckets — each letter group becomes a column-like card.
  const html = `
    <div class="index-grid stagger">
      ${buckets.map(bucket => {
        const artists = bucket.artist ?? [];
        return `
          <section class="index-bucket">
            <div class="head">
              <span class="letter">${escapeHtml(bucket.name)}</span>
              <span class="count">${artists.length} artist${artists.length === 1 ? "" : "s"}</span>
            </div>
            <ul>
              ${artists.map(a => `
                <li>
                  <a href="#/artist/${encodeURIComponent(a.id)}">
                    <span>${escapeHtml(a.name)}</span>
                    <span class="albums">${a.albumCount ?? ""}</span>
                  </a>
                </li>
              `).join("")}
            </ul>
          </section>
        `;
      }).join("")}
    </div>
  `;
  content.innerHTML = html;
}
