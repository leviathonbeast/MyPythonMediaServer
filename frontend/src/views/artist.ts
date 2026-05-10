// src/views/artist.ts
//
// Artist page — a richer take than the original getMusicDirectory dump.
//
// Sources data from the custom /api/artist/{id} endpoint, which returns:
//   - artist name + total album count
//   - albums grouped by release_type (album / EP / single / compilation / other)
//   - an optional Last.fm bio (when MUSE_LASTFM_API_KEY is configured)
//
// We render:
//   - A "masthead" header with the artist name and (when available) a
//     hero image from Last.fm. Falls back to typography-only when no
//     image is configured — keeps the editorial feel even on dry libraries.
//   - A bio paragraph, with a "more on Last.fm" link to the source.
//   - Album sections, in this order, skipping any that are empty:
//     Albums, EPs, Singles, Compilations, Other.

import { getArtistDetail, coverArtUrl, type ArtistAlbum } from "../api";
import { albumCardHtml } from "./albums";
import { escapeHtml } from "./_util";

const SECTION_LABELS: Array<[keyof ArtistDetailGrouped, string]> = [
  ["albums",       "Albums"],
  ["eps",          "EPs"],
  ["singles",      "Singles"],
  ["compilations", "Compilations"],
  ["other",        "Other"],
];

// Local re-statement of the grouped shape (keeps this view tied to the
// API contract without re-importing the full ArtistDetail type).
interface ArtistDetailGrouped {
  albums: ArtistAlbum[];
  eps: ArtistAlbum[];
  singles: ArtistAlbum[];
  compilations: ArtistAlbum[];
  other: ArtistAlbum[];
}

export async function renderArtist(host: HTMLElement, id: string): Promise<void> {
  host.innerHTML = `<div class="loading">Loading artist</div>`;
  try {
    const data = await getArtistDetail(id);
    const grouped = data.albums_grouped as ArtistDetailGrouped;

    const totalAlbums =
      grouped.albums.length + grouped.eps.length +
      grouped.singles.length + grouped.compilations.length +
      grouped.other.length;

    host.innerHTML = `
      ${heroHtml(data.name, data.bio?.image_url ?? null, totalAlbums)}
      ${data.bio ? bioHtml(data.bio) : ""}
      ${SECTION_LABELS.map(([key, label]) => {
        const items = grouped[key] ?? [];
        if (items.length === 0) return "";
        return sectionHtml(label, items);
      }).join("")}
      ${totalAlbums === 0 ? `<div class="empty">No albums for this artist.</div>` : ""}
    `;
  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load artist: ${escapeHtml((e as Error).message)}</div>`;
  }
}

function heroHtml(name: string, imageUrl: string | null, albumCount: number): string {
  // Two layouts: with image (split column) and without (typography hero).
  // The image-based version uses the Last.fm artist photo as a backdrop.
  if (imageUrl) {
    return `
      <header class="page-head stagger" style="grid-template-columns:280px 1fr 1fr;align-items:stretch;border-bottom:none;padding-bottom:0;margin-bottom:var(--gap-8);gap:var(--gap-8)">
        <div style="aspect-ratio:1;background:var(--bg-2) center/cover no-repeat url('${escapeHtml(imageUrl)}');border:1px solid var(--rule);grid-row:span 2"></div>
        <div style="grid-column:span 2;border-bottom:2px solid var(--ink);padding-bottom:var(--gap-4);display:flex;flex-direction:column;justify-content:flex-end">
          <span class="label">— Artist</span>
          <h1 style="font-family:var(--font-display);font-variation-settings:'opsz' 144,'SOFT' 50,'WONK' 1;font-weight:400;font-size:var(--t-mast);letter-spacing:-0.035em;line-height:0.95;margin:.5rem 0 0">${escapeHtml(name)}</h1>
          <div class="meta" style="margin-top:.75rem">${albumCount} release${albumCount === 1 ? "" : "s"}</div>
        </div>
      </header>
    `;
  }
  return `
    <header class="page-head stagger">
      <h1>${escapeHtml(name)}</h1>
      <div class="meta">
        — Artist<br/>
        ${albumCount} release${albumCount === 1 ? "" : "s"}
      </div>
    </header>
  `;
}

function bioHtml(bio: { summary: string; content: string; url: string | null; tags: string[] }): string {
  // Prefer the longer `content` if the summary is just a sentence; cap
  // at a generous-but-not-endless ~600 characters. Last.fm's full
  // articles can run to a few paragraphs and crowd out the album grid.
  const maxLen = 600;
  let text = bio.content || bio.summary;
  if (!text) return "";
  let truncated = false;
  if (text.length > maxLen) {
    text = text.slice(0, maxLen).replace(/\s+\S*$/, "") + "…";
    truncated = true;
  }

  const tagsHtml = bio.tags.length > 0
    ? `<div style="margin-top:.75rem;display:flex;gap:.5rem;flex-wrap:wrap">
         ${bio.tags.map(t => `
           <span class="label" style="border:1px solid var(--rule);padding:.25rem .6rem;color:var(--muted);font-size:var(--t-micro)">${escapeHtml(t)}</span>
         `).join("")}
       </div>`
    : "";

  const moreLink = (bio.url && (truncated || bio.content.length > maxLen))
    ? ` <a href="${escapeHtml(bio.url)}" target="_blank" rel="noopener noreferrer"
           style="color:var(--accent);font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;margin-left:.5rem">
         more on last.fm →
       </a>`
    : "";

  return `
    <section class="stagger" style="max-width:70ch;margin-bottom:var(--gap-8)">
      <span class="label">— Bio</span>
      <p style="font-family:var(--font-display);font-size:1.0625rem;line-height:1.65;color:var(--ink);margin-top:.5rem">
        ${escapeHtml(text)}${moreLink}
      </p>
      ${tagsHtml}
    </section>
  `;
}

function sectionHtml(label: string, items: ArtistAlbum[]): string {
  return `
    <div class="section-head">
      <h2>${escapeHtml(label)}</h2>
      <span class="rule"></span>
      <span class="count">${items.length}</span>
    </div>
    <div class="album-grid stagger">
      ${items.map(a => albumCardHtml({
        id: a.id,
        name: a.name,
        artist: a.artist,
        coverArt: a.coverArt ?? undefined,
        year: a.year ?? undefined,
      })).join("")}
    </div>
  `;
}

// Suppress an unused-import warning while keeping the helper available
// for a future "show cover-art if no Last.fm image" fallback path.
void coverArtUrl;
