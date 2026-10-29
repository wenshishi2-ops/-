#!/usr/bin/env bash
# Nexa Quant server setup for Ubuntu 24.04. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/wenshishi2-ops/-/main/deploy/setup.sh | bash
# Safe to run again: it only adds what is missing.
set -euo pipefail
APP=/opt/nexa-quant
REPO_SSH=git@github.com:wenshishi2-ops/-.git
REPO_HTTPS=https://github.com/wenshishi2-ops/-.git

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates >/dev/null

id nexa >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP" --shell /bin/bash nexa
mkdir -p "$APP" && chown nexa:nexa "$APP"

echo "==> Installing Python 3.11 and dependencies"
sudo -u nexa -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
UV="$APP/.local/bin/uv"
[ -d "$APP/repo/.git" ] || sudo -u nexa git clone -q "$REPO_HTTPS" "$APP/repo"
sudo -u nexa "$UV" venv -q --python 3.11 "$APP/venv"
sudo -u nexa "$UV" pip install -q --python "$APP/venv/bin/python" -r "$APP/repo/requirements.txt"

echo "==> Creating a deploy key so the server can publish data.json"
if [ ! -f "$APP/.ssh/id_ed25519" ]; then
  sudo -u nexa mkdir -p "$APP/.ssh"
  sudo -u nexa ssh-keygen -q -t ed25519 -N "" -C "nexa-quant-server" -f "$APP/.ssh/id_ed25519"
fi
sudo -u nexa bash -c "ssh-keyscan -t ed25519 github.com >> '$APP/.ssh/known_hosts' 2>/dev/null; sort -u -o '$APP/.ssh/known_hosts' '$APP/.ssh/known_hosts'"
sudo -u nexa git -C "$APP/repo" remote set-url origin "$REPO_SSH"
sudo -u nexa git -C "$APP/repo" config user.name "nexa-quant-server"
sudo -u nexa git -C "$APP/repo" config user.email "nexa-quant-server@users.noreply.github.com"

echo "==> Polymarket API key (optional, read-only check; press Enter to skip)"
if [ ! -f /etc/nexa-quant.env ] && [ -r /dev/tty ]; then
  read -rp "Key ID: " KEY_ID </dev/tty || KEY_ID=""
  if [ -n "$KEY_ID" ]; then
    read -rsp "Secret Key (hidden): " SECRET </dev/tty; echo
    umask 077
    printf 'POLYMARKET_KEY_ID=%s\nPOLYMARKET_SECRET_KEY=%s\n' "$KEY_ID" "$SECRET" > /etc/nexa-quant.env
    chown root:nexa /etc/nexa-quant.env && chmod 640 /etc/nexa-quant.env
  fi
fi

echo "==> Installing the 5-minute timer"
chmod +x "$APP/repo/deploy/run.sh"
cat > /etc/systemd/system/nexa-quant.service <<EOF
[Unit]
Description=Nexa Quant paper-trading cycle (no real orders)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=nexa
ExecStart=$APP/repo/deploy/run.sh
TimeoutStartSec=240
EOF
cat > /etc/systemd/system/nexa-quant.timer <<EOF
[Unit]
Description=Run Nexa Quant every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

echo
echo "================================================================"
echo "Almost done. Add this public key to GitHub as a deploy key"
echo "WITH 'Allow write access' ticked:"
echo "  https://github.com/wenshishi2-ops/-/settings/keys/new"
echo
cat "$APP/.ssh/id_ed25519.pub"
echo
echo "Then start the timer with:"
echo "  systemctl enable --now nexa-quant.timer"
echo "================================================================"
