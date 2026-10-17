# Operations runbook

## Daily (2 minutes)
- Read the **Telegram daily summary** (05:05 PKT): equity, trades, AI lessons.
- If something looks off, open the dashboard and look at **Signals & AI decisions** to see why trades were taken or skipped.

## Weekly (10 minutes, Sunday evening PKT)
- Read the **playbook update** in Telegram and on the dashboard. Roll it back if a rule looks wrong.
- Check the **scorecard**. Is any signal auto-disabled? Is the average R positive anywhere?
- Check the **budget card**: AI month-to-date spend.
- Update the **swap rates** in Settings → Costs from your intended broker's swap table, roughly once a month.

## Telegram commands
`/status` · `/pause` · `/resume` · `/closeall` · `/help`. Only your `TELEGRAM_CHAT_ID` is obeyed.

## Alerts you can get
| Alert | Meaning / what to do |
|---|---|
| 🟢 Opened / ✅ / 🔴 Closed | normal trading |
| 👀 Would open | `signals_only` mode is on |
| ⏸ Paused (paused_daily / paused_weekly) | loss limit hit. Lifts automatically at the next UTC day or on Monday |
| 🛑 KILL SWITCH | 20% drawdown from peak, and everything is closed. Review before pressing **Resume** |
| ⚠️ Data feed down | no price for 5+ min while the market is open. Check `journalctl -u trade-broski`; the feed reconnects by itself |
| Nightly backup file | your off-server DB copy. Keep the chat |

## Common commands (on the server)
```bash
systemctl status trade-broski          # running?
journalctl -u trade-broski -f          # live logs
journalctl -u trade-broski --since "1 hour ago" | grep -iE "error|warn"
systemctl restart trade-broski         # safe: state is in SQLite
cd /opt/trade-broski && sudo -u trader .venv/bin/python -m app.check
sqlite3 data/trader.db "select symbol, side, pnl_usd, exit_reason from trades order by id desc limit 10"
```

## Backups and restore
- Every night at 00:30 UTC a consistent copy goes to `backups/trader-YYYYMMDD.db` (the last 14 are kept) **and** is sent to your Telegram chat.
- To restore:
  ```bash
  systemctl stop trade-broski
  cp /opt/trade-broski/data/trader.db /opt/trade-broski/data/trader.db.broken
  cp /path/to/trader-YYYYMMDD.db /opt/trade-broski/data/trader.db
  rm -f /opt/trade-broski/data/trader.db-wal /opt/trade-broski/data/trader.db-shm
  chown trader:trader /opt/trade-broski/data/trader.db
  systemctl start trade-broski
  ```
- To **start the paper account over** at $250: stop the service, move `data/trader.db` away and start again. Settings go back to defaults, so note yours first.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Gold/forex show "closed" | It's the weekend or gold's daily break at 5–6pm NY. Prices keep moving (Binance/Kraken trade 24/7), but the bot won't trade or trigger stops. Normal |
| A price shows "stale" | That feed is reconnecting. Check `journalctl -u trade-broski` and run `python -m app.check`. USD/JPY (Yahoo) is polled, so it updates less often |
| Backtest download is slow or times out | Dukascopy is slow (about 5s per day). Re-run it: finished days are cached |
| BTC feed keeps reconnecting, log says 451 | Binance blocks the server's region. The Bybit fallback is used automatically; consider a server in another region |
| No trades for days | Look at "last check" under each price on the dashboard (e.g. "outside London/NY session", "dead market") and at the decisions table. Conservative settings and the margin cap skip a lot by design |
| "AI blocked: daily AI call cap reached" | Raise the cap in Settings → AI, or lower the news digest frequency |
| "AI error -> SKIP" | Check the Anthropic Console (credit, spend limit). The bot keeps protecting open positions without AI |
| Dashboard asks for login repeatedly | Wrong `DASHBOARD_USER`/`DASHBOARD_PASSWORD`. After editing `.env`, run `systemctl restart trade-broski` |
| Caddy has no HTTPS | Ports 80/443 must be open, and the sslip.io name must match the server IP exactly |

## Costs
Hard guards that keep the bill under the $8 cap:
1. **Anthropic prepaid credit** ($5) with **auto-reload off**. The API simply stops when the credit runs out.
2. **A monthly spend limit in the Anthropic Console** ($2).
3. **In code**: a max number of AI calls per day (default 60), the monthly AI budget from settings (default $2, hard max $5), ≤ 4k input tokens per call, and max_tokens 400 (600 for the digest, 800 for the playbook). Over a limit, the bot trades on rules only or skips, and the dashboard shows why.
4. **`cost_log`** records tokens and $ for every call. The dashboard budget card shows month-to-date spend broken down by purpose.
5. **Fixed-price VPS**, with no usage-based hosting.

Expected AI usage at the defaults: gate ~10–20 calls/day plus digest ~20/day plus review 1/day ≈ 30–45 calls/day ≈ **$0.60–0.80/month**.
