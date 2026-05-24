// src/player.ts
//
// Singleton audio player.
//
// We keep this deliberately small — the browser already gives us a fully
// capable <audio> element with native HTTP Range / seek support. Our job:
//   1. Maintain a queue (an ordered list of tracks).
//   2. Expose play/pause/next/prev/seek operations.
//   3. Render the dock UI (title/artist/art/scrubber/volume).
//   4. Notify subscribers when the "now playing" track changes (so the
//      tracklist view can highlight the current row).
//
// WHY a custom event bus (instead of a framework reactive store):
//   We're vanilla TS by design. A 30-line pub/sub is enough and avoids
//   pulling in any state library; views simply call `player.subscribe(fn)`
//   and re-render the relevant pieces.

import type { SubsonicSong } from "./api";
import {
  subsonic, coverArtUrl, streamUrl, getTranscodingPolicy, getTranscodingPrefs,
  resolveStreamPlan, type TranscodingPolicy,
  getPlayQueueByIndex, savePlayQueueByIndex,
  getScrobbleThreshold,
  getLyricsBySongId, type SongLyrics,
  continueRadio,
} from "./api";

// Endless mode (autoplay). When on, the queue auto-extends with tracks
// similar to recent listening so it never runs dry.
//   * REFILL_WHEN_REMAINING — start fetching once this few tracks are left
//     after the current one, so the next song is ready before silence.
//   * REFILL_BATCH — how many to ask the server for each refill.
//   * REFILL_SEEDS — how many recent tracks to seed the similarity from
//     (current track + the ones just before it), so the radio follows the
//     session rather than a single song.
const AUTOPLAY_KEY = "muse.autoplay";
const REFILL_WHEN_REMAINING = 2;
const REFILL_BATCH = 20;
const REFILL_SEEDS = 5;

// Debounce before firing the now-playing ping. Burning Last.fm's 5/sec
// budget on rapid skips is wasteful and produces flickery "currently
// listening to X" updates on the user's profile; 2s is short enough
// to land before most users care, long enough to absorb a few
// next-button presses in a row.
const NOW_PLAYING_DEBOUNCE_MS = 2000;

type Listener = (state: PlayerState) => void;

export interface PlayerState {
  queue: SubsonicSong[];
  index: number;             // -1 = nothing loaded
  playing: boolean;
  currentTime: number;
  duration: number;
  volume: number;
  current: SubsonicSong | null;
  autoplay: boolean;         // endless mode on/off
}

class Player {
  private audio = new Audio();
  private queue: SubsonicSong[] = [];
  private index = -1;
  private listeners = new Set<Listener>();
  private scrobbled = false;
  // Persistence: skip save() while we're restoring from the server so we
  // don't clobber the saved state with a partial in-memory snapshot.
  private suppressSave = false;
  private positionTimer: number | null = null;
  private pendingRestorePosition = 0;
  // Pending now-playing ping. Cleared when the user changes tracks
  // before NOW_PLAYING_DEBOUNCE_MS elapses, so quick skips don't
  // generate stale "currently listening" entries on Last.fm.
  private nowPlayingTimer: number | null = null;
  // Endless mode: when true, the queue auto-extends near its end.
  private autoplay = false;
  // In-flight refill, shared so concurrent triggers (proactive + end-of-queue)
  // await the same request instead of firing duplicates.
  private refillPromise: Promise<void> | null = null;

