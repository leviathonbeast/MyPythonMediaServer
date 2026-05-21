// src/views/track.ts
//
// Single-track view — the page you see when you deep-link to a song
// (URL: #/track/<id>). Modeled after Apple Music's song page:
//   - Hero with cover, title, artist, and metadata stack
//   - "From the Album" — one card linking to the parent album
//   - "More by Artist" — horizontally scrolling row of the artist's
//     other releases
//
// Data sourcing:
//   We need TWO things from the server: the song itself, and the artist's
//   other albums. Crucially we do NOT need to call getAlbum() — the song's
//   own response already contains everything required to render a single
//   "from this album" card (album name, album id, cover art id). One less
//   round trip, simpler code.
//
//   The two calls fire in parallel via Promise.all so the total wait is
//   max(song_latency, artist_latency), not sum. On a LAN both finish in
//   well under 50ms.

import {
  getSong,
  getArtistDetail,
  getSonicSimilarTracks,
  coverArtUrl,
  type SubsonicAlbum,
  type SubsonicSong,
  type ArtistAlbum,
} from "../api";
import { player, fmtDuration } from "../player";
import { albumCardHtml } from "./albums";
import { escapeHtml, albumPlaceholder, renderArtistLinks } from "./_util";
import { pickPlaylistAndAdd } from "./_playlist_picker";

