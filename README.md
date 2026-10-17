# AI Paper Trader v2: XM-style paper trading, three modes side by side

A 24/7 **paper-trading** bot for GOLD, EURUSD, GBPUSD, BTCUSD (and optionally USDJPY), modelled on an **XM Standard MT5 account at 1:1000**: lots, contract sizes, spreads in points, swaps, margin and stop-out. It runs **three $250 paper accounts at the same time** on the same live prices, so you can compare trading styles:

| Account | Style |
|---|---|
| **Standard** | M15, 1% risk, A-grade setups only, skips trades the 0.01 lot can't size correctly |
| **Moderate** | M15, 1.5% risk, accepts 0.01 lot up to 3% risk, news-momentum trades |
| **Aggressive scalper** | M5, 2% risk, quick targets, breakeven at +1R, stops for the day at +3% |

Prices are free and need no keys:

| Symbol | Live price source | Traded |
|---|---|---|
| GOLD | Binance XAUUSDT gold perpetual, WebSocket (Bybit fallback) | ~23h/day, Mon–Fri |
| EURUSD, GBPUSD | Kraken, WebSocket | 24h/day, Mon–Fri |
| USDJPY (off by default) | Yahoo Finance, polled every 15s | 24h/day, Mon–Fri |
| BTCUSD | Binance futures WebSocket (Bybit fallback) | 24/7 |

XM's spreads are applied on top of the feed price. Gold and forex freeze at Friday's close like on MT5, and weekend candles are dropped.

**How it decides.** Free coded signals check every closed candle (M15, or M5 for the scalper). When a signal passes the news and risk checks, **Claude Haiku 5.5** is asked TAKE or SKIP, judging it against **that account's own playbook**. The risk engine then sizes the trade in lots, inside hard caps that the AI cannot change. Each account learns from its own trades: daily lessons, then a weekly playbook rewrite.

> **This is paper trading with virtual money.** It is not financial advice. The app refuses to start unless `PAPER_ONLY=true` and contains no live-order code. Most leveraged retail accounts lose money; prove an edge on paper for months first.

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
| `/` **Terminal** | MT5-style: account tabs (Standard / Moderate / Aggressive / ⚖ Compare), Market Watch (bid, ask, XM spread, live/closed, 0.01-lot risk), candlestick chart (M5/M15) with entry/SL/TP lines, Toolbox with **Trade · History · Journal · Scorecard · Playbook · AI & budget · Account**, and an MT5 status bar (Balance, Equity, Margin, Free margin, Margin level) |
| `/settings` **Settings** | The three modes side by side (every rule editable per mode, reset to default), XM broker specs (leverage, spreads, swaps, stop-out), instruments, news, AI budget. Changes apply live and every version is kept for rollback |
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
| Haiku 5.5: trade gate, 3 accounts (~30–60 calls/day) | ~0.60 |
| Haiku 5.5: hourly news digest (shared) | ~0.30 |
| Haiku 5.5: daily review and weekly playbook, per account | ~0.30 |
| Binance, Bybit, Kraken, Yahoo, Dukascopy, Telegram, news feeds, calendar, Caddy/sslip.io HTTPS | 0 |
| **Total** | **~$3.70** (AWS free plan: ~$1.20) |

The AI can never push the bill past $8, because four separate guards stop it: a daily call cap, a monthly $ budget in settings (hard max $5), a Console spend limit, and prepaid credit with auto-reload off. Details are in [docs/OPERATIONS.md](docs/OPERATIONS.md#costs).