  constructor() {
    // Restore volume from previous session if available.
    const savedVol = Number(localStorage.getItem("muse.volume") ?? "0.9");
    this.audio.volume = Number.isFinite(savedVol) ? savedVol : 0.9;
    // Restore endless-mode preference (per-browser, default off).
    this.autoplay = localStorage.getItem(AUTOPLAY_KEY) === "1";

    // We forward the small set of <audio> events we care about. Each one
    // pokes subscribers with the latest state.
    const fwd = () => this.emit();
    this.audio.addEventListener("play", () => { fwd(); this.startPositionTimer(); });
    this.audio.addEventListener("pause", () => {
      fwd();
      this.stopPositionTimer();
      void this.save();
    });
    this.audio.addEventListener("timeupdate", () => {
      fwd();
      const track = this.queue[this.index];
      const knownDuration = track?.duration ?? 0;
      // Threshold is read every tick rather than cached: it's a cheap
      // localStorage read, and reading lets the user change the setting
      // mid-track and have it take effect on the next tick without a
      // page reload.
      const threshold = getScrobbleThreshold();
      if (
        threshold > 0 &&                          // 0 = "never scrobble"
        !this.scrobbled &&
        knownDuration > 0 &&
        this.audio.currentTime / knownDuration >= threshold
      ) {
        this.scrobbled = true;
        this.triggerScrobble();
      }
    });
    this.audio.addEventListener("durationchange", fwd);
    this.audio.addEventListener("volumechange", () => {
      localStorage.setItem("muse.volume", String(this.audio.volume));
      fwd();
    });
    // When a track ends, advance the queue. At the tail: in endless mode try
    // one more refill then continue; otherwise stop. (Proactive refill in
    // playAt usually means we never actually hit the tail, but this covers a
    // slow/failed earlier fetch.)
    this.audio.addEventListener("ended", () => {
      if (this.index >= 0 && this.index < this.queue.length - 1) {
        this.playAt(this.index + 1);
      } else if (this.autoplay) {
        void this.maybeRefill().then(() => {
          if (this.index < this.queue.length - 1) {
            this.playAt(this.index + 1);
          } else {
            this.audio.pause();
            this.emit();
          }
        });
      } else {
        this.audio.pause();
        this.emit();
      }
    });
    // If the network coughs up, surface a console error and emit so the
    // UI can show the previous state rather than appearing frozen.
    this.audio.addEventListener("error", () => {
      console.error("Audio error", this.audio.error);
      this.emit();
    });
    // After the audio element has metadata for the restored track, seek to
    // the saved position. We can't do this synchronously in restore() because
    // the metadata isn't loaded yet.
    this.audio.addEventListener("loadedmetadata", () => {
      if (this.pendingRestorePosition > 0) {
        this.audio.currentTime = this.pendingRestorePosition;
        this.pendingRestorePosition = 0;
      }
    });
    // Persist on tab close so the saved position is current.
    window.addEventListener("pagehide", () => { void this.save(); });

    // Kick off async restore. Browsers will block autoplay so we just load
    // the track and seek to the saved position — user clicks play to resume.
    void this.restore();
  }

  /* ---------- queue management ---------- */

  /** Replace the queue and start playing at `startIndex`. */
  playQueue(songs: SubsonicSong[], startIndex = 0): void {
    if (songs.length === 0) return;
    this.queue = songs.slice();
    this.playAt(Math.max(0, Math.min(startIndex, songs.length - 1)));
  }

  /** Append to the existing queue without changing what's playing. */
  enqueue(songs: SubsonicSong[]): void {
    this.queue.push(...songs);
    this.emit();
    void this.save();
  }

  /** Jump to a specific index in the current queue. Used by the queue view. */
  jumpTo(i: number): void {
    if (i < 0 || i >= this.queue.length) return;
    this.playAt(i);
  }

  /* ---------- transport ---------- */

  toggle(): void {
    if (this.index < 0) return;
    if (this.audio.paused) {
      void this.audio.play();
    } else {
      this.audio.pause();
    }
  }

  next(): void {
    if (this.index < 0) return;
    if (this.index < this.queue.length - 1) this.playAt(this.index + 1);
  }

  prev(): void {
    if (this.index < 0) return;
    // Match common player behavior: if more than 3s in, restart current
    // track instead of jumping back. Only jump when near the start.
    if (this.audio.currentTime > 3 || this.index === 0) {
      this.audio.currentTime = 0;
    } else {
      this.playAt(this.index - 1);
    }
  }

  seek(seconds: number): void {
    if (Number.isFinite(seconds)) this.audio.currentTime = seconds;
  }

  setVolume(v: number): void {
    this.audio.volume = Math.max(0, Math.min(1, v));
  }

  private playAt(i: number): void {
    this.scrobbled = false
    this.index = i;
    const track = this.queue[i];
    // We point the <audio> element at the Subsonic stream URL. Range
    // requests, buffering and seek-while-buffering are all handled by
    // the browser + our server's range-aware streamer.
    this.audio.src = streamUrl(track.id);
    void this.audio.play().catch((err) => {
      console.warn("Play rejected (autoplay policy?)", err);
    });
    this.emit();
    void this.save();
    this.scheduleNowPlaying(track.id);
    // Proactively top up the queue when we're near its end, so endless mode
    // has the next track ready before this one finishes.
    void this.maybeRefill();
  }

