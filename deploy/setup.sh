#!/usr/bin/env bash
# One-time (and safe to re-run) server setup for Debian / Ubuntu. Run as root on the VPS:
#   sudo bash /opt/concert-scraper/deploy/setup.sh concerts.example.com
# Installs Python + Caddy, creates the `concert` user, the venv, headless Chromium,
# the systemd service, the HTTPS site and the cron entry. Never touches .env if it exists.
set -euo pipefail

DOMAIN="${1:?usage: setup.sh <subdomain, e.g. concerts.example.com>}"
APP=/opt/concert-scraper
USER_NAME=concert

echo "== packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https gnupg cron
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

echo "== user and files"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$USER_NAME --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP/data/logs"
chown -R "$USER_NAME:$USER_NAME" "$APP"

echo "== python venv + chromium"
sudo -u "$USER_NAME" python3 -m venv "$APP/.venv"
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
"$APP/.venv/bin/python" -m playwright install-deps chromium
sudo -u "$USER_NAME" "$APP/.venv/bin/python" -m playwright install chromium

echo "== .env"
if [ ! -f "$APP/.env" ]; then
  PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
  SK=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sed -e "s|^APP_PASSWORD=.*|APP_PASSWORD=$PW|" -e "s|^SECRET_KEY=.*|SECRET_KEY=$SK|" \
      -e "s|^COOKIE_SECURE=.*|COOKIE_SECURE=true|" -e "s|^APP_URL=.*|APP_URL=https://$DOMAIN|" \
      "$APP/.env.example" > "$APP/.env"
  chown "$USER_NAME:$USER_NAME" "$APP/.env"; chmod 600 "$APP/.env"
  echo "   created $APP/.env with a new APP_PASSWORD (read it with: sudo grep APP_PASSWORD $APP/.env)"
else
  echo "   $APP/.env exists, left as is"
fi

echo "== web service"
cp "$APP/deploy/concert-watcher.service" /etc/systemd/system/concert-watcher.service
systemctl daemon-reload
systemctl enable --now concert-watcher
systemctl restart concert-watcher

echo "== HTTPS (Caddy)"
sed "s|{DOMAIN}|$DOMAIN|" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "== cron (every hour; the app scans when one is due, every 12 h by default, set on the Scans page)"
CRON_LINE="0 * * * * cd $APP && .venv/bin/python -m app.watch --trigger cron >> data/logs/cron.log 2>&1"
( crontab -u "$USER_NAME" -l 2>/dev/null | grep -v 'app.watch' ; echo "$CRON_LINE" ) | crontab -u "$USER_NAME" -

echo "== done: https://$DOMAIN  (server time now: $(date '+%H:%M %Z'))"
