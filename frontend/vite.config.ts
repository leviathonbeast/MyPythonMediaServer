// vite.config.ts
//
// Vite dev-server configuration.
//
// WHY proxy /rest and /api: during development the frontend runs on Vite's
// dev server (default :5173) while the FastAPI backend runs on :4040.
// Rather than enabling CORS and embedding absolute backend URLs in client
// code, we proxy the two API path prefixes through Vite. The frontend can
// then `fetch("/rest/ping.view?...")` exactly as it would in production
// (where the same backend serves both the API and any built static assets).
//
// This also means there is ONE place to change the backend port for dev.

import { defineConfig } from "vite";

const BACKEND  = process.env.MUSE_BACKEND_URL  ?? "http://127.0.0.1:4040";
const DEV_HOST = process.env.MUSE_FRONTEND_HOST ?? "0.0.0.0";

export default defineConfig({
  // Production assets are served by the FastAPI backend at /web/*. Vite
  // bakes this prefix into emitted index.html (`<script src="/web/assets/...">`)
  // and into any dynamic imports. Must match the mount paths in backend/main.py.
  base: "/web/",
  server: {
    host: DEV_HOST,
    port: 5173,
    strictPort: false,
    proxy: {
      // Subsonic-compatible endpoints live under /rest
      "/rest": {
        target: BACKEND,
        changeOrigin: true,
        // Streaming responses must not be buffered by the proxy
        ws: false,
      },
      // Internal web UI endpoints (login, scan progress, etc.)
      "/api": {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2022",
  },
});
