#!/usr/bin/env bash
# Pull the latest code and restart. Run as root on the server: sudo bash /opt/trade-broski/deploy/update.sh
# (git runs as root because the GitHub deploy key is in /root/.ssh; files are then handed to the trader user)
set -euo pipefail
cd /opt/trade-broski
git -c safe.directory=/opt/trade-broski pull --ff-only
chown -R trader:trader /opt/trade-broski
sudo -u trader .venv/bin/pip install -r requirements.txt -q
sudo -u trader .venv/bin/python -m pytest -q tests || { echo "Tests failed - not restarting"; exit 1; }
systemctl restart trade-broski
sleep 3 && systemctl --no-pager status trade-broski | head -5
