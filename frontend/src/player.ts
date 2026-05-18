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
  private scrobbled = false;
  // Persistence: skip save() while we're restoring from the server so we
  // don't clobber the saved state with a partial in-memory snapshot.
  private suppressSave = false;
  private positionTimer: number | null = null;
  private pendingRestorePosition = 0;

  constructor() {
    // Restore volume from previous session if available.
    const savedVol = Number(localStorage.getItem("muse.volume") ?? "0.9");
    this.audio.volume = Number.isFinite(savedVol) ? savedVol : 0.9;

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
      if (
        !this.scrobbled &&
        knownDuration > 0 &&
        this.audio.currentTime / knownDuration >= 0.5
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
    <div class="queue-panel" data-queue-panel hidden>
      <div class="queue-header">
        <span class="queue-title">QUEUE</span>
        <button data-queue-close title="Close">×</button>
      </div>
      <div class="queue-list" data-queue-list></div>
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
  });

  return () => {
    unsub();
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("muse:transcoding-prefs-changed", onPrefsChanged);
  };
}