  /* ---------- endless mode (autoplay) ---------- */

  /** Turn endless mode on/off. Persisted per-browser. Turning it on near the
   * end of the queue kicks off an immediate refill. */
  setAutoplay(on: boolean): void {
    this.autoplay = on;
    localStorage.setItem(AUTOPLAY_KEY, on ? "1" : "0");
    this.emit();
    if (on) void this.maybeRefill();
  }

  /** Fetch similar tracks and append them when the queue is running low.
   * Returns the in-flight request if one is already running, so the proactive
   * (playAt) and end-of-queue (ended) triggers can't double-fetch. */
  private maybeRefill(): Promise<void> {
    if (!this.autoplay) return Promise.resolve();
    if (this.refillPromise) return this.refillPromise;
    if (this.index < 0 || this.queue.length === 0) return Promise.resolve();
    const remaining = this.queue.length - 1 - this.index;
    if (remaining > REFILL_WHEN_REMAINING) return Promise.resolve();

    this.refillPromise = this.doRefill().finally(() => {
      this.refillPromise = null;
    });
    return this.refillPromise;
  }

  private async doRefill(): Promise<void> {
    // Seed from the most-recent tracks (current first, then the ones just
    // played) so suggestions follow the session, not just the last song.
    const seeds: string[] = [];
    for (let i = this.index; i >= 0 && seeds.length < REFILL_SEEDS; i--) {
      seeds.push(this.queue[i].id);
    }
    const exclude = this.queue.map(t => t.id);
    try {
      const fresh = await continueRadio(seeds, exclude, REFILL_BATCH);
      // Defensive de-dup against the live queue: it may have changed while the
      // request was in flight (user enqueued something, or a prior refill).
      const have = new Set(this.queue.map(t => t.id));
      const toAdd = fresh.filter(t => !have.has(t.id));
      if (toAdd.length > 0) {
        this.queue.push(...toAdd);
        this.emit();
        void this.save();
      }
    } catch (err) {
      // Best-effort: a failed refill just means the queue may end. Don't
      // surface it as an error — endless mode is a convenience.
      console.debug("endless refill failed:", err);
    }
  }

  /**
   * Schedule a Last.fm now-playing ping for `trackId`, replacing any
   * previously-scheduled one. The actual call fires after a short
   * debounce so a flurry of skip-button presses doesn't produce a
   * burst of "currently listening" updates on the user's profile.
   */
  private scheduleNowPlaying(trackId: string): void {
    if (this.nowPlayingTimer !== null) {
      clearTimeout(this.nowPlayingTimer);
    }
    this.nowPlayingTimer = window.setTimeout(() => {
      this.nowPlayingTimer = null;
      void subsonic("scrobble", { id: trackId, submission: "false" }).catch((err) => {
        // Best-effort: a missed now-playing ping is cosmetic, never
        // blocks playback. Log at debug so it doesn't clutter
        // production consoles.
        console.debug("nowPlaying failed:", err);
      });
    }, NOW_PLAYING_DEBOUNCE_MS);
  }

  /* ---------- persistence ---------- */

  private async restore(): Promise<void> {
    this.suppressSave = true;
    try {
      const pq = await getPlayQueueByIndex();
      const entries = pq?.entry ?? [];
      if (entries.length === 0) return;

      this.queue = entries;
      // Use the saved index directly — no findIndex on track id, so a
      // duplicate-track queue resumes on the right occurrence.
      const saved = pq?.currentIndex ?? 0;
      this.index = saved >= 0 && saved < this.queue.length ? saved : 0;

      const track = this.queue[this.index];
      if (track) {
        this.audio.src = streamUrl(track.id);
        // Convert ms → seconds. Applied in the 'loadedmetadata' handler since
        // currentTime can't be set until the audio has loaded enough metadata.
        this.pendingRestorePosition = Math.max(0, (pq?.position ?? 0) / 1000);
      }
      this.emit();
    } catch (err) {
      console.warn("Failed to restore play queue:", err);
    } finally {
      this.suppressSave = false;
    }
  }

