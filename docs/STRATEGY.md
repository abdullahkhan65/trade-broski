# Strategy: how a trade happens

```
every 5s     live bid/ask ──► stops / targets / stop-out / loss limits
every 60s    new closed 15m candle? ──► filters ──► signal? ──► pre-checks ──► Haiku TAKE/SKIP ──► risk engine ──► paper broker
every 10m    RSS headlines ──┐
hourly       econ calendar ──┼──► AI news digest (bias + risk per instrument) ──► used by pre-checks, prompt and news signal
daily        fear & greed  ──┘
00:05 UTC    daily summary + AI review (lessons) + auto-disable losing signals
Sun 12:00    weekly AI playbook rewrite
```

## 1. Prices (24/7)
- **Gold**: the Binance **XAUUSDT gold perpetual** WebSocket pushes every bid/ask change. It tracks spot gold closely. Bybit's XAUUSDT is the fallback.
- **EUR/USD, GBP/USD**: the Kraken WebSocket (v2 `ticker`) pushes every bid/ask change.
- **USD/JPY** (off by default): Yahoo Finance, polled every 15s. Kraken's yen market is too thin, and OANDA doesn't accept Pakistan.
- 15m candles are refreshed from each source's REST API every minute.
- **Honesty adjustments.** Exchange spreads are tighter than a retail broker's, so each gold/forex quote is widened to a minimum spread (Settings → Costs; gold $0.30, EUR 0.8 pip, GBP 1.2 pips). These venues also quote at weekends, but the bot ignores weekend quotes for stops, just as a real broker wouldn't fill then. Gold and forex are only *traded* from Sunday 5pm to Friday 5pm New York, with gold pausing for an hour each day at 5pm NY.
- **BTC**: the Binance futures WebSocket sends the best bid/ask on every change, plus the mark price and funding rate every second. If Binance is unreachable, Bybit's public API is polled every 2 seconds instead.
- The dashboard shows prices live through a server-sent event stream that updates every second.

## 2. Signal engine (free)
Runs on **closed** 15m candles only, so a candle that is still forming can't trigger a trade.

| Signal | Long when… | Short when… |
|---|---|---|
| `trend_pullback` | close > 50 EMA and 20 EMA > 50 EMA; a recent low touched the 20 EMA (± ¼ ATR); RSI(14) turned up (and < 70); close back above the 20 EMA | the mirror image |
| `session_breakout` (gold/forex) | **first** 15m close above the Asian-session high (00:00–07:00 UTC) | first close below the Asian low |
| `news_momentum` (off by default) | AI news bias ≥ +threshold **and** close > 20 EMA > 50 EMA | bias ≤ −threshold and close < 20 EMA < 50 EMA |

Filters applied to every signal:
- **Dead market**: ATR(14) is below its 20th percentile of the last ~20 days.
- **News spike**: the last candle's range is more than 3× ATR.
- **Session**: gold/forex only trade during London (08:00–17:00 London time) or New York (08:00–17:00 NY time) hours. Daylight saving is handled.
- If two signals disagree on direction, neither is taken.

## 3. Pre-checks (free; run *before* paying for AI)
Any of these cancels the trade, and the reason is logged in the dashboard's decisions table:
1. The bot is paused or halted.
2. It's too close to the Friday close (gold/forex).
3. **News blackout**: a high-impact event for that instrument's currencies is within ±15 min.
4. **News risk extreme**: the latest AI digest flagged extreme risk for the instrument.
5. Portfolio rules: already in this instrument, max positions reached, or max USD forex positions reached.
6. Cooldown: Haiku skipped this signal type on this instrument within the last 2h.
7. No fresh live price (more than 2 minutes old).
8. A dry-run size check fails, e.g. the margin cap would be breached.

## 4. AI gate (Claude Haiku 5.5, about $0.0004 per call)
The prompt is ≤ 4k tokens. It contains the signal, the last 48 candles in compact form, ATR, spread, session, account and margin, open positions, the last 10 trades, the scorecard line for this signal, **the news digest for this instrument, the high/medium-impact events in the next 4h, the latest relevant headlines** and the current playbook.

