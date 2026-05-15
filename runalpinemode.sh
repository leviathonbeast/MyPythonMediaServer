#!/bin/sh
# run.sh — Alpine-compatible launcher

set -eu

cd "$(dirname "$0")"

MODE="${1:-prod}"

# ---- ensure Python venv support exists (Alpine requirement) ----
if [ ! -d ".venv" ]; then
  echo "→ Creating virtualenv .venv"

  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not installed"
    exit 1
  fi

  python3 -m venv .venv
fi

# shellcheck source=/dev/null
. .venv/bin/activate

echo "→ Installing/refreshing dependencies"
pip install --upgrade pip
pip install -r backend/requirements.txt

if [ ! -f "config.yaml" ]; then
  echo "⚠ No config.yaml found — using defaults"
fi

get_ip() {
  # Alpine-safe IP detection fallback
  ip addr show 2>/dev/null | awk '/inet / && $2 !~ /^127/ {print $2}' | cut -d/ -f1 | head -n1
}

case "$MODE" in
  dev)
    echo "→ Starting backend (dev)"

    uvicorn backend.main:app \
      --host 0.0.0.0 \
      --port 4040 \
      --reload &
    BACKEND_PID=$!

    echo "→ Starting frontend (dev)"

    if [ ! -d "frontend/node_modules" ]; then
      echo "→ Installing frontend dependencies"
      (cd frontend && npm install)
    fi

    HOST_IP="$(get_ip || echo 127.0.0.1)"

    (cd frontend && \
      MUSE_FRONTEND_HOST="$HOST_IP" npm run dev) &
    FRONTEND_PID=$!

    trap 'echo "→ Shutting down..."; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true; wait' INT TERM EXIT
    wait
    ;;

  prod|*)
    echo "→ Starting Muse (production)"

    exec python3 -m backend.main
    ;;
esac
