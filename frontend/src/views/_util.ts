// src/views/_util.ts
//
// Tiny helpers shared by view modules. Kept underscore-prefixed so it's
// obvious these aren't routes.

/** HTML-escape user-supplied text before interpolating into innerHTML. */
export function escapeHtml(s: string | null | undefined): string {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * Pick a 1–2 character placeholder for an album with no cover art.
 * We try to get the most distinctive opening character of the title.
 */
export function albumPlaceholder(name: string | undefined | null): string {
  if (!name) return "?";
  const trimmed = name.trim();
  if (!trimmed) return "?";
  // First word's first character — if it's a "The" we skip it.
  const words = trimmed.split(/\s+/);
  const first = words[0]?.toLowerCase() === "the" && words.length > 1 ? words[1] : words[0];
  return (first?.charAt(0) ?? "?").toUpperCase();
}

// Collaboration separators we'll split on. Case-insensitive, split-only —
// we drop the delimiter so "The Score & 2WEI" becomes ["The Score", "2WEI"].
//
// Conservative on purpose. We deliberately do NOT split on:
//
//   ","  — far too many legit artist names contain commas:
//           "Our Hollow, Our Home", "Earth, Wind & Fire",
//           "Crosby, Stills, Nash & Young". Without a name dictionary we
//           can't tell a comma-in-name from a comma-as-collab-separator,
//           and the cost of a false split (a band's discography becomes
//           unreachable from its own row) is much worse than the cost of
//           not splitting a rare "Artist A, Artist B" credit.
//   "/"  — used inside legitimate names like "AC/DC".
//   " - "— often part of a single name ("Tyler, the Creator - Album" etc.)
//
// What we DO split on:
//   ";"               — the canonical ID3v2.4 multi-value delimiter, so
//                       seeing one is a strong signal of multiple artists.
//   "&"               — the most common collab separator in display tags.
//   " feat. " / " ft. " / " featuring " / " with " / " vs. " / " x "
//                     — word-form collab markers; the surrounding spaces
//                       prevent accidental matches inside ordinary words
//                       (e.g. " x " won't match "Malcolm X" since X is
//                       at end of string, not between two name words).
const ARTIST_SPLIT_RE =
  /\s*(?:;|&| feat\.? | featuring | ft\.? | with | vs\.? | x )\s*/i;

/**
 * Split a display string like "The Score & 2WEI" into ["The Score", "2WEI"].
 * Empty fragments are dropped.
 */
export function splitArtistNames(s: string | null | undefined): string[] {
  if (!s) return [];
  return s
    .split(ARTIST_SPLIT_RE)
    .map(p => p.trim())
    .filter(p => p.length > 0);
}

/**
 * Render an artist display string as one-or-more clickable links.
 *
 * If the string contains multiple artist names ("A & B", "A feat. B", etc.)
 * each name becomes a separate link. The piece that case-insensitively matches
 * the song/album's primary artist (when an id is known) deep-links to that
 * artist's page; the rest deep-link to a search prefilled with the name, since
 * we don't have ids for the featured artists from the Subsonic payload alone.
 */
export function renderArtistLinks(
  artist: string | null | undefined,
  artistId: string | null | undefined,
): string {
  const text = (artist ?? "").trim();
  if (!text) return "";

  const parts = splitArtistNames(text);

  // Single name — keep the existing behavior so we don't regress simple cases.
  if (parts.length <= 1) {
    if (artistId) {
      return `<a href="#/artist/${encodeURIComponent(artistId)}">${escapeHtml(text)}</a>`;
    }
    return escapeHtml(text);
  }

  const primary = artistId ? (artist ?? "").toLowerCase().trim() : null;
  // Pick exactly one fragment to claim the artistId — prefer an exact (case-
  // insensitive) match against any single piece. If nothing matches we fall
  // back to the first piece, which is the conventional "main" credit.
  let claimedIdx = -1;
  if (artistId) {
    claimedIdx = parts.findIndex(p => p.toLowerCase() === primary);
    if (claimedIdx === -1) claimedIdx = 0;
  }

  const links = parts.map((p, i) => {
    if (i === claimedIdx && artistId) {
      return `<a href="#/artist/${encodeURIComponent(artistId)}">${escapeHtml(p)}</a>`;
    }
    return `<a href="#/search/${encodeURIComponent(p)}">${escapeHtml(p)}</a>`;
  });
  // Re-join with a thin separator that matches typical display ("A, B, C").
  return links.join(", ");
}
