#!/usr/bin/env bash
# Command Center — one-shot droplet deploy (Ubuntu 22.04+).
# Run on the droplet:  bash deploy.sh
# Reads secrets from environment or prompts. Sets up a systemd service behind
# gunicorn, persistent SQLite on the droplet disk, and an optional Caddy reverse
# proxy for automatic HTTPS.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/command_center}"
DATA_DIR="${CC_DATA_DIR:-/opt/command_center/data}"
DOMAIN="${CC_DOMAIN:-}"                 # e.g. cc.ncwealthprotection.me (optional)
PORT="${COMMAND_CENTER_PORT:-5055}"
CC_GEO_FILTER_ENABLED="${CC_GEO_FILTER_ENABLED:-true}"
CC_TARGET_BIRTH_YEARS="${CC_TARGET_BIRTH_YEARS:-1962}"

read -rp "Dashboard username [chris]: " CC_USERNAME; CC_USERNAME="${CC_USERNAME:-chris}"
read -rsp "Dashboard password (required): " CC_PASSWORD; echo
read -rsp "Ingest token (laptop->cloud secret): " CC_INGEST_TOKEN; echo
[ -z "$CC_PASSWORD" ] && { echo "Password required."; exit 1; }
[ -z "$CC_INGEST_TOKEN" ] && CC_INGEST_TOKEN="$(openssl rand -hex 24)"

echo "[1/5] packages..."
sudo apt-get update -y && sudo apt-get install -y python3-pip python3-venv git

echo "[2/5] app + venv..."
sudo mkdir -p "$APP_DIR" "$DATA_DIR"
sudo chown -R "$USER" "$APP_DIR"
# (code is expected to already be in $APP_DIR via git clone or upload)
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" gunicorn

echo "[3/5] systemd service..."
sudo tee /etc/systemd/system/command-center.service >/dev/null <<EOF
[Unit]
Description=Command Center
After=network.target
[Service]
WorkingDirectory=$APP_DIR
Environment=CC_USERNAME=$CC_USERNAME
Environment=CC_PASSWORD=$CC_PASSWORD
Environment=CC_INGEST_TOKEN=$CC_INGEST_TOKEN
Environment=CC_AGENT_TOKEN=$CC_AGENT_TOKEN
Environment=CC_DATA_DIR=$DATA_DIR
Environment=COMMAND_CENTER_PORT=$PORT
Environment=CC_GEO_FILTER_ENABLED=$CC_GEO_FILTER_ENABLED
Environment=CC_TARGET_BIRTH_YEARS=$CC_TARGET_BIRTH_YEARS
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 -b 127.0.0.1:$PORT app:app
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now command-center

echo "[4/5] firewall..."
sudo ufw allow OpenSSH >/dev/null 2>&1 || true
sudo ufw allow 80 >/dev/null 2>&1 || true
sudo ufw allow 443 >/dev/null 2>&1 || true

echo "[5/5] HTTPS reverse proxy (Caddy)..."
if [ -n "$DOMAIN" ]; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update -y && sudo apt-get install -y caddy
  echo "$DOMAIN { reverse_proxy 127.0.0.1:$PORT }" | sudo tee /etc/caddy/Caddyfile
  sudo systemctl restart caddy
  echo "Done. HTTPS: https://$DOMAIN   (ingest token: $CC_INGEST_TOKEN)"
else
  echo "Done. Reachable on http://<droplet-ip> after you also: sudo ufw allow $PORT"
  echo "Ingest token: $CC_INGEST_TOKEN"
  echo "TIP: set CC_DOMAIN and re-run for automatic HTTPS (strongly recommended for PII)."
fi
