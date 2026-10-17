# Next steps, roadmap and open questions

## Status (17 Oct 2026)

**Built (phase 1 code complete):**
- [x] Live 24/7 prices with no keys: Binance (BTC and gold perp), Kraken (EUR/USD, GBP/USD), Yahoo (USD/JPY), Bybit fallback; broker-level spreads, no weekend stops
- [x] 2-year gold/forex backtest history from Dukascopy
- [x] 15m candle store, the signal engine and a backtest script
- [x] Margin paper engine: spread, gaps, swap, stop-out, weekend close or halve
- [x] Risk engine with leverage caps, margin cap, portfolio rules, daily and weekly limits, kill switch
- [x] Haiku 5.5 gate with strict JSON, call and token caps, and `cost_log`
- [x] News: economic calendar blackout, RSS headlines tagged by instrument, hourly AI news digest, optional news-momentum signal
- [x] Three paper accounts side by side (Standard M15, Moderate M15, Aggressive scalper M5), each with its own rules, playbook and learning loop
- [x] XM Standard (MT5) modelling: lots and contract sizes, 0.01-lot minimum rules, spreads and swaps in points, 1:1000 margin, 20% stop-out, breakeven, daily profit target
- [x] MT5-style terminal (Market Watch, chart with SL/TP lines, Trade/History/Journal tabs, account bar, Compare view), settings with modes side by side, news page, Telegram alerts
- [x] Daily review, weekly playbook, scorecard with auto-disable
- [x] Nightly backups (local and Telegram), systemd, Caddy, setup and update scripts
- [x] 30 unit tests: XM lot sizing and min-lot rules, spreads, gaps, breakeven, swap, stop-out, weekend freeze, DB upgrade, settings limits, signals on M5/M15

**Not yet verified**, because it needs your keys or a server:
- [ ] A real Haiku call through the gate (budget guards and parsing are unit-level only)
- [ ] Telegram delivery
- [ ] 7 days running on the VPS

## Do this next (in order)

1. **Accounts** (DEPLOYMENT.md §1): Anthropic key with $5 prepaid and auto-reload off (done ✅), and the Telegram bot. Prices need no accounts.
2. **Run `python -m app.check` locally.** Everything should be ✅.
3. **Backtest gold and forex**: `python -m app.backtest --symbol all --days 730 --csv bt.csv` (the first run downloads history, about 20–30 min per instrument). Decide which signal/instrument pairs to keep on before paper trading starts.
4. **Buy the VPS and deploy** (DEPLOYMENT.md §3–6).
5. **Week 1:** let all three accounts run. Check the Journal tab of each to see why trades were taken or skipped. Confirm AI spend is on track.
6. **Copy XM's real numbers:** in MT5, right-click each symbol → Specification, and put the spread, swap long/short and margin into Settings → Broker.
7. **Months 1–3:** let it run, and do the weekly review in OPERATIONS.md. Don't change strategy settings more than once a week, so you can tell what helped.

## Strategy work (important)

The first backtest (BTC, 120 days) shows the raw `trend_pullback` signal **losing about 0.27R per trade**, with fees and slippage costing about 0.3R per round trip. Ideas to test **in the backtester first**, one at a time:
- **Higher timeframe for BTC**: 1h candles for BTC, so stops are wider, positions bigger relative to fees, and there are fewer trades. This needs a `timeframe` setting per instrument.
- **Trend filter from a higher timeframe**: only take 15m pullbacks in the direction of the 4h 50 EMA.
- **Fewer, better trades**: require ADX > 20, or a minimum distance between the 20 and 50 EMA.
- **Maker entries for BTC**: limit orders pay 0.02% instead of 0.05% (needs a limit-order simulation).
- **Wider target / trailing stop** for trend trades.

Expect some ideas to fail. If nothing beats buy-and-hold after honest testing, the right call is to stop or rethink, not to add leverage.

## Go-live criteria (before any real money)

All of these, over **at least 3 months of paper trading**:
- [ ] 100+ closed trades, with 30+ for every signal type you keep
- [ ] Positive average R **after** all costs, on paper and in a 2-year backtest
- [ ] Beats unleveraged buy-and-hold of BTC and of gold over the same period, on a risk-adjusted basis
- [ ] Max drawdown < 15% and no kill-switch events
- [ ] No unexplained trades: every trade in the log makes sense to you
- [ ] A broker/exchange that is legally usable from Pakistan has been chosen (see open questions)

Even then, start with an amount you can afford to lose entirely, with the rules of the account that did best on a **risk-adjusted** basis (not just the highest return).

## Roadmap (phase 2+)
| Item | Why | Effort |
|---|---|---|
| Timeframe setting per instrument (15m / 1h) | BTC costs vs. edge, see above | M |
| Telegram approval mode (tap ✅/❌ per trade) | Open question 2; a gentle start before full auto | S |
| Broker demo mirroring (spec "Option A") | Real fills, margin and swap from a broker that accepts Pakistan (e.g. an MT5 demo via a bridge) instead of simulated ones | L |
| Better USD/JPY feed | Yahoo is polled and unofficial; replace it if you want USD/JPY on | S |
| Limit-order (maker) simulation for BTC | Lower fees | M |
| Backtest with historical news blackout | The FF calendar keeps history; it makes the backtest closer to live | M |
| Monthly report email/PDF | Easier review | S |
| Swap rates fetched automatically from a broker | No manual updates | M |

## Known limitations
- **Swap rates** are estimates you maintain in Settings, unless broker mirroring is built.
- **Price sources are proxies.** Gold comes from Binance's gold perpetual and EUR/GBP from Kraken's fiat pairs. Both track the interbank market closely but aren't identical. They're also not the broker you'd trade live, so expect small differences.
- **Kraken only returns 7.5 days of 15m history** at startup. The dead-market filter uses less than 20 days until the bot has stored more.
- **Backtest** assumes the AI takes every signal and ignores news, so it tests the coded edge only. Funding and swap are approximated.
- **News digest** reads headlines only, not full articles. RSS sources can change or rate-limit, so replace dead feeds in Settings.
- **The ForexFactory calendar JSON** is an unofficial public endpoint. If it breaks, blackouts stop working (trades are not blocked), so watch for "Calendar refresh failed" in the logs.
- **Session times** use fixed local hours (London 8–17, NY 8–17), not exchange holiday calendars.
- **Single user**, HTTP Basic auth: fine behind HTTPS for one person.

## Open questions (from the spec, still yours to decide)
1. **Forex pairs.** You asked for "5 pairs" and listed 4 (XAU/USD, EUR/USD, GBP/USD, BTC). The build keeps **USD/JPY** from the spec as the 5th, but **off by default** (Yahoo is its only free source). Want a different 5th pair? EUR/GBP, USD/CAD, AUD/USD and USD/CHF are on Kraken and easy to add in `instruments.py`.
2. **Fully automatic vs. Telegram approval** in month 1. `signals_only` mode exists today; tap-to-approve is on the roadmap.
3. **Live broker from Pakistan.** Which broker or exchange is legally usable for leveraged gold, forex and BTC? Check SECP/SBP rules before any live phase.