The reply is forced to strict JSON with structured outputs:
```json
{"decision": "TAKE", "symbol": "XAU_USD", "side": "LONG", "leverage": 3, "stop_atr": 1.5,
 "target_atr": 3, "confidence": 0.58, "reason": "Pullback to 20 EMA in uptrend, London open"}
```
Then:
- `SKIP`, a side flip, or confidence below the minimum means no trade (and a 2h cooldown for that signal).
- Invalid JSON, API errors, a refusal or a reply cut off at max_tokens means **SKIP**.
- If the budget is spent or AI is disabled, the bot falls back to `rules_only` or `skip`, per your settings.
- `stop_atr` and `target_atr` are clamped to your limits. `leverage` is advisory only: **size always comes from the stop distance**.

Thinking is switched off and effort is low. These are short judgement calls, and thinking tokens would be billed and could eat the 400-token reply cap.

## 5. Risk engine (code, the AI can't override it)
```
risk $        = equity × risk%                          (1% of $250 = $2.50)
units         = risk $ ÷ stop distance in $ per unit
leverage      = notional ÷ equity                       must be ≤ cap, else skip (or shrink)
margin        = notional ÷ broker margin leverage       (30:1 forex, 20:1 gold & BTC)
total margin  ≤ max margin used %
```
Example: EUR/USD at 1.1000 with ATR 8 pips gives a stop of 12 pips, 2,083 units, $2,290 notional, **9.2x leverage** and $76 margin (31% of equity).

**Why margin isn't "notional ÷ your cap".** On 15m candles, a 1%-risk trade with a 1.5×ATR stop typically needs 5–10x leverage. If margin were notional ÷ 10, a single EUR/USD trade would use about 92% of equity, and the "≤ 50% margin" rule would block nearly every trade. Real brokers charge margin at their own rate (OANDA Europe: 30:1 on majors, 20:1 on gold), so the simulator does the same. Your leverage cap still limits how big each position can be.

## 6. Paper broker
- Buys fill at the ask and sells at the bid, with the spread widened to at least the broker minimum. BTC also pays 0.02% slippage and a 0.05% taker fee on each side.
- Stops are checked every 5s against bid/ask. **If price has gapped through the stop, the fill is the first price seen, not the stop**, and the trade is marked "stop (gap)". Targets fill at the target price exactly, never better.
- Swap (gold/forex) is charged daily at 5pm New York, ×3 on Wednesday, using the rates from settings.
- Funding (BTC) is charged every 8h at Binance's live funding rate. When the rate is positive, longs pay.
- Stop-out: if margin level falls below 50%, the largest loser is closed.
- Friday close: gold/forex positions are closed or halved 30 min before the weekend (per settings).

## 7. Learning loop
1. **Per trade**: the signal, Haiku's reason, entry and exit, P&L in $ and in R, fees, swap and funding are all stored.
2. **Daily (00:05 UTC)**: Haiku reads yesterday's trades and skipped signals and writes 3–5 lessons. You get a Telegram summary.
3. **Weekly (Sun 12:00 UTC)**: Haiku rewrites the playbook (max 12 one-line rules) from the week's lessons and the scorecard. Old versions are kept, and you can edit or roll back.
4. **Scorecard (daily)**: a signal type with a negative average R after 30+ trades on an instrument is **switched off automatically**.
5. **Backtest before changing rules**: `python -m app.backtest --symbol all --days 730`. An AI can easily "learn" patterns that were just luck.

## 8. First backtest result (honest)
Run on 17 Oct 2026 on BTC:

```
BTC_USDT · 120 days · balanced profile
Trades: 244 | win rate 31.6% | avg R -0.273
Final equity $125.35 (-49.9%) | fees $93.38
Buy & hold (unleveraged): +30.3%
```

The raw `trend_pullback` signal on BTC 15m **has no edge**, and costs make it worse. With 1% risk and a 15m ATR stop, BTC positions are small, so fees plus slippage come to about 0.3R per round trip. The AI gate can only filter trades; it can't turn a losing signal into a winning one. Before trusting BTC paper results, see NEXT_STEPS.md → "Strategy work". Gold and forex haven't been backtested yet. Run `python -m app.backtest --symbol all --days 730` (the first run downloads the history).
