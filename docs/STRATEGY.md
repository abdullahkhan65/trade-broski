# Strategy: how a trade happens

```
every 3s     live bid/ask ──► stops / targets / breakeven / stop-out / limits   (all 3 accounts)
every 60s    new closed candle of the account's timeframe (M15 or M5)?
               ──► filters ──► signal? ──► pre-checks ──► Haiku TAKE/SKIP (with that account's playbook)
               ──► lot sizing ──► paper broker  (done separately for each account)
every 10m    RSS headlines ──┐
hourly       econ calendar ──┼──► AI news digest (bias + risk per instrument) ──► used by pre-checks, prompt and news signal
daily        fear & greed  ──┘
00:05 UTC    daily summary + AI review per account (lessons) + auto-disable losing signals
Sun 12:00    weekly AI playbook rewrite, one per account
```

## 1. Prices (24/7)
- **Gold**: the Binance **XAUUSDT gold perpetual** WebSocket pushes every bid/ask change. It tracks spot gold closely. Bybit's XAUUSDT is the fallback.
- **EUR/USD, GBP/USD**: the Kraken WebSocket (v2 `ticker`) pushes every bid/ask change.
- **USD/JPY** (off by default): Yahoo Finance, polled every 15s. Kraken's yen market is too thin, and OANDA doesn't accept Pakistan.
- 15m candles are refreshed from each source's REST API every minute.
- **Honesty adjustments.** Exchange spreads are tighter than a retail broker's, so each gold/forex quote is widened to a minimum spread (Settings → Costs; gold $0.30, EUR 0.8 pip, GBP 1.2 pips). These venues also quote at weekends, but the bot ignores weekend quotes for stops, just as a real broker wouldn't fill then. Gold and forex are only *traded* from Sunday 5pm to Friday 5pm New York, with gold pausing for an hour each day at 5pm NY.
- **BTC**: the Binance futures WebSocket sends the best bid/ask on every change; it's simulated as XM's BTCUSD CFD (swap, no funding). If Binance is unreachable, Bybit's public API is polled every 2 seconds instead.
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

## 5. Risk engine: XM lot sizing (code, the AI can't override it)
```
risk $          = equity × the account's risk %
loss per 1 lot  = stop distance × contract size     (EURUSD 100,000 · GOLD 100 oz · BTCUSD 1)
lots            = risk $ ÷ loss per 1 lot, rounded DOWN to 0.01
lots < 0.01?    → mode's rule: skip, or take 0.01 if its risk ≤ "allow up to" %
leverage        = notional ÷ equity  ≤ the mode's cap (lots reduced to fit)
margin          = notional ÷ XM leverage (1:1000 FX/gold, 1:50 BTC by default)
total margin    ≤ the mode's max margin used %
```
Example, Standard account ($250, 1%): EURUSD ATR 8 pips → stop 12 pips → $120 per lot → $2.50 ÷ $120 = 0.0208 → **0.02 lots**, risking $2.40, margin $2.24.
Gold on M15 (ATR ~$8 → stop $12): 0.01 lot already risks $12 = 4.8%. Standard **skips** it, Moderate (≤ 3%) skips it, Aggressive (≤ 5%) takes 0.01 lot. See SETTINGS.md → "The 0.01-lot problem".

## 6. Paper broker (XM Standard simulation)
- **Quotes:** the feed's mid price, with XM's spread in points around it (GOLD 35, EURUSD 16, GBPUSD 21, BTCUSD 4000). If the live spread is wider, the live one is used. Buys fill at the ask, sells at the bid. No commission (XM Standard).
- **Market hours:** gold and forex quotes **freeze at Friday's close** and weekend candles are dropped, as on MT5. Stops aren't checked while the market is closed, so a held position takes the Monday gap.
- **Stops:** checked every 3 seconds against bid/ask. **If price gapped through the stop, the fill is the first price seen**, marked "stop loss (gap)". Targets fill at the target price exactly, never better.
- **Breakeven:** per mode, at +X R the stop moves to the entry price ("breakeven stop" on exit).
- **Swap:** XM points per lot per night at 17:00 New York. FX and gold are charged ×3 on Wednesday and not at weekends; BTCUSD is charged every night.
- **Margin call and stop-out:** at 20% margin level the biggest loser is closed, as XM does.
- **Friday close:** gold/forex positions are closed or halved before the weekend, per mode. A 0.01 lot can't be halved, so it's closed.
- **Daily target / limits:** per account. Hitting the daily profit target or loss limit stops new trades until the next UTC day.

## 7. Learning loop (per account)
1. **Per trade:** account, lots, signal, Haiku's reason, entry and exit, P&L in $ and in R, and swap are stored.
2. **Daily (00:05 UTC):** for each account, Haiku reads yesterday's trades and skipped signals **for that account** and writes 3–5 lessons.
3. **Weekly (Sun 12:00 UTC):** each account's playbook is rewritten from its own lessons and scorecard, staying true to its style. Old versions are kept, and you can edit or roll back.
4. **Scorecard (daily, per account):** a signal with a negative average R after 30+ trades on a symbol is **switched off for that account**.
5. **Backtest before changing rules:** `python -m app.backtest --mode all --symbol all --days 365`.

## 8. First backtest result (honest)
Run on 17 Oct 2026 on BTC:

```
BTC_USDT · 120 days · 1% risk (before the XM/3-mode rebuild)
Trades: 244 | win rate 31.6% | avg R -0.273
Final equity $125.35 (-49.9%) | fees $93.38
Buy & hold (unleveraged): +30.3%
```

The raw `trend_pullback` signal on BTC 15m **has no edge**, and costs make it worse. With 1% risk and a 15m ATR stop, BTC positions are small, so fees plus slippage come to about 0.3R per round trip. The AI gate can only filter trades; it can't turn a losing signal into a winning one. Before trusting BTC paper results, see NEXT_STEPS.md → "Strategy work". Gold and forex haven't been backtested yet. Run `python -m app.backtest --symbol all --days 730` (the first run downloads the history).
