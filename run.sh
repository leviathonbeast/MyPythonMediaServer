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
    echo "→ Starting Muse (development, auto-reload)"
    exec uvicorn backend.main:app --host 0.0.0.0 --port 4040 --reload
    ;;
  prod|*)
    echo "→ Starting Muse"
    exec python -m backend.main
    ;;
esac
