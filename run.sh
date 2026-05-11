#!/usr/bin/env bash
# run.sh — convenience launcher for development.
#
# Reads ./config.yaml if present, falls back to env-var-only configuration.
# Use `./run.sh dev` to run the backend with auto-reload.
#
# For production, run uvicorn (or gunicorn+uvicorn workers) directly and
# put a TLS reverse proxy in front. See README "Security notes".

set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-prod}"

if [ ! -d ".venv" ]; then
  echo "→ Creating virtualenv .venv"
  python3 -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate

echo "→ Installing/refreshing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

if [ ! -f "config.yaml" ]; then
  echo "⚠  No config.yaml found — using built-in defaults."
  echo "   Copy config.example.yaml → config.yaml and edit before exposing the server."
fi

case "$MODE" in
  dev)
    echo "→ Starting Muse backend (development, auto-reload)"
    uvicorn backend.main:app --host 0.0.0.0 --port 4040 --reload &
    BACKEND_PID=$!

    echo "→ Starting Muse frontend (Vite dev server)"
    if [ ! -d "frontend/node_modules" ]; then
      echo "→ Installing frontend dependencies"
      (cd frontend && npm install)
    fi
    HOST_IP=$(hostname -I | awk '{print $1}')
    MUSE_FRONTEND_HOST="$HOST_IP" (cd frontend && npm run dev) &
    FRONTEND_PID=$!

    trap 'echo "→ Shutting down…"; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; wait' INT TERM EXIT
    wait
    ;;
  prod|*)
    echo "→ Starting Muse"
    exec python -m backend.main
    ;;
esac