export async function renderTrack(host: HTMLElement, id: string): Promise<void> {
  // Loading state — set immediately so users see *something* during the
  // (brief) network round trip rather than a blank page.
  host.innerHTML = `<div class="loading">Loading track</div>`;

  try {
    // Fetch the song first because we need its artistId before we can
    // ask for artist detail. This is the one place we *can't* parallelize:
    // the artist call depends on data the song call returns.
    const song = await getSong(id);
    song.lyrics = "[00:01.00] Hello world\n[00:03.00] This is a test\nNo timestamp here";  // TEMP

    // The artist "More by" row and the "Sonic similar" row both depend only
    // on the song we just fetched, so fire them in parallel. Each can fail
    // (artist lookup error, or no feature vectors yet) without breaking the
    // page — they just render nothing.
    const [artistDetail, similar] = await Promise.all([
      song.artistId
        ? getArtistDetail(song.artistId).catch(() => null)
        : Promise.resolve(null),
      getSonicSimilarTracks(id, 12).catch(() => [] as SubsonicSong[]),
    ]);

    // ---- Render --------------------------------------------------------
    // Sections, top to bottom. Each section is its own helper so the main
    // function reads as a table of contents. Sections that have no data
    // to show (e.g. lyrics on a track without them) return "" and are
    // visually absent — no empty headers floating around.
    host.innerHTML = `
      <div class="stagger">
        ${heroHtml(song)}
        ${fromAlbumHtml(song)}
        ${lyricsHtml(song)}
        ${sonicSimilarHtml(similar)}
        ${moreByArtistHtml(song, artistDetail)}

      </div>
    `;

    // ---- Wire up the play / queue buttons ------------------------------
    // Both helpers return arrays because the player's playQueue() and
    // enqueue() take *arrays* of songs. For a single-track page the array
    // has exactly one element.
    host.querySelector<HTMLButtonElement>("[data-play]")
        ?.addEventListener("click", () => player.playQueue([song], 0));
    host.querySelector<HTMLButtonElement>("[data-queue]")
        ?.addEventListener("click", () => player.enqueue([song]));
    host.querySelector<HTMLButtonElement>("[data-add-to-playlist]")
        ?.addEventListener("click", async () => {
          const r = await pickPlaylistAndAdd([String(song.id)]);
          // Surface failures only; success is implicit. Switch to a toast
          // component here when one lands.
          if (!r.added && r.message) window.alert(r.message);
        });

    // ---- Sonic-similar row: play-all + click-a-row-to-play -------------
    // Clicking a row plays the whole similar set starting at that track, so
    // the rest queues up as a "more like this" radio.
    host.querySelector<HTMLButtonElement>("[data-similar-playall]")
        ?.addEventListener("click", () => player.playQueue(similar, 0));
    host.querySelector<HTMLTableSectionElement>("[data-similar] tbody")
        ?.addEventListener("click", (e) => {
          const target = e.target as HTMLElement;
          if (target.closest("a")) return;  // let artist/album links navigate
          const tr = target.closest("tr");
          if (!tr) return;
          const idx = Number(tr.dataset.idx);
          if (Number.isFinite(idx)) player.playQueue(similar, idx);
        });

  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load track: ${escapeHtml((e as Error).message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Hero — type label, title, artist line, and the cover+meta+actions block.
//
// Visually closest to Apple Music's song page header. We keep it
// typographically loud at the top (giant Fraunces title) and let the
// metadata sit quietly to the right of the cover.
// ---------------------------------------------------------------------------
function heroHtml(song: SubsonicSong): string {
  const art = coverArtUrl(song.coverArt, 600);
  const placeholder = albumPlaceholder(song.album ?? song.title);

  // Subline values — joined with " · " separators, but only the ones
  // that actually have data. Skipping empties keeps the line tight on
  // sparse libraries (a single FLAC with no year, no genre, etc.).
  const subline: string[] = [];
  if (song.album)    subline.push(song.album);
  if (song.year)     subline.push(String(song.year));
  if (song.duration) subline.push(fmtDuration(song.duration));

  return `
    <header class="track-hero">
      <span class="label">— Song${song.duration ? ` · ${escapeHtml(fmtDuration(song.duration))}` : ""}</span>
      <h1 class="track-title">${escapeHtml(song.title)}</h1>
      <div class="track-artist">
        ${renderArtistLinks(song.artist ?? "Unknown", song.artistId)}
      </div>

      <div class="track-hero-body">
        <div class="art" ${art
            ? `style="background-image:url('${art}')"`
            : `style="display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-size:6rem;color:var(--muted-2)"`
          }>
          ${art ? "" : escapeHtml(placeholder)}
        </div>

        <div class="track-hero-info">
          <div class="actions">
            <button class="btn primary" data-play>▶ Play</button>
            <button class="btn ghost" data-queue>+ Queue</button>
            <button class="btn ghost" data-add-to-playlist>+ Playlist</button>
          </div>

          <dl class="track-meta">
            ${metaRow("Album",
                song.albumId
                  ? `<a href="#/album/${encodeURIComponent(song.albumId)}">${escapeHtml(song.album ?? "—")}</a>`
                  : escapeHtml(song.album ?? "—"))}
            ${song.track ? metaRow("Track", String(song.track)) : ""}
            ${song.genre ? metaRow("Genre", `<a href="#/genre/${encodeURIComponent(song.genre)}">${escapeHtml(song.genre)}</a>`) : ""}
            ${song.year ? metaRow("Released", String(song.year)) : ""}
            ${song.suffix ? metaRow("Format", `${escapeHtml(song.suffix.toUpperCase())}${song.bitRate ? ` · ${song.bitRate} kbps` : ""}`) : ""}
            ${song.playCount !== undefined ? metaRow("Play Count", String(song.playCount)): ""}
          </dl>
        </div>
      </div>
    </header>
  `;
}

// One row inside the metadata `<dl>`. <dt>/<dd> is semantically right
// for a label/value pair; CSS turns them into the two-column grid.
function metaRow(label: string, value: string): string {
  return `
    <div class="track-meta-row">
      <dt class="label">— ${escapeHtml(label)}</dt>
      <dd>${value}</dd>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// "From the Album" — a single album card linking back.
//
// We deliberately do NOT call getAlbum() here. The song response already
// has everything we need to render the card: albumId for the link,
// album name, coverArt for the image. Constructing a SubsonicAlbum-shaped
// object from these few fields and passing it through the existing
// albumCardHtml() helper keeps the implementation trivial.
// ---------------------------------------------------------------------------
function fromAlbumHtml(song: SubsonicSong): string {
  if (!song.albumId || !song.album) return "";  // unlikely, but be safe

  // Synthesize a minimal SubsonicAlbum the card helper can render.
  // We're missing a few optional fields (genre, songCount, duration)
  // but the card only displays name + artist + year + cover, all of
  // which we have or can omit gracefully.
  const album: SubsonicAlbum = {
    id:       song.albumId,
    name:     song.album,
    artist:   song.artist ?? "",
    artistId: song.artistId,
    coverArt: song.coverArt,
    year:     song.year,
  };

  return `
    <section style="margin-top:var(--gap-12)">
      <div class="section-head">
        <h2>From the Album</h2>
        <span class="rule"></span>
      </div>
      <div class="album-grid" style="grid-template-columns:repeat(auto-fill, minmax(200px, 240px))">
        ${albumCardHtml(album)}
      </div>
    </section>
  `;
}

// ---------------------------------------------------------------------------
// Lyrics
//
// Renders the lyrics section if the track has any. Two formats supported:
//
//   1. Plain text — just the words, no timestamps. Render as paragraphs.
//   2. LRC — synced format with timestamp markers like
//        [00:29.06] Once upon a time...
//      We parse the timestamps out and render the lines plain. The data
//      is structured (LyricLine[] with `time` and `text`) so a future
//      "highlight current line as the song plays" implementation only
//      needs to: (a) subscribe to player.subscribe, (b) find the line
//      where time <= currentTime < nextTime, (c) toggle a `.active`
//      class. ~15 extra lines, no new deps.
//
// Until the scanner starts extracting lyrics tags, song.lyrics will be
// undefined for everything and this section will be invisible. We render
// nothing (empty string) in that case rather than a "no lyrics" empty
// state — empty states for not-yet-implemented features feel like bugs.
// ---------------------------------------------------------------------------

interface LyricLine {
  /** Seconds into the song this line starts. -1 for plain-text (untimed) lyrics. */
  time: number;
  text: string;
}

function lyricsHtml(song: SubsonicSong): string {
  const raw = song.lyrics?.trim();
  if (!raw) return "";  // no lyrics tagged → no section

  const lines = parseLyrics(raw);
  if (lines.length === 0) return "";

  const synced = lines.some(l => l.time >= 0);

  return `
    <section style="margin-top:var(--gap-12)">
      <div class="section-head">
        <h2>Lyrics</h2>
        <span class="rule"></span>
        ${synced ? `<span class="count">synced</span>` : ""}
      </div>
      <div class="lyrics" data-lyrics ${synced ? `data-synced="true"` : ""}>
        ${lines.map((l, i) => `
          <div class="lyric-line"
               data-time="${l.time >= 0 ? l.time : ""}"
               data-idx="${i}">${escapeHtml(l.text || "\u00a0")}</div>
        `).join("")}
      </div>
    </section>

    <!--
      Future karaoke-style sync hook:
      When ready to implement, subscribe to the player here and toggle
      .lyric-line.active on the row whose data-time matches the current
      playback position. Pseudo-code:

        const lines = host.querySelectorAll('.lyric-line[data-time]');
        const times = [...lines].map(l => Number(l.dataset.time));
        player.subscribe(state => {
          const t = state.currentTime;
          const idx = times.findLastIndex(time => time <= t);
          lines.forEach((l, i) => l.classList.toggle('active', i === idx));
          // optional: scroll the active line into view
        });
    -->
  `;
}

/**
 * Parse a lyrics blob into structured lines.
 *
 * Detects LRC format by the presence of `[mm:ss.xx]` timestamps at line
 * starts. If no timestamps are found, treats the input as plain text
 * (one LyricLine per non-empty line, all with time = -1).
 *
 * Tolerates LRC metadata lines like `[ar:Artist]`, `[ti:Title]`,
 * `[al:Album]`, `[length:02:27]` — these are skipped.
 *
 * Some LRC files use multiple timestamps for the same line (chorus
 * repeats). We expand those into separate entries so each repetition
 * highlights at the right time.
 */
function parseLyrics(raw: string): LyricLine[] {
  // Capture mm:ss(.xx)? at the start of a line, optionally repeated.
  const TIMESTAMP_RE = /\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]/g;
  // LRC metadata tags: [ar:..], [ti:..], [length:..], [offset:..], etc.
  // These have an alpha key, not digits, so the timestamp regex skips
  // them naturally — but we test the line for "is this content?" too.
  const META_RE = /^\[[a-z]+:/i;

  const out: LyricLine[] = [];
  let sawAnyTimestamp = false;

  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (META_RE.test(line)) continue;  // skip LRC metadata

    // Pull out all timestamps on this line.
    const timestamps: number[] = [];
    TIMESTAMP_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = TIMESTAMP_RE.exec(line)) !== null) {
      const mins = Number(m[1]);
      const secs = Number(m[2]);
      const frac = m[3] ? Number(`0.${m[3]}`) : 0;
      timestamps.push(mins * 60 + secs + frac);
    }

    // Strip timestamps from the text portion.
    const text = line.replace(TIMESTAMP_RE, "").trim();

    if (timestamps.length > 0) {
      sawAnyTimestamp = true;
      // Expand multi-timestamp lines into individual entries.
      for (const t of timestamps) {
        out.push({ time: t, text });
      }
    } else {
      // No timestamp on this line — keep as untimed.
      out.push({ time: -1, text });
    }
  }

  // If we found at least one timestamp, the file is LRC; sort by time.
  // Untimed lines (e.g. an "Instrumental" intro before the first stamped
  // line) sort to the front and are rendered as static text.
  if (sawAnyTimestamp) {
    out.sort((a, b) => a.time - b.time);
  }

  return out;
}


// ---------------------------------------------------------------------------
// "More by Artist" — horizontally-scrolling row of the artist's other
// releases. We pull from artistDetail.albums_grouped, flattening all
// release types (albums, EPs, singles, compilations, other) into one
// row in their natural year order.
//
// Apple Music includes the current album in this row; we follow suit.
// Filtering the current album out feels like an obvious nicety but
// breaks visually when the artist has only one release — better to be
// consistent.
// ---------------------------------------------------------------------------
function moreByArtistHtml(
  song: SubsonicSong,
  artistDetail: Awaited<ReturnType<typeof getArtistDetail>> | null,
): string {
  if (!artistDetail) return "";

  // Flatten all release-type buckets back into a single chronological list.
  const grouped = artistDetail.albums_grouped;
  const allAlbums: ArtistAlbum[] = [
    ...grouped.albums,
    ...grouped.eps,
    ...grouped.singles,
    ...grouped.compilations,
    ...grouped.other,
  ];

  // If the artist has no other releases (single-album artist with their
  // only album already visible above), skip the section entirely.
  if (allAlbums.length === 0) return "";

  return `
    <section style="margin-top:var(--gap-12)">
      <div class="section-head">
        <h2>More by ${escapeHtml(song.artist ?? artistDetail.name)}</h2>
        <span class="rule"></span>
        <span class="count">${allAlbums.length}</span>
      </div>
      <div class="scroll-row">
        ${allAlbums.map(a => albumCardHtml({
          // ArtistAlbum is shaped slightly differently from SubsonicAlbum
          // (release_type field, nullable year/genre). We map only the
          // fields albumCardHtml uses.
          id:       a.id,
          name:     a.name,
          artist:   a.artist,
          artistId: a.artistId,
          coverArt: a.coverArt ?? undefined,
          year:     a.year ?? undefined,
        })).join("")}
      </div>
    </section>
  `;
}

// ---------------------------------------------------------------------------
// "Sonically similar" — tracks near this one in the librosa feature space,
// from the getSonicSimilarTracks endpoint.
//
// Renders nothing when empty. That happens whenever this track (or the
// library) hasn't been analysed yet — and an empty "no similar tracks"
// state would read as a bug rather than "feature not populated". The
// analysis pass is discoverable from Settings, which is the right home for
// the "make this work" action.
// ---------------------------------------------------------------------------
function sonicSimilarHtml(similar: SubsonicSong[]): string {
  if (similar.length === 0) return "";

  return `
    <section style="margin-top:var(--gap-12)">
      <div class="section-head">
        <h2>Sonically Similar</h2>
        <span class="rule"></span>
        <span class="count">${similar.length}</span>
      </div>
      <div style="margin-bottom:1rem">
        <button class="btn ghost" data-similar-playall>▶ Play all</button>
      </div>
      <table class="tracklist" data-similar>
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
          ${similar.map((s, i) => similarRowHtml(s, i)).join("")}
        </tbody>
      </table>
    </section>
  `;
}

function similarRowHtml(s: SubsonicSong, idx: number): string {
  const artistCell = renderArtistLinks(s.artist, s.artistId);
  const albumCell = s.albumId
    ? `<a href="#/album/${encodeURIComponent(s.albumId)}">${escapeHtml(s.album ?? "")}</a>`
    : escapeHtml(s.album ?? "");
  return `
    <tr data-idx="${idx}" style="cursor:pointer">
      <td class="num">${idx + 1}</td>
      <td class="title">${escapeHtml(s.title)}</td>
      <td>${artistCell}</td>
      <td>${albumCell}</td>
      <td class="duration">${fmtDuration(s.duration)}</td>
    </tr>
  `;
}