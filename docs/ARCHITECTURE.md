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
                │ static/*.html: terminal, settings, news (lightweight-charts)│──► Caddy (HTTPS) ──► you
                └──────────────────────── SQLite: data/trader.db ────────────┘
```

## Modules

| File | Responsibility |
|---|---|
| `app/main.py` | App wiring, lifespan (starts feeds and loops), scheduled jobs, HTTP Basic auth, all API routes |
| `app/config.py` | `.env` → `ENV` (secrets, paths, `PAPER_ONLY`). No trading rules here |
| `app/settings_store.py` | Global + per-mode rules, the 3 mode defaults, field schemas with **hard limits**, validation, versioning, `cfg_for(mode)` |
| `app/instruments.py` (EXTRA_INSTRUMENTS) | Research-only candidate markets (silver, US30/US100/US500, GER40, UK100, WTI, EURJPY, GBPJPY, AUDUSD) with estimated specs; promoted to INSTRUMENTS with a live feed when they pass the 2-year test |
| `app/instruments.py` | The 5 symbols as XM defines them: MT5 name, digits/point, contract size, lot limits; P&L and pip-value maths |
| `app/sessions.py` | Market hours, London/NY sessions, Asian range, weekly close, rollover (DST-aware) |
| `app/indicators.py` | EMA, RSI, ATR and percentile in pure Python |
| `app/signals.py` | Filters, regime detection, the strategies, H1 trend filter (`htf_trend`). `raw_signals` (filters + every strategy, with features) then `evaluate` (allowlist, exit profile, per-setup rules, conflicts, H1, SMC) - the live engine, backtest and study all run this code |
| `app/smc.py` | Smart Money Concepts as rules: PDH/PDL, session highs/lows, liquidity pools and equal highs/lows, sweeps, FVGs, order blocks; location filter; AI context text |
| `app/risk.py` | Lot sizing (0.01 step, min-lot rule), leverage and margin caps, portfolio rules, loss limits and daily target, day rules (trades/day, losing streak, protect a good day), cost filter |
| `app/broker.py` | XM paper broker for all accounts: XM spreads, fills, stops and gaps, breakeven, partial closes, swap in points, stop-out |
| `app/engine.py` | Loops over the 3 accounts: pre-checks, AI gate flow, weekend logic, feed health, snapshots, controls |
| `app/ai/client.py` | The only place that calls Anthropic: two tiers (Haiku / Sonnet), budget guards per tier, input caps, structured JSON, cost log |
| `app/ai/gate.py` | Gate prompt, JSON schema, reply validation, Sonnet second-opinion trigger and prompt |
| `app/ai/review.py` | Daily lessons (Haiku), weekly playbook (Sonnet, Haiku fallback), seed playbook |
| `app/news/sources.py` | Economic calendar, RSS parsing and instrument tagging, fear & greed, blackout lookup |
| `app/news/digest.py` | Hourly AI news digest: bias and risk per instrument |
| `app/scorecard.py` | Win rate and average R per signal and instrument, auto-disable and re-enable |
| `app/alerts.py` | Telegram send and file upload, command polling |
| `app/feeds/__init__.py` | Feed registry: builds the Binance, Kraken and Yahoo feeds; `fetch_candles` dispatches by instrument source |
| `app/feeds/binance.py`, `kraken.py`, `yahoo.py` | Live quotes (`run_stream`) and 15m candles (`fetch_candles`) per source |
| `app/feeds/swissquote.py` | Spot gold quote (XM's GOLD is spot; the Binance perp is ~$7 higher). Gold candles come from the perp shifted by the live perp-spot gap; if Swissquote drops, the quote falls back to perp minus the gap. US30/US100 come from Yahoo futures, ~10 min delayed (candles too, so signal and fill stay consistent) |
| `app/feeds/dukascopy.py` | Free 2-year gold/forex history for backtests (minute candles aggregated to 15m) |
| `app/backtest.py` | History download and cache; `collect_signals` (run once) + `simulate` (cheap replay) with the same signal and risk code |
| `app/adaptive.py` | Per-account strategy × regime scores: active / cautious / off / probation, risk multiplier |
| `app/exits.py` | Trade management shared by backtest and live broker: stall exit, breakeven, partial take-profit, trailing stop |
| `app/shadow.py` | Shadow outcomes: follows every skipped signal to stop/target/48h; summary by skip reason |
| `app/audit.py` | The floor manager: signal funnel, losing-streak statistics, drift vs backtest, health, exploration budget; daily report |
| `app/ai/postmortem.py` | Haiku post-mortem of each losing trade: bad luck or bad decision |
| `app/ai/outlook.py` | Weekly outlook (Sun 18:00 UTC): Sonnet + web search researches the week ahead, then a per-instrument plan (bias, drivers, events with times, levels, setups that suit the week, caution days). Floor page + Telegram; one line per instrument goes into the AI gate as context |
| `app/ai/coach.py` | Skipped-winner reviews (fair skip / too cautious), winner reviews (good process / lucky), daily coaching note, weekly lesson curation (max 10 active lessons, read by the AI gate with the playbook) |
| `app/study.py` | The 2-year setup study (`python -m app.study grid`): scans each market once with every strategy, caches the signals with their features (`data/history/scan-<SYMBOL>.pkl`, only new candles are scanned later), replays every setup × exit profile × account at $250, both-years split; PROVEN and AGGRESSIVE bars |
| `app/hypothesis.py` | Walk-forward rule tester: year-1 breakdown of a setup by hour/day/regime/side/volatility/ADX (`profile`), and baseline vs one-knob rule on year 2, which the AI never saw (`test`) |
| `app/ai/hypothesis.py` | Weekly: Sonnet reads loss causes, skipped-setup outcomes and the year-1 breakdowns and proposes up to 3 rules from a fixed menu (`signals.RULE_KNOBS`); main.py tests each and turns passes into `rule` proposals |
| `app/lab.py` | The Lab as the learning account: loss-cause tags for every losing trade (all accounts), refills to the start balance and seasons, the Floor's learning-loop summary |
| `app/promoter.py` | Lab league table, allowlist candidates/removals, 2-year both-years verification (`python -m app.promoter verify ...`) |
| `app/ai/research.py` | Weekly research review by Sonnet (Haiku fallback) |
| `app/tuner.py` | Weekly walk-forward tuning of stop/target/breakeven; runs as a separate low-priority process |
| `app/check.py` | Pre-flight connectivity check |

## Database (SQLite, WAL mode, schema version 3)
On first start after an upgrade from the single-account version, the old file is **moved aside** (`trader.db.v0-<time>.bak`, kept) and a fresh 3-account database is created.

| Table | Contents |
|---|---|
| `accounts` | one row per mode: cash (MT5 Balance), equity, peak, starting balance, status (active / paused / paused_daily / paused_weekly / target_hit / halted) |
| `prices` | M5 and M15 OHLC per symbol (`tf` column); weekend candles for gold/forex are dropped |
| `positions` | open positions: account, lots, units, entry, stop, initial stop, target, margin, risk $, swap, signal, AI reason (id = MT5 ticket) |
| `trades` | closed trades: account, ticket, lots, P&L $ (net), P&L R, swap, exit reason, decision id, best move (MFE, R), loss-cause tags |
| `decisions` | every signal per account: taken or not, the reason, Haiku's raw reply, the plan (for shadow outcomes) and the signal's features (hour, day, regime, ADX, volatility, H1 trend, SMC verdict) |
| `cost_log` | every AI call: purpose (`gate:<mode>`, `news_digest`, `daily_review:<mode>`…), tokens, $ |
| `equity_snapshots` | every 15 min per account, plus BTC and gold buy-and-hold benchmarks |
| `playbook_versions`, `lessons` | the learning loop, per mode |
| `settings_versions` | full settings JSON per save |
| `disabled_signals` | auto-disabled signals per account and symbol |
| `news_items`, `calendar_events`, `news_digests` | the news pipeline |
| `strategy_state` | per account, strategy and regime: status, risk multiplier, trades, avg R, why, until |
| `tuning_runs` | every weekly tuning result: settings before, best found, train/test R, applied or not, why |
| `lab_seasons` | the Lab's seasons: start/end, equity, why it was refilled |
| `hypotheses` | weekly rule hypotheses: setup, rule, the AI's reason, passed/failed on unseen data, the proposal it became |
| `kv` | engine state: day/week start equity per account, benchmark start prices |

## API (all routes need HTTP Basic auth except `/healthz`)

| Method & path | Purpose |
|---|---|
| `GET /api/state?account=` | that account, all accounts' summaries, its positions, Market Watch quotes (XM bid/ask/spread, 0.01-lot risk), AI budget, feeds |
| `GET /api/stream?account=` | server-sent events every 1s: quotes, account, position profit |
| `GET /api/candles?symbol=&tf=` | M5/M15 candles for the chart |
| `GET /api/audit?account=` / `POST /api/audit/run` | Today's audit, the last 14 daily reports, post-mortems / run now |
| `GET /api/outlook` / `POST /api/outlook/run` | Latest weekly outlook / write it now |
| `GET /api/floor` (page `/floor`) | All four traders side by side: today's audit and signal funnel, decision log, recent trades, post-mortems, skipped-winner and winner reviews, coach's note, active lessons, last 7 daily grades, and the week in R: trades taken vs what every skipped setup would have made |
| `GET /api/loop` | The learning loop for the Floor page: Lab season, P&L with refills, loss causes (7 days), recent hypotheses and their test results |
| `GET /api/research` | Proposals, Lab league table, latest research review, allowlists |
| `POST /api/research/run` | Run the weekly research job now |
| `POST /api/proposals/{id}/approve` or `/reject` | Decide a proposal: allowlist add/remove, AI confidence, or a tested per-setup rule (approve edits that account's settings) |
| `GET /api/levels?symbol=&tf=` | SMC map for the chart: levels (swept or not), FVGs and order blocks on the chart timeframe and on H1 (`tf: "H1"`) |
| `GET /api/compare` | per-account stats: equity, trades, win rate, avg R, avg %/day, max drawdown |
| `GET /api/equity`, `/api/trades?account=`, `/api/decisions?account=`, `/api/scorecard?account=`, `/api/playbook?mode=`, `/api/news` | data for the tabs |
| `GET/PUT /api/settings` | schema (global + per-mode fields) and values / save (validated and clamped) |
| `POST /api/settings/reset/{mode}`, `/api/settings/revert/{id}` | reset one mode / roll back |
| `PUT /api/playbook`, `POST /api/playbook/rollback/{id}` | edit or roll back a mode's playbook |
| `POST /api/control/{pause,resume,close_all,kill,close_position,reset_account}?account=` | account controls |
| `GET /api/learning?account=` | regimes now, strategy states, results by regime, tuning runs |
| `POST /api/learning/reset`, `POST /api/tune/{mode}` | reset a strategy's state / run the tuner now |
| `POST /api/news/digest` | force a news digest now |

## Two AI models

| | Claude Haiku 5.5 ("fast") | Claude Sonnet 5.5 ("strong") |
|---|---|---|
| Price per M tokens in / out | $0.10 / $0.50 | $2 / $10 (20× Haiku) |
| Used for | every trade gate, daily review per mode, hourly news digest | **second opinion** on high-stakes trades, **weekly playbook** per mode |
| Thinking | off, effort low | adaptive; effort low (second opinion), medium (playbook) |
| Typical cost per call | ~$0.0003 | ~$0.008 (second opinion), ~$0.03–0.05 (playbook) |
| Budget | the total monthly budget (default $6, hard max $8) | its own sub-budget (default $3, hard max $5) and max 10 calls a day |

**When Sonnet is asked (`gate.needs_second_opinion`):** only after Haiku says TAKE, because a SKIP costs nothing to double-check, and only if one of these is true:
- the 0.01-lot minimum forces risk above the mode's normal %;
- the trade risks ≥ 1.5% of equity;
- Haiku's confidence is within 0.1 of the minimum.

Sonnet can veto (the Journal shows "Sonnet veto") or approve with its own stop/target; approved trades show "[Sonnet ✓]". When Sonnet is over budget or off, Haiku's decision stands. If Sonnet errors on a high-stakes trade, the trade is skipped (fail closed).

**Expected monthly cost:** Haiku about $1–1.5, Sonnet second opinions up to about $2.50, weekly playbooks about $0.50. The budgets are hard stops, so the worst case is the total budget.

## Dashboard rendering (why it doesn't flicker)
- Market Watch rows, account buttons and the stats strip are **built once**. Each 1-second stream update changes only the text that differs, and only the bid that moved flashes.
- Stream updates are batched into **one render per animation frame**.
- Tables re-render only when their HTML actually changed, and they **keep their scroll position**.
- The live candle uses the mid price and **server time** (`ts` in the stream). Closed candles are pulled every minute and patched in place; there are no full chart reloads.
- FVG and OB zones are positioned divs over the chart, repositioned every 250 ms and on scroll or zoom.

## Design rules worth keeping
- **The AI never touches risk.** Sizing, caps, limits and the kill switch all live in `risk.py` and `engine.py`.
- **Pay for AI last.** Every free check (filters, news blackout, day rules, cost filter, portfolio, margin dry-run) runs before the gate, and Sonnet only runs after Haiku has approved.
- **Fail closed.** API errors, invalid JSON, stale prices or an unknown state all mean no trade.
- **Settings are data, limits are code.** The UI can change values but never the `max` in `FIELDS`.
- **The backtest uses the same `signals.evaluate` and `risk.size_order`** as the live engine.
