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

import { getArtistDetail, coverArtUrl, type ArtistAlbum, type SubsonicSong } from "../api";
import { albumCardHtml } from "./albums";
import { escapeHtml } from "./_util";
import { player, fmtDuration } from "../player";

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
    const appearances = data.appearances ?? [];

    const totalAlbums =
      grouped.albums.length + grouped.eps.length +
      grouped.singles.length + grouped.compilations.length +
      grouped.other.length;

    host.innerHTML = `
      ${heroHtml(
        data.name,
        // Prefer the local-cache form (immutable browser cache) when we have it.
        data.image_cover_art_id
          ? coverArtUrl(data.image_cover_art_id, 600)
          : (data.image_url ?? data.bio?.image_url ?? null),
        totalAlbums,
      )}
      ${data.bio ? bioHtml(data.bio) : ""}
      ${SECTION_LABELS.map(([key, label]) => {
        const items = grouped[key] ?? [];
        if (items.length === 0) return "";
        return sectionHtml(label, items);
      }).join("")}
      ${appearances.length > 0 ? appearancesHtml(appearances) : ""}
      ${totalAlbums === 0 && appearances.length === 0
        ? `<div class="empty">No albums for this artist.</div>`
        : ""}
    `;

    if (appearances.length > 0) wireAppearanceClicks(host, appearances);
  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load artist: ${escapeHtml((e as Error).message)}</div>`;
  }
}

function appearancesHtml(songs: SubsonicSong[]): string {
  return `
    <div class="section-head">
      <h2>Appears on</h2>
      <span class="rule"></span>
      <span class="count">${songs.length}</span>
    </div>
    <table class="tracklist stagger" data-appearances>
      <thead>
        <tr>
          <th class="num">#</th>
          <th>Title</th>
          <th>Album</th>
          <th class="duration">Time</th>
        </tr>
      </thead>
      <tbody>
        ${songs.map((s, i) => appearanceRowHtml(s, i)).join("")}
      </tbody>
    </table>
  `;
}

function appearanceRowHtml(s: SubsonicSong, idx: number): string {
  const albumLabel = s.year
    ? `${escapeHtml(s.album ?? "")} (${s.year})`
    : escapeHtml(s.album ?? "");
  const albumCell = s.albumId
    ? `<a href="#/album/${encodeURIComponent(s.albumId)}">${albumLabel}</a>`
    : albumLabel;
  return `
    <tr data-idx="${idx}" data-tid="${escapeHtml(String(s.id))}" style="cursor:pointer">
      <td class="num">${idx + 1}</td>
      <td class="title"><a href="#/track/${encodeURIComponent(s.id)}">${escapeHtml(s.title)}</a></td>
      <td>${albumCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
    </tr>
  `;
}

function wireAppearanceClicks(host: HTMLElement, songs: SubsonicSong[]): void {
  const tbody = host.querySelector<HTMLTableSectionElement>("[data-appearances] tbody");
  if (!tbody) return;
  tbody.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    // Let nested links (track / album navigation) work normally.
    if (target.closest("a")) return;
    const tr = target.closest("tr");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    if (Number.isFinite(idx)) player.playQueue(songs, idx);
  });
}

function heroHtml(name: string, imageUrl: string | null, albumCount: number): string {
  // Two layouts: with image (split column) and without (typography hero).
  // The image-based version uses the Last.fm artist photo as a backdrop.
  // Layout/responsive behavior lives in style.css (.artist-hero) so the
  // mobile collapse and font sizes can media-query cleanly.
  if (imageUrl) {
    return `
      <header class="artist-hero stagger">
        <div class="art" style="background-image:url('${escapeHtml(imageUrl)}')"></div>
        <div class="info">
          <span class="label">— Artist</span>
          <h1>${escapeHtml(name)}</h1>
          <div class="meta">${albumCount} release${albumCount === 1 ? "" : "s"}</div>
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

  // Last.fm exposes "tags" as genre-ish labels for the artist. They don't
  // necessarily match the user's library tags, but they're a reasonable
  // jump-off point — clicking one routes to the genre browse view, which
  // shows whatever songs/albums Muse has tagged that way locally (empty
  // page when the user's library doesn't carry that tag, which is fine
  // UX for a "try" link).
  const tagsHtml = bio.tags.length > 0
    ? `<div style="margin-top:.75rem;display:flex;gap:.5rem;flex-wrap:wrap">
         ${bio.tags.map(t => `
           <a href="#/genre/${encodeURIComponent(t)}"
              class="label"
              style="border:1px solid var(--rule);padding:.25rem .6rem;color:var(--muted);font-size:var(--t-micro);text-decoration:none">${escapeHtml(t)}</a>
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

