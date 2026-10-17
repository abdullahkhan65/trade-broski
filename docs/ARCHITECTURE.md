# Architecture

One Python process (FastAPI + asyncio + APScheduler) and one SQLite file. There is no Docker, no separate frontend and no message queue, so it fits in about 150 MB of RAM on a 1 GB VPS.

```
                ┌──────────── app/main.py (uvicorn, one process) ─────────────┐
 Binance WS  ──►│ feeds/binance.py ┐  (BTC + gold; Bybit REST fallback)       │
 Kraken WS   ──►│ feeds/kraken.py  ┼─► feeds/prices.PriceCache (live quotes)   │
 Yahoo REST  ──►│ feeds/yahoo.py   │            │                             │
                │                  └─► prices table (15m candles)             │
                │                                │                            │
 RSS feeds   ──►│ news/sources.py ─► news_items / calendar_events             │
 FF calendar ──►│ news/digest.py  ─► news_digests  (Haiku)                    │
                │                                │                            │
                │ engine.py  fast loop (5s): broker.check_exits / stop-out /  │
                │                            equity / loss limits             │
                │            minute loop: candles → signals.py → pre-checks → │
                │                         ai/gate.py → risk.py → broker.py    │──► Telegram (alerts.py)
                │ ai/client.py: budget guard + cost_log for every AI call     │
                │ ai/review.py: daily lessons, weekly playbook                │
                │ scorecard.py: per-signal stats, auto-disable                │
                │ settings_store.py: live, versioned rules (settings page)   │
                │ static/*.html: dashboard, settings, news (Chart.js)        │──► Caddy (HTTPS) ──► you
                └──────────────────────── SQLite: data/trader.db ────────────┘
```

## Modules

| File | Responsibility |
|---|---|
| `app/main.py` | App wiring, lifespan (starts feeds and loops), scheduled jobs, HTTP Basic auth, all API routes |
| `app/config.py` | `.env` → `ENV` (secrets, paths, `PAPER_ONLY`). No trading rules here |
| `app/settings_store.py` | Trading rules: defaults, presets, field schema with **hard limits**, validation, versioning |
| `app/instruments.py` | The 5 instruments, P&L and notional maths in USD (USD/JPY conversion), broker margin rates |
| `app/sessions.py` | Market hours, London/NY sessions, Asian range, weekly close, rollover (DST-aware) |
| `app/indicators.py` | EMA, RSI, ATR and percentile in pure Python |
| `app/signals.py` | Filters + `trend_pullback`, `session_breakout`, `news_momentum` |
| `app/risk.py` | Position sizing, leverage and margin caps, portfolio rules, loss-limit status |
| `app/broker.py` | Paper broker: fills, fees, stops and gaps, targets, partial closes, swap, funding, stop-out |
| `app/engine.py` | Orchestration loops, pre-checks, AI gate flow, weekend logic, feed health, snapshots, controls |
| `app/ai/client.py` | The only place that calls Anthropic: budget guards, 4k-token input cap, structured JSON, cost log |
| `app/ai/gate.py` | Gate prompt, JSON schema, reply validation |
| `app/ai/review.py` | Daily lessons, weekly playbook, seed playbook |
| `app/news/sources.py` | Economic calendar, RSS parsing and instrument tagging, fear & greed, blackout lookup |
| `app/news/digest.py` | Hourly AI news digest: bias and risk per instrument |
| `app/scorecard.py` | Win rate and average R per signal and instrument, auto-disable and re-enable |
| `app/alerts.py` | Telegram send and file upload, command polling |
| `app/feeds/__init__.py` | Feed registry: builds the Binance, Kraken and Yahoo feeds; `fetch_candles` dispatches by instrument source |
| `app/feeds/binance.py`, `kraken.py`, `yahoo.py` | Live quotes (`run_stream`) and 15m candles (`fetch_candles`) per source |
| `app/feeds/dukascopy.py` | Free 2-year gold/forex history for backtests (minute candles aggregated to 15m) |
| `app/backtest.py` | History download and cache, offline simulation with the same signal and risk code |
| `app/check.py` | Pre-flight connectivity check |

## Database (SQLite, WAL mode)

| Table | Contents |
|---|---|
| `account` | one row: cash, equity, peak, starting balance, status (active / paused / paused_daily / paused_weekly / halted) |
| `prices` | 15m OHLC per instrument (last ~3000 kept) |
| `positions` | open positions: side, units, leverage, entry, stop, target, margin, risk $, fees/swap/funding so far, signal, AI reason |
| `trades` | closed trades: P&L $ (net), P&L R, fees, swap, funding, exit reason |
| `decisions` | every signal: taken or not, the reason, Haiku's raw reply |
| `cost_log` | every AI call: purpose, tokens, $ and errors |
| `equity_snapshots` | every 15 min: equity plus BTC and gold buy-and-hold benchmarks |
| `playbook_versions`, `lessons` | the learning loop |
| `settings_versions` | full settings JSON per save (the current one is the latest row) |
| `disabled_signals` | signal types auto-disabled per instrument |
| `news_items`, `calendar_events`, `news_digests` | the news pipeline (7 days of headlines, 30 days of digests) |
| `kv` | small engine state: day/week start equity, benchmark start prices |

## API (all routes need HTTP Basic auth except `/healthz`)

| Method & path | Purpose |
|---|---|
| `GET /api/state` | account, positions, quotes, AI budget, feed status, config warnings |
| `GET /api/stream` | server-sent events: quotes, account and position P&L every 1s |
| `GET /api/equity?days=` | equity snapshots |
| `GET /api/trades`, `/api/decisions`, `/api/scorecard`, `/api/news`, `/api/playbook` | dashboard data |
| `GET/PUT /api/settings` | read the schema and current values / save (validated and clamped) |
| `POST /api/settings/preset/{conservative,balanced,aggressive}` | apply a profile |
| `POST /api/settings/revert/{id}` | roll back settings |
| `PUT /api/playbook`, `POST /api/playbook/rollback/{id}` | edit or roll back the playbook |
| `POST /api/control/{pause,resume,close_all,kill,close_position}` | controls |
| `POST /api/news/digest` | force a news digest now |

## Design rules worth keeping
- **The AI never touches risk.** Sizing, caps, limits and the kill switch all live in `risk.py` and `engine.py`.
- **Pay for AI last.** Every free check (filters, news blackout, portfolio, margin dry-run) runs before the gate.
- **Fail closed.** API errors, invalid JSON, stale prices or an unknown state all mean no trade.
- **Settings are data, limits are code.** The UI can change values but never the `max` in `FIELDS`.
- **The backtest uses the same `signals.evaluate` and `risk.size_order`** as the live engine.
