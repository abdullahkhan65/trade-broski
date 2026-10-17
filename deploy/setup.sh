#!/usr/bin/env bash
# One-time server setup for Debian 12 / Ubuntu 22.04+ (run as root):
#   curl -fsSL <raw url of this file> | bash      OR      bash deploy/setup.sh
# Then: edit /opt/trade-broski/.env, edit /etc/caddy/Caddyfile, and start the service.
set -euo pipefail

REPO_URL="${REPO_URL:-}"          # e.g. https://github.com/you/trade-broski.git (private repo: use a deploy key)
APP_DIR=/opt/trade-broski

echo "==> Packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git sqlite3 ufw debian-keyring debian-archive-keyring apt-transport-https curl gnupg

echo "==> Caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> 1 GB swap (helps a 1 GB VPS survive pip installs)"
if ! swapon --show | grep -q swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> App user + code"
id trader >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin trader
if [ ! -d "$APP_DIR/.git" ]; then
  if [ -z "$REPO_URL" ]; then echo "Set REPO_URL=... or copy the code to $APP_DIR first"; exit 1; fi
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/backups"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown -R trader:trader "$APP_DIR"

echo "==> systemd service"
cp "$APP_DIR/deploy/trade-broski.service" /etc/systemd/system/trade-broski.service
systemctl daemon-reload
systemctl enable trade-broski

echo "==> Keep logs small"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nMaxRetentionSec=30day\n' > /etc/systemd/journald.conf.d/size.conf
systemctl restart systemd-journald

echo "==> Firewall: SSH + HTTP/HTTPS only (the app itself listens on 127.0.0.1)"
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable

IP=$(curl -s https://api.ipify.org || echo "YOUR_IP")
cat <<MSG

Done. Next:
 1. nano $APP_DIR/.env            (Anthropic key, DASHBOARD_PASSWORD, optional Telegram)
 2. sudo -u trader $APP_DIR/.venv/bin/python -m app.check   (run from $APP_DIR)
 3. cp $APP_DIR/deploy/Caddyfile /etc/caddy/Caddyfile
    sed -i "s/YOUR_DOMAIN/${IP//./-}.sslip.io/" /etc/caddy/Caddyfile && systemctl reload caddy
 4. systemctl start trade-broski && journalctl -u trade-broski -f
 5. Open https://${IP//./-}.sslip.io  (login: DASHBOARD_USER / DASHBOARD_PASSWORD)
MSG