  private async save(): Promise<void> {
    if (this.suppressSave || this.queue.length === 0) return;
    const ids = this.queue.map(t => t.id);
    // index is -1 when nothing has played yet; the byIndex spec wants a
    // valid index whenever the queue is non-empty, so pin to 0.
    const idx = this.index >= 0 && this.index < this.queue.length ? this.index : 0;
    const positionMs = Math.floor((this.audio.currentTime || 0) * 1000);
    try {
      await savePlayQueueByIndex(ids, idx, positionMs);
    } catch (err) {
      console.warn("savePlayQueueByIndex failed:", err);
    }
  }

  private startPositionTimer(): void {
    this.stopPositionTimer();
    // Save position every 10s during playback so a tab crash doesn't lose
    // more than ~10s of progress.
    this.positionTimer = window.setInterval(() => { void this.save(); }, 10_000);
  }

  private stopPositionTimer(): void {
    if (this.positionTimer != null) {
      clearInterval(this.positionTimer);
      this.positionTimer = null;
    }
  }

  private triggerScrobble(): void {
    const track = this.queue[this.index];
    if (!track) return;
    console.log("Scrobbling track:", track.id, track.title);
    void subsonic("scrobble", { id: track.id, submission: "true" }).catch((err) => {
      console.error("Scrobble failed:", err);
    // scrobble is best-effort — don't let it crash the player
  });
}

  /* ---------- subscriptions ---------- */

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.snapshot());
    return () => this.listeners.delete(fn);
  }

  snapshot(): PlayerState {
    const current = this.index >= 0 ? this.queue[this.index] : null;
    return {
      queue: this.queue,
      index: this.index,
      playing: !this.audio.paused && this.index >= 0,
      currentTime: this.audio.currentTime || 0,
      duration: Number.isFinite(this.audio.duration)
        ? this.audio.duration
        : (current?.duration ?? 0),
      volume: this.audio.volume,
      current,
      autoplay: this.autoplay,
    };
  }

  private emit(): void {
    const s = this.snapshot();
    for (const fn of this.listeners) fn(s);
  }
}

export const player = new Player();

/* ---------- dock rendering ---------- */

