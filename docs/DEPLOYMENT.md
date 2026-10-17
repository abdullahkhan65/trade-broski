# Deployment: from zero to running 24/7

This takes about 1 hour. Do the steps in order. Each step ends with a check, so you know it worked before moving on.

## 0. What you need

- A card for the VPS (~€2.09/month) and for Anthropic credit ($5 prepaid lasts months)
- A Telegram account
- This repo pushed to a **private** GitHub repo (the server pulls from it)

---

## 1. Accounts and keys

### 1a. Prices: nothing to sign up for
All price data is public and needs no account: Binance (BTC and gold), Kraken (EUR/USD, GBP/USD), Yahoo (USD/JPY) and Dukascopy (backtest history). OANDA was dropped because it doesn't accept clients from Pakistan.

✅ Check: `python -m app.check` shows ✅ for Binance, Kraken, Yahoo and Dukascopy.

### 1b. Anthropic API key (Claude Haiku 5.5)
1. Create an account at console.anthropic.com and go to **Billing → buy $5 credit. Turn auto-reload OFF.**
2. Go to **Limits → set a monthly spend limit of $2.**
3. Go to **API Keys → Create key**, then set `ANTHROPIC_API_KEY=...` in `.env`.

✅ Check: `python -m app.check` shows `Anthropic: key works, model claude-haiku-5-5 available`.

### 1c. Telegram bot (alerts, free)
1. In Telegram, message **@BotFather** and send `/newbot`. Copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Copy `"chat":{"id": 123456789}` into `TELEGRAM_CHAT_ID`.

✅ Check: `python -m app.check` sends "✅ Paper trader check: Telegram works" to your phone.

### 1d. Dashboard password
Set `DASHBOARD_USER` and a long `DASHBOARD_PASSWORD` in `.env`. The app won't serve pages without a password.

---

## 2. Try it locally first (10 min)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
python -m app.check
pytest -q
python -m app.main            # open http://127.0.0.1:8000
```

You should see live prices ticking at once for all instruments. Binance and Kraken quote around the clock, but the bot only trades gold and forex while the real market is open (Sun 5pm to Fri 5pm New York, which is about 2–3am PKT Monday to 2–3am PKT Saturday).

Optional: run a backtest before going further. The first run downloads 2 years of history (about 20–30 min per gold/forex instrument from Dukascopy, one time only; it's cached in `data/history/`).
```bash
python -m app.backtest --symbol all --days 730
```

---

## 3. Buy the server

**Recommended: Netcup VPS nano** (~€2.09/month, about $2.50). Check before buying that it has **≥ 1 GB RAM**.
- Choose **Debian 12** or **Ubuntu 24.04** as the OS. Location: Germany/Austria is fine, since Binance futures works from there.
- Pay monthly or yearly at a fixed price. Avoid usage-based billing.
- **Fallback:** Hetzner CX23 (~€5.49/month, about $6.40). The total still stays under $8.
- **Avoid:** free tiers that sleep (Render, Railway free, etc.). The bot must check stops every few seconds, 24/7.

✅ Check: `ssh root@YOUR_IP` works.

> Some regions are blocked by Binance (HTTP 451, e.g. US-based servers). The app falls back to Bybit automatically, but `app.check` tells you up front.

---

## 4. Install on the server (15 min)

```bash
ssh root@YOUR_IP
apt-get update && apt-get install -y git
# private repo: add a deploy key first (GitHub → repo → Settings → Deploy keys), or use an HTTPS token
REPO_URL=git@github.com:YOU/trade-broski.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/YOU/trade-broski/main/deploy/setup.sh)"
#   (or: git clone the repo to /opt/trade-broski and run: bash /opt/trade-broski/deploy/setup.sh)
```

`deploy/setup.sh` installs Python, Caddy and SQLite. It also creates a `trader` user, a 1 GB swap file, the virtualenv and the systemd service, sets the firewall (SSH and 80/443 only) and caps the logs at 200 MB.

Then:

```bash
nano /opt/trade-broski/.env                    # paste the same values as your laptop .env
cd /opt/trade-broski && sudo -u trader .venv/bin/python -m app.check
```

✅ Check: every line is ✅. Pay particular attention to Binance (some server regions are blocked; the Bybit fallback covers that).

---

## 5. HTTPS dashboard with a free domain (5 min)

[sslip.io](https://sslip.io) gives you a free hostname built from your IP, and Caddy fetches a real HTTPS certificate for it automatically.

```bash
IP=$(curl -s https://api.ipify.org)
cp /opt/trade-broski/deploy/Caddyfile /etc/caddy/Caddyfile
sed -i "s/YOUR_DOMAIN/${IP//./-}.sslip.io/" /etc/caddy/Caddyfile
systemctl reload caddy
```

If you later buy a domain (~$1/month), point an A record at the IP, put the domain in the Caddyfile and reload.

---

## 6. Start it

```bash
systemctl start trade-broski
journalctl -u trade-broski -f        # watch logs, Ctrl-C to stop watching
```

✅ Checks:
- Telegram says **"🤖 Paper trader started."**
- `https://<ip-with-dashes>.sslip.io` asks for your login, then shows the dashboard with live prices
- `curl -s localhost:8000/healthz` returns `{"ok":true,...}`

The service restarts automatically if it crashes (`Restart=always`) and starts again after a reboot.

---

## 7. First week checklist

| Day | Do this |
|---|---|
| 1 | Optionally set trading mode to `signals_only` (Settings → General) for the first few days, to watch what each account *would* do before it trades |
| 1 | Confirm the nightly backup arrives in Telegram (00:30 UTC, 5:30am PKT) |
| 2–3 | Read the Signals & AI decisions table. Are the skips sensible? Is the AI's reasoning readable? |
| 3 | Switch trading mode to `auto` |
| 7 | Dashboard budget card: is month-to-date AI spend on track for < $2? Is the server bill as expected? |
| 7 | First weekly playbook arrives on Sunday 12:00 UTC (5pm PKT). Read it |

---

## 8. Updating the code later

```bash
# on your laptop: commit + push
ssh root@YOUR_IP 'bash /opt/trade-broski/deploy/update.sh'
```

`update.sh` pulls, installs requirements and **runs the tests**. It only restarts the service if the tests pass. Open positions and all history live in SQLite, so a restart loses nothing. Stops are re-checked within seconds of the restart.

---

## Where things live on the server

| Path | What |
|---|---|
| `/opt/trade-broski` | code and `.env` (mode 600) |
| `/opt/trade-broski/data/trader.db` | everything: account, trades, settings history, news, costs |
| `/opt/trade-broski/backups/` | last 14 nightly DB copies (each one is also sent to Telegram) |
| `/etc/systemd/system/trade-broski.service` | service definition |
| `/etc/caddy/Caddyfile` | HTTPS config |
| `journalctl -u trade-broski` | logs |
