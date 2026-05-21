#!/usr/bin/env bash
#
# install-service.sh — install Muse as a systemd service on Debian.
#
# Generates /etc/systemd/system/muse.service pointing at THIS checkout and the
# user running the script, then enables + starts it. Safe to re-run (it just
# rewrites the unit and restarts). Run it as your normal user — it uses sudo
# only for the steps that touch /etc and systemctl.
#
#   ./deploy/install-service.sh
#
# Afterwards:
#   systemctl status muse          # health
#   journalctl -u muse -f          # live logs
#   sudo systemctl restart muse    # restart after a code pull
#   sudo systemctl disable --now muse   # stop + remove from boot

set -euo pipefail

# --- must NOT be root: the venv and data dir should be owned by the human ---
if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (the one that owns the checkout), not root."
  echo "It will sudo only for installing the systemd unit."
  exit 1
fi

# Repo root = parent of this script's directory. Resolving from the script
# location (not \$PWD) means it works no matter where you invoke it from.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$USER"
RUN_GROUP="$(id -gn)"
VENV="$REPO_DIR/.venv"
PYTHON="$VENV/bin/python"
SERVICE=/etc/systemd/system/muse.service

echo "→ repo: $REPO_DIR"
echo "→ user: $RUN_USER ($RUN_GROUP)"

# --- preflight: system packages -------------------------------------------
command -v python3 >/dev/null || { echo "ERROR: python3 not installed (sudo apt install python3)"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "ERROR: python3-venv missing (sudo apt install python3-venv)"; exit 1; }
command -v ffmpeg >/dev/null || echo "⚠  ffmpeg not found — transcoding & sonic analysis need it: sudo apt install ffmpeg"

# --- config sanity: secrets live in config.yaml ---------------------------
if [ ! -f "$REPO_DIR/config.yaml" ]; then
  echo "ERROR: no config.yaml in $REPO_DIR."
  echo "       cp config.example.yaml config.yaml and set admin_password + jwt_secret first."
  exit 1
fi

# --- venv + dependencies (created once, refreshed each run) ----------------
if [ ! -x "$PYTHON" ]; then
  echo "→ creating virtualenv at $VENV"
  python3 -m venv "$VENV"
fi
echo "→ installing/refreshing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/backend/requirements.txt"

# --- write the unit (sudo) ------------------------------------------------
echo "→ writing $SERVICE"
sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Muse music server
After=network-online.target
Wants=network-online.target
# If your music lives on a network mount, uncomment and point this at it so
# the scanner doesn't start before the share is available:
# RequiresMountsFor=/mnt/music

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON -m backend.main
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGTERM
SyslogIdentifier=muse
# Light hardening. We deliberately do NOT set ProtectHome, because the app,
# its config.yaml and (often) ./data live under /home — ProtectHome would
# make them invisible to the service.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# --- enable + (re)start ----------------------------------------------------
echo "→ enabling + starting muse.service"
sudo systemctl daemon-reload
sudo systemctl enable muse.service
sudo systemctl restart muse.service

sleep 1
sudo systemctl --no-pager --full status muse.service || true

cat <<'TIPS'

Installed. Handy commands:
  systemctl status muse          # is it up?
  journalctl -u muse -f          # follow logs
  sudo systemctl restart muse    # after pulling new code
  sudo systemctl disable --now muse   # stop and remove from boot
TIPS