/** Minimal HTML-escape for text we inject via innerHTML (lyric lines). */
function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Format `123` → `2:03`. Used in tracklists and the player. */
export function fmtDuration(seconds: number | undefined | null): string {
  if (!seconds || !Number.isFinite(seconds)) return "—:—";
  const total = Math.floor(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/**
 * Mount the persistent player dock into a host element and wire it up.
 * Returns a function that unsubscribes / detaches (used on signout).
 */
export function mountPlayerDock(host: HTMLElement): () => void {
  host.classList.add("player");
  host.innerHTML = `
    <div class="queue-panel" data-queue-panel hidden>
      <div class="queue-header">
        <span class="queue-title">QUEUE</span>
        <button data-queue-close title="Close">×</button>
      </div>
      <div class="queue-list" data-queue-list></div>
    </div>
    <div class="lyrics-panel" data-lyrics-panel hidden>
      <div class="queue-header">
        <span class="queue-title">LYRICS</span>
        <button data-lyrics-close title="Close">×</button>
      </div>
      <div class="lyrics-body" data-lyrics-body></div>
    </div>
    <div class="now">
      <div class="art" data-art></div>
      <div class="meta">
        <div class="title" data-title>—</div>
        <div class="sub" data-sub>nothing playing</div>
        <div class="stream-badge" data-streambadge style="display:none;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.18em;text-transform:uppercase;margin-top:.25rem"></div>
      </div>
    </div>
    <div class="controls">
      <div class="row">
        <button data-prev title="Previous">◀◀ PREV</button>
        <button class="play" data-play title="Play / pause">PLAY</button>
        <button data-next title="Next">NEXT ▶▶</button>
        <button data-queue title="Show queue">QUEUE</button>
        <button data-lyrics title="Show lyrics">LYRICS</button>
        <button data-autoplay title="Endless queue — keep adding tracks similar to what you've played">∞ ENDLESS</button>
      </div>
      <div class="scrub">
        <span class="time" data-cur>0:00</span>
        <input type="range" min="0" max="100" step="0.1" value="0" data-seek />
        <span class="time" data-dur>0:00</span>
      </div>
    </div>
    <div class="vol">
      <span class="label">VOL</span>
      <input type="range" min="0" max="1" step="0.01" data-vol />
    </div>
  `;

  const $ = <T extends Element = Element>(sel: string) => host.querySelector(sel) as T;

  const elArt = $("[data-art]") as HTMLElement;
  const elTitle = $("[data-title]") as HTMLElement;
  const elSub = $("[data-sub]") as HTMLElement;
  const elBadge = $("[data-streambadge]") as HTMLElement;
  const elPlay = $("[data-play]") as HTMLButtonElement;
  const elPrev = $("[data-prev]") as HTMLButtonElement;
  const elNext = $("[data-next]") as HTMLButtonElement;
  const elQueueBtn = $("[data-queue]") as HTMLButtonElement;
  const elQueuePanel = $("[data-queue-panel]") as HTMLElement;
  const elQueueClose = $("[data-queue-close]") as HTMLButtonElement;
  const elQueueList = $("[data-queue-list]") as HTMLElement;
  const elAutoplay = $("[data-autoplay]") as HTMLButtonElement;
  const elLyricsBtn = $("[data-lyrics]") as HTMLButtonElement;
  const elLyricsPanel = $("[data-lyrics-panel]") as HTMLElement;
  const elLyricsClose = $("[data-lyrics-close]") as HTMLButtonElement;
  const elLyricsBody = $("[data-lyrics-body]") as HTMLElement;
  const elSeek = $("[data-seek]") as HTMLInputElement;
  const elCur = $("[data-cur]") as HTMLElement;
  const elDur = $("[data-dur]") as HTMLElement;
  const elVol = $("[data-vol]") as HTMLInputElement;

  // The transcoding policy rarely changes at runtime; we fetch it once
  // and keep it in closure scope. Used for the badge calculation.
  let policy: TranscodingPolicy | null = null;
  void getTranscodingPolicy().then(p => {
    policy = p;
    // Re-render the badge for whatever's currently playing.
    updateBadge(player.snapshot().current);
  }).catch(() => { /* policy is optional; badge falls back to local prefs */ });

  function updateBadge(current: SubsonicSong | null): void {
    if (!current) {
      elBadge.style.display = "none";
      return;
    }
    const prefs = getTranscodingPrefs();
    const plan = resolveStreamPlan(
      (current.suffix ?? "").toLowerCase(),
      typeof current.bitRate === "number" ? current.bitRate : null,
      prefs,
      policy,
    );
    const fmt = (plan.format || "?").toUpperCase();
    const br  = plan.bitrate ? `${plan.bitrate}` : "";
    if (plan.transcoded) {
      // Hot accent for transcoding, so the cost is visible at a glance.
      elBadge.style.color = "var(--accent)";
      elBadge.textContent = `↻ Transcoded · ${fmt}${br ? " " + br : ""}${plan.capped ? " (capped)" : ""}`;
    } else {
      elBadge.style.color = "var(--muted)";
      elBadge.textContent = `Original · ${fmt}${br ? " " + br : ""}`;
    }
    elBadge.style.display = "block";
  }

  // The Settings view fires this when the user changes their preferences.
  const onPrefsChanged = () => updateBadge(player.snapshot().current);
  window.addEventListener("muse:transcoding-prefs-changed", onPrefsChanged);

  // Wire interactions
  elPlay.addEventListener("click", () => player.toggle());
  elPrev.addEventListener("click", () => player.prev());
  elNext.addEventListener("click", () => player.next());
  elAutoplay.addEventListener("click", () => player.setAutoplay(!player.snapshot().autoplay));

  // Queue panel toggle + jump-to-row delegation
  const renderQueue = (state: PlayerState) => {
    if (state.queue.length === 0) {
      elQueueList.innerHTML = `<div class="queue-empty">Nothing queued</div>`;
      return;
    }
    elQueueList.innerHTML = state.queue.map((t, i) => {
      const art = coverArtUrl(t.coverArt, 64);
      const isCurrent = i === state.index;
      const artStyle = art ? `background-image:url("${art}")` : "";
      const title = (t.title ?? "").replace(/</g, "&lt;");
      const sub = `${(t.artist ?? "Unknown").replace(/</g, "&lt;")} — ${(t.album ?? "").replace(/</g, "&lt;")}`;
      return `
        <div class="queue-row${isCurrent ? " is-current" : ""}" data-idx="${i}">
          <div class="queue-art" style="${artStyle}"></div>
          <div class="queue-meta">
            <div class="queue-row-title">${title}</div>
            <div class="queue-row-sub">${sub}</div>
          </div>
          <div class="queue-row-dur">${fmtDuration(t.duration ?? 0)}</div>
        </div>`;
    }).join("");
  };
  elQueueBtn.addEventListener("click", () => {
    const open = elQueuePanel.hasAttribute("hidden");
    if (open) {
      elQueuePanel.removeAttribute("hidden");
      renderQueue(player.snapshot());
    } else {
      elQueuePanel.setAttribute("hidden", "");
    }
  });
  elQueueClose.addEventListener("click", () => {
    elQueuePanel.setAttribute("hidden", "");
  });
  elQueueList.addEventListener("click", (e) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>(".queue-row");
    if (!row) return;
    const idx = Number(row.dataset.idx);
    if (Number.isFinite(idx)) player.jumpTo(idx);
  });

  // ---- Lyrics panel ----------------------------------------------------
  // Shows lyrics for the current track and, for synced (LRC) lyrics,
  // highlights + auto-scrolls the active line as playback advances. The
  // per-tick cost is kept tiny: we fetch only when the track id changes and
  // re-render only the active-line class when the active index changes — the
  // heavy work (building the line elements) happens once per track.
  let lyricsLoadedId: string | null = null;  // track id the panel is showing
  let lyricsReqId = 0;                        // guards against stale async fetches
  let lyricsSynced = false;
  let lyricsLineEls: HTMLElement[] = [];      // line elements, in document order
  let lyricsTimes: number[] = [];             // their start times (s), -1 = untimed
  let lyricsActiveIdx = -1;

  function setLyricsMessage(msg: string): void {
    elLyricsBody.innerHTML = `<div class="lyrics-empty">${escapeText(msg)}</div>`;
    lyricsLineEls = [];
    lyricsTimes = [];
    lyricsActiveIdx = -1;
    lyricsSynced = false;
  }

  function renderLyrics(data: SongLyrics): void {
    if (data.lines.length === 0) {
      setLyricsMessage("No lyrics for this track");
      return;
    }
    lyricsSynced = data.synced;
    elLyricsBody.innerHTML = `
      <div class="lyrics" data-synced="${data.synced}">
        ${data.lines.map((l, i) => `
          <div class="lyric-line" data-idx="${i}"
               data-time="${l.time >= 0 ? l.time : ""}">${escapeText(l.text || " ")}</div>
        `).join("")}
      </div>`;
    lyricsLineEls = Array.from(elLyricsBody.querySelectorAll<HTMLElement>(".lyric-line"));
    lyricsTimes = lyricsLineEls.map((el) => (el.dataset.time ? Number(el.dataset.time) : -1));
    lyricsActiveIdx = -1;
  }

  async function loadLyrics(track: SubsonicSong | null): Promise<void> {
    lyricsLoadedId = track?.id ?? null;
    if (!track) { setLyricsMessage("Nothing playing"); return; }
    const req = ++lyricsReqId;
    setLyricsMessage("Loading lyrics…");
    try {
      const data = await getLyricsBySongId(track.id);
      if (req !== lyricsReqId) return;  // a newer track superseded this fetch
      renderLyrics(data);
      updateActiveLyric(player.snapshot().currentTime);
    } catch {
      if (req === lyricsReqId) setLyricsMessage("Couldn't load lyrics");
    }
  }

  function updateActiveLyric(currentTime: number): void {
    if (!lyricsSynced || lyricsTimes.length === 0) return;
    // Last line whose timestamp we've reached.
    let idx = -1;
    for (let i = 0; i < lyricsTimes.length; i++) {
      const t = lyricsTimes[i];
      if (t < 0) continue;
      if (t <= currentTime) idx = i;
      else break;
    }
    if (idx === lyricsActiveIdx) return;
    lyricsLineEls[lyricsActiveIdx]?.classList.remove("active");
    lyricsActiveIdx = idx;
    const el = lyricsLineEls[idx];
    if (el) {
      el.classList.add("active");
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  elLyricsBtn.addEventListener("click", () => {
    const opening = elLyricsPanel.hasAttribute("hidden");
    if (opening) {
      elLyricsPanel.removeAttribute("hidden");
      const cur = player.snapshot().current;
      if ((cur?.id ?? null) !== lyricsLoadedId) void loadLyrics(cur);
    } else {
      elLyricsPanel.setAttribute("hidden", "");
    }
  });
  elLyricsClose.addEventListener("click", () => elLyricsPanel.setAttribute("hidden", ""));
  // Click a synced line to jump playback there.
  elLyricsBody.addEventListener("click", (e) => {
    const line = (e.target as HTMLElement).closest<HTMLElement>(".lyric-line");
    if (!line || !line.dataset.time) return;
    const t = Number(line.dataset.time);
    if (Number.isFinite(t) && t >= 0) player.seek(t);
  });
  elSeek.addEventListener("input", () => {
    const snap = player.snapshot();
    if (snap.duration > 0) {
      const pct = Number(elSeek.value) / 100;
      player.seek(pct * snap.duration);
    }
  });
  elVol.addEventListener("input", () => player.setVolume(Number(elVol.value)));

  // Keyboard: spacebar play/pause anywhere except when typing
  const onKey = (e: KeyboardEvent) => {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.code === "Space") {
      e.preventDefault();
      player.toggle();
    }
  };
  window.addEventListener("keydown", onKey);

  let isUserSeeking = false;
  elSeek.addEventListener("pointerdown", () => { isUserSeeking = true; });
  elSeek.addEventListener("pointerup",   () => { isUserSeeking = false; });

  // The subscribe callback fires on every audio timeupdate (~4×/sec). Each
  // call to coverArtUrl() generates a fresh random salt+token, producing a
  // brand-new URL string for the same image — which makes the browser
  // re-fetch the cover art on every tick. Cache the "what we last rendered"
  // key so we only rebuild the URL (and trigger a network fetch) when the
  // cover actually changes. "" is the sentinel for "nothing playing".
  let lastCoverKey = "";
  let lastQueueSig = "";

  const unsub = player.subscribe((state) => {
    if (state.current) {
      elTitle.textContent = state.current.title;
      elSub.textContent = `${state.current.artist ?? "Unknown"} — ${state.current.album ?? ""}`;
      const nextKey = `art:${state.current.coverArt ?? ""}`;
      if (nextKey !== lastCoverKey) {
        lastCoverKey = nextKey;
        const art = coverArtUrl(state.current.coverArt, 120);
        elArt.style.backgroundImage = art ? `url("${art}")` : "";
      }
    } else {
      elTitle.textContent = "—";
      elSub.textContent = "nothing playing";
      if (lastCoverKey !== "") {
        lastCoverKey = "";
        elArt.style.backgroundImage = "";
      }
    }
    updateBadge(state.current);
    elPlay.textContent = state.playing ? "❚❚ PAUSE" : "▶ PLAY";
    // Endless-mode toggle: accent + pressed state when on.
    elAutoplay.classList.toggle("is-on", state.autoplay);
    elAutoplay.setAttribute("aria-pressed", state.autoplay ? "true" : "false");
    elAutoplay.style.color = state.autoplay ? "var(--accent)" : "";
    elCur.textContent = fmtDuration(state.currentTime);
    elDur.textContent = fmtDuration(state.duration);
    if (!isUserSeeking && state.duration > 0) {
      elSeek.value = String((state.currentTime / state.duration) * 100);
    }
    elVol.value = String(state.volume);
    // Re-render the queue panel only when the queue or current index actually
    // changes — coverArtUrl() bakes a fresh token into every call, so
    // re-rendering every timeupdate would reload all the cover images.
    if (!elQueuePanel.hasAttribute("hidden")) {
      const sig = `${state.index}|${state.queue.map(t => t.id).join(",")}`;
      if (sig !== lastQueueSig) {
        lastQueueSig = sig;
        renderQueue(state);
      }
    }
    // Lyrics panel: refetch when the track changes (while open), otherwise just
    // advance the highlighted line. `?? null` so "nothing playing" compares
    // equal to the initial state and doesn't refetch every tick.
    if (!elLyricsPanel.hasAttribute("hidden")) {
      if ((state.current?.id ?? null) !== lyricsLoadedId) {
        void loadLyrics(state.current);
      } else {
        updateActiveLyric(state.currentTime);
      }
    }
  });

  return () => {
    unsub();
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("muse:transcoding-prefs-changed", onPrefsChanged);
  };
}
