# AI Paper Trader v2: gold, forex and BTC on a $8/month budget

A 24/7 **paper-trading** bot for 5 instruments. It trades leveraged long and short on a $250 virtual balance, and runs on a ~$2.50/month VPS.

| Instrument | Live price source (free, no key) | Traded |
|---|---|---|
| XAU/USD (gold) | Binance XAUUSDT gold perpetual, WebSocket (Bybit fallback) | ~23h/day, Mon–Fri |
| EUR/USD, GBP/USD | Kraken, WebSocket | 24h/day, Mon–Fri |
| USD/JPY (off by default) | Yahoo Finance, polled every 15s | 24h/day, Mon–Fri |
| BTC/USDT perpetual | Binance futures WebSocket (Bybit fallback) | 24/7 |

OANDA isn't available from Pakistan, so prices come from public exchange feeds instead. Gold and forex still follow real market hours, pay CFD-style swap and use broker-level spreads, so the paper results stay honest. Two years of gold/forex history for backtests comes from Dukascopy's free datafeed.

**How it decides.** Free coded signals check every closed 15-minute candle. When a signal fires and passes the news and risk checks, **Claude Haiku 5.5** is asked TAKE or SKIP. The risk engine then sizes the trade from the stop distance, inside hard caps that the AI cannot change. Headlines and the economic calendar feed every decision. Lessons from closed trades are rewritten into a weekly playbook.

> **This is paper trading with virtual money.** It is not financial advice. The app refuses to start unless `PAPER_ONLY=true`, and it contains no live-order code. Leverage multiplies losses as fast as gains. The job of this MVP is to prove, over at least 3 months on paper, whether the bot beats doing nothing.

---

## Quick start (your laptop)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys - see docs/DEPLOYMENT.md §1
python -m app.check           # confirms every API key/feed works (free)
python -m app.main            # http://127.0.0.1:8000  (login from .env)
```

All prices work with no keys at all. Without an Anthropic key the bot runs on coded rules only, or skips trades, depending on your settings.

Run the tests with `pytest -q`. Backtest the signals with `python -m app.backtest --symbol all --days 730`.

## Pages

| Page | What it's for |
|---|---|
| `/` **Dashboard** | Equity, P&L, margin, live prices (updated every second), equity curve vs buy-and-hold BTC/gold, positions, trade log with Haiku's reasons, every signal and why it was taken or skipped, scorecard, playbook, AI budget, and the pause / close all / kill switch controls |
| `/settings` **Settings** | One-click **Conservative / Balanced / Aggressive** profiles, plus every rule editable on its own: risk %, leverage caps, loss limits, signals, AI confidence and budget, news blackout, weekend behaviour, swap rates. Changes apply live, every version is kept, and you can roll back |
| `/news` **News & events** | The AI's bias and risk for each instrument, the latest headlines tagged by instrument, and the economic calendar |

Telegram alerts cover trades opened and closed, a daily summary and review, loss limits, the kill switch, budget warnings, feeds going down and nightly backups. It also accepts the commands `/status /pause /resume /closeall`.

## Docs

| Doc | Read it when… |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | You're setting up the accounts and the server: step by step, about 1 hour |
| [docs/SETTINGS.md](docs/SETTINGS.md) | You want to change the rules or make the bot more or less aggressive |
| [docs/STRATEGY.md](docs/STRATEGY.md) | You want to know how signals, the AI gate, news and the learning loop work |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | You're changing the code: modules, data flow, database tables |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | It's running and you need daily checks, backups/restore, troubleshooting or costs |
| [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) | You want the roadmap, the go-live criteria, known limitations and open questions |

## Monthly cost (target $3–4, hard cap $8)

| Item | $/month |
|---|---|
| Netcup VPS nano (or Hetzner CX23 fallback ~$6.40) | ~2.50 |
| Haiku 5.5: trade gate (≤20 calls/day) | ~0.30 |
| Haiku 5.5: hourly news digest | ~0.30 |
| Haiku 5.5: daily review and weekly playbook | ~0.15 |
| Binance, Bybit, Kraken, Yahoo, Dukascopy, Telegram, news feeds, calendar, Caddy/sslip.io HTTPS | 0 |
| **Total** | **~$3.25** |

The AI can never push the bill past $8, because four separate guards stop it: a daily call cap, a monthly $ budget in settings (hard max $5), a Console spend limit, and prepaid credit with auto-reload off. Details are in [docs/OPERATIONS.md](docs/OPERATIONS.md#costs).
