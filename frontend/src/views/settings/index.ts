// src/views/settings/index.ts
//
// Settings page composer.
//
// The settings page used to live in one ~530-line file with five concerns
// (stats, folders, streaming, scan, maintenance) tangled together. Each
// concern is now its own module in this directory; this file's only job
// is to compose them.
//
// What this file does:
//   1. Decides which sections to mount (some are admin-only).
//   2. Mounts them in display order (each section appends its own DOM).
//   3. Wires cross-section communication — e.g. a finished scan or a GC
//      run invalidates the stats counters, so we tell stats to refresh.
//   4. Aggregates every section's cleanup into a single __cleanup hook
//      so the router can tear everything down on view-unmount.
//
// Adding a new section is a 3-step exercise:
//   - create views/settings/<section>.ts exporting a render function that
//     returns { refresh, cleanup }
//   - import it here
//   - call it from renderSettings() at the desired position

import { authState } from "../../auth";
import { renderStatsSection }       from "./stats";
import { renderStreamingSection }   from "./streaming";
import { renderFoldersSection }     from "./folders";
import { renderScanSection }        from "./scan";
import { renderAnalyzeSection }     from "./analyze";
import { renderMaintenanceSection } from "./maintenance";
import { renderLastfmSection }      from "./lastfm";
import { renderListenBrainzSection } from "./listenbrainz";

// Every section exposes this minimal shape. We accept it as a return type
// from each section's mount function, then aggregate the cleanup hooks.
interface MountedSection {
  refresh: () => Promise<void>;
  cleanup: () => void;
}

export async function renderSettings(host: HTMLElement): Promise<void> {
  const isAdmin = authState().is_admin;

  // Page header is shared. Each section appends its own DOM AFTER this,
  // so the layout reads top-to-bottom in the order we mount things below.
  host.innerHTML = `
    <header class="page-head">
      <h1><em>Settings</em></h1>
      <div class="meta">— ${isAdmin ? "Library admin" : "Library stats & playback"}</div>
    </header>
  `;

  // Collect cleanup callbacks. The router's view-unmount hook expects a
  // single __cleanup; we glue every section's cleanup into one at the end.
  const cleanups: Array<() => void> = [];

  // Stats are always shown. Other sections may call its refresh() when
  // they mutate the library, so we keep a reference to the handle.
  const stats = await renderStatsSection(host);
  cleanups.push(stats.cleanup);

  // Folders is admin-only. It sits between stats and streaming so the
  // page reads "what's in the library → playback settings → housekeeping".
  let folders: MountedSection | null = null;
  if (isAdmin) {
    folders = await renderFoldersSection(host, {
      // Add/remove a folder → stats counters shift.
      onLibraryChanged: () => { void stats.refresh(); },
    });
    cleanups.push(folders.cleanup);
  }

  // Streaming preferences are per-browser (localStorage) and shown to all.
  const streaming = await renderStreamingSection(host);
  cleanups.push(streaming.cleanup);

  // Last.fm linking is per-user and shown to all. Sits with streaming
  // because both are "your playback / your account" preferences.
  const lastfm = await renderLastfmSection(host);
  cleanups.push(lastfm.cleanup);

  // ListenBrainz sits right after Last.fm — same "your account" concern,
  // and it adds importing ListenBrainz's generated playlists on top of
  // scrobbling.
  const listenbrainz = await renderListenBrainzSection(host);
  cleanups.push(listenbrainz.cleanup);

  if (isAdmin) {
    // Both scan-finish and GC/vacuum can change stats AND folder track
    // counts, so they refresh both.
    const onLibraryChanged = () => {
      void stats.refresh();
      if (folders) void folders.refresh();
    };

    const scan = await renderScanSection(host, { onLibraryChanged });
    cleanups.push(scan.cleanup);

    // Sonic analysis sits right after Scan — it's the second-stage, opt-in
    // pass that fingerprints tracks once they're in the library.
    const analyze = await renderAnalyzeSection(host);
    cleanups.push(analyze.cleanup);

    const maint = await renderMaintenanceSection(host, { onLibraryChanged });
    cleanups.push(maint.cleanup);
  }

  // The router (see main.ts) calls __cleanup before swapping views. Run
  // each section's teardown — wrap in try/catch so a buggy section can't
  // prevent the others from cleaning up.
  (host as any).__cleanup = () => {
    for (const fn of cleanups) {
      try { fn(); } catch { /* ignore — best-effort teardown */ }
    }
  };
}
