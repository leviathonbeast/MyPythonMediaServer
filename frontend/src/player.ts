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
  coverArtUrl, streamUrl, getTranscodingPolicy, getTranscodingPrefs,
  resolveStreamPlan, type TranscodingPolicy,
} from "./api";

type Listener = (state: PlayerState) => void;

export interface PlayerState {
  queue: SubsonicSong[];
  index: number;             // -1 = nothing loaded
  playing: boolean;
  currentTime: number;
  duration: number;
  volume: number;
  current: SubsonicSong | null;
}

class Player {
  private audio = new Audio();
  private queue: SubsonicSong[] = [];
  private index = -1;
  private listeners = new Set<Listener>();

  constructor() {
    // Restore volume from previous session if available.
    const savedVol = Number(localStorage.getItem("muse.volume") ?? "0.9");
    this.audio.volume = Number.isFinite(savedVol) ? savedVol : 0.9;

    // We forward the small set of <audio> events we care about. Each one
    // pokes subscribers with the latest state.
    const fwd = () => this.emit();
    this.audio.addEventListener("play", fwd);
    this.audio.addEventListener("pause", fwd);
    this.audio.addEventListener("timeupdate", fwd);
    this.audio.addEventListener("durationchange", fwd);
    this.audio.addEventListener("volumechange", () => {
      localStorage.setItem("muse.volume", String(this.audio.volume));
      fwd();
    });
    // When a track ends, advance the queue. If we're at the tail, stop.
    this.audio.addEventListener("ended", () => {
      if (this.index >= 0 && this.index < this.queue.length - 1) {
        this.playAt(this.index + 1);
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
    };
  }

  private emit(): void {
    const s = this.snapshot();
    for (const fn of this.listeners) fn(s);
  }
}

export const player = new Player();

/* ---------- dock rendering ---------- */

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

  // The Workshop view fires this when the user changes their preferences.
  const onPrefsChanged = () => updateBadge(player.snapshot().current);
  window.addEventListener("muse:transcoding-prefs-changed", onPrefsChanged);

  // Wire interactions
  elPlay.addEventListener("click", () => player.toggle());
  elPrev.addEventListener("click", () => player.prev());
  elNext.addEventListener("click", () => player.next());
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
    elCur.textContent = fmtDuration(state.currentTime);
    elDur.textContent = fmtDuration(state.duration);
    if (!isUserSeeking && state.duration > 0) {
      elSeek.value = String((state.currentTime / state.duration) * 100);
    }
    elVol.value = String(state.volume);
  });

  return () => {
    unsub();
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("muse:transcoding-prefs-changed", onPrefsChanged);
  };
}
