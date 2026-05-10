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
