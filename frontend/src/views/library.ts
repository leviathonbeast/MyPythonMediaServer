// src/views/library.ts
//
// The browse landing — an A–Z artist index built from /rest/getIndexes.
// Two view modes (list / grid) persisted in localStorage.
// An alphabet strip at the top scrolls to each letter section.

import { getIndexes, type SubsonicArtist, type SubsonicIndex } from "../api";
import { escapeHtml } from "./_util";

const BUCKET_LIMIT = 5;
const VIEW_KEY = "muse.library.view";
const ALL_LETTERS = ["#", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"];

type ViewMode = "list" | "grid";

function getView(): ViewMode {
  return (localStorage.getItem(VIEW_KEY) as ViewMode) ?? "list";
}
function saveView(v: ViewMode): void {
  localStorage.setItem(VIEW_KEY, v);
}

export async function renderLibrary(host: HTMLElement): Promise<void> {
  host.innerHTML = `
    <header class="page-head">
      <h1>The <em>library</em></h1>
      <div class="meta">— Index of artists<br/>Sorted A–Z</div>
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

  const activeLetters = new Set(buckets.map(b => b.name));
  let view = getView();

  const toolbar = buildToolbar(activeLetters, view);
  const sections = document.createElement("div");
  sections.className = "lib-sections";

  content.innerHTML = "";
  content.appendChild(toolbar);
  content.appendChild(sections);

  function render(): void {
    sections.innerHTML = "";
    for (const bucket of buckets) {
      const artists = bucket.artist ?? [];
      sections.appendChild(
        view === "grid"
          ? buildGridSection(bucket.name, artists)
          : buildListSection(bucket.name, artists),
      );
    }
    toolbar.querySelectorAll<HTMLButtonElement>("[data-view]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
  }

  toolbar.addEventListener("click", e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-view]");
    if (!btn?.dataset.view) return;
    view = btn.dataset.view as ViewMode;
    saveView(view);
    render();
  });

  render();
}

// ---------------------------------------------------------------------------
// Toolbar — alphabet strip + view toggle
// ---------------------------------------------------------------------------

function buildToolbar(activeLetters: Set<string>, view: ViewMode): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "lib-toolbar";

  // Alphabet strip
  const nav = document.createElement("nav");
  nav.className = "alpha-nav";
  for (const letter of ALL_LETTERS) {
    const btn = document.createElement("button");
    btn.className = "alpha-btn";
    btn.textContent = letter;
    if (activeLetters.has(letter)) {
      btn.addEventListener("click", () => {
        document.getElementById(`bucket-${letter}`)
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } else {
      btn.disabled = true;
    }
    nav.appendChild(btn);
  }
  bar.appendChild(nav);

  // View toggle
  const toggle = document.createElement("div");
  toggle.className = "view-toggle";
  toggle.innerHTML = `
    <button class="btn ghost${view === "list" ? " active" : ""}" data-view="list">List</button>
    <button class="btn ghost${view === "grid" ? " active" : ""}" data-view="grid">Grid</button>
  `;
  bar.appendChild(toggle);

  return bar;
}

// ---------------------------------------------------------------------------
// List mode — per-letter sections with collapse
// ---------------------------------------------------------------------------

function buildListSection(letter: string, artists: SubsonicArtist[]): HTMLElement {
  const section = document.createElement("section");
  section.className = "index-bucket";
  section.id = `bucket-${letter}`;

  section.innerHTML = `
    <div class="head">
      <span class="letter">${escapeHtml(letter)}</span>
      <span class="count">${artists.length} artist${artists.length === 1 ? "" : "s"}</span>
    </div>
  `;

  const ul = document.createElement("ul");
  ul.innerHTML = artists.slice(0, BUCKET_LIMIT).map(artistRowHtml).join("");
  section.appendChild(ul);

  if (artists.length > BUCKET_LIMIT) {
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
  }

  return section;
}

function artistRowHtml(a: SubsonicArtist): string {
  return `<li><a href="#/artist/${encodeURIComponent(a.id)}">
    <span>${escapeHtml(a.name)}</span>
    <span class="albums">${a.albumCount ?? ""}</span>
  </a></li>`;
}

// ---------------------------------------------------------------------------
// Grid mode — poster cards grouped by letter
// ---------------------------------------------------------------------------

function buildGridSection(letter: string, artists: SubsonicArtist[]): HTMLElement {
  const section = document.createElement("section");
  section.className = "lib-grid-section";
  section.id = `bucket-${letter}`;

  const head = document.createElement("div");
  head.className = "lib-grid-head";
  head.innerHTML = `
    <span class="letter">${escapeHtml(letter)}</span>
    <span class="rule"></span>
    <span class="count">${artists.length} artist${artists.length === 1 ? "" : "s"}</span>
  `;
  section.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "artist-grid";
  grid.innerHTML = artists.slice(0, BUCKET_LIMIT).map(artistCardHtml).join("");
  section.appendChild(grid);

  if (artists.length > BUCKET_LIMIT) {
    const overflow = document.createElement("div");
    overflow.className = "artist-grid";
    overflow.innerHTML = artists.slice(BUCKET_LIMIT).map(artistCardHtml).join("");
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
  }

  return section;
}

function artistInitials(name: string): string {
  const words = name.replace(/^(the|a|an)\s+/i, "").split(/\s+/).filter(Boolean);
  return words.slice(0, 2).map(w => w[0].toUpperCase()).join("");
}

function artistCardHtml(a: SubsonicArtist): string {
  const initials = escapeHtml(artistInitials(a.name));
  const albums = a.albumCount ?? 0;
  return `
    <a class="artist-card" href="#/artist/${encodeURIComponent(a.id)}">
      <div class="art"><div class="placeholder">${initials}</div></div>
      <div class="name">${escapeHtml(a.name)}</div>
      ${albums ? `<div class="albums">${albums} album${albums === 1 ? "" : "s"}</div>` : ""}
    </a>
  `;
}
