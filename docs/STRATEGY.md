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
- **H1 trend filter** (Standard by default): hourly candles are built from the account's own candles. Longs only when the H1 close is above EMA50 and EMA20 is above EMA50; shorts the opposite. A flat H1 means no trade.

## 3. Pre-checks (free; run *before* paying for AI)
Any of these cancels the trade, and the reason is logged in the dashboard's decisions table:
1. The bot is paused or halted.
2. It's too close to the Friday close (gold/forex).
3. **News blackout**: a high-impact event for that instrument's currencies is within ±15 min.
4. **News risk extreme**: the latest AI digest flagged extreme risk for the instrument.
5. Portfolio rules: already in this instrument, max positions reached, or max USD forex positions reached.
6. Cooldown: Haiku skipped this signal type on this instrument within the last 2h.
7. No fresh live price (more than 2 minutes old).
8. **Day rules**: the mode's max trades per day is reached, or its losing streak says "done for the day". These rules can also halve risk: after 2 losses in a row (Moderate/Aggressive), or once the day is up +3% (Aggressive).
9. **Cost filter**: the spread is more than X% of the stop (Aggressive: 15%).
10. A dry-run size check fails, e.g. the margin cap would be breached.

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

**Sonnet second opinion.** When Haiku approves a high-stakes trade, Claude Sonnet 5.5 reviews it with the same prompt plus Haiku's verdict and the exact risk. A trade is high-stakes when the 0.01-lot minimum forces extra risk, the risk is ≥ 1.5%, or Haiku's approval is borderline. Sonnet is told to look for the reason the trade fails, and it can veto it. It costs about $0.008 per call, within its own budget and daily cap. See ARCHITECTURE.md → "Two AI models".

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
- **Quotes:** the feed's mid price, with XM's spread in points around it (GOLD 50, EURUSD 23, GBPUSD 28, BTCUSD 4000). If the live spread is wider, the live one is used. Buys fill at the ask, sells at the bid. No commission (XM Standard).
- **Market hours:** gold and forex quotes **freeze at Friday's close** and weekend candles are dropped, as on MT5. Stops aren't checked while the market is closed, so a held position takes the Monday gap.
- **Stops:** checked every 3 seconds against bid/ask. **If price gapped through the stop, the fill is the first price seen**, marked "stop loss (gap)". Targets fill at the target price exactly, never better.
- **Breakeven:** per mode, at +X R the stop moves to the entry price ("breakeven stop" on exit).
- **Swap:** XM points per lot per night at 17:00 New York. FX and gold are charged ×3 on Wednesday and not at weekends; BTCUSD is charged every night.
- **Margin call and stop-out:** at 20% margin level the biggest loser is closed, as XM does.
- **Friday close:** gold/forex positions are closed or halved before the weekend, per mode. A 0.01 lot can't be halved, so it's closed.
- **Daily target / limits:** per account. Hitting the daily profit target or loss limit stops new trades until the next UTC day.

## 7. Self-learning (per account): four loops

| Loop | When | What it changes | Costs AI? |
|---|---|---|---|
| **Playbook** | daily lessons, Sunday rewrite | How Haiku judges setups for this account | yes (cents) |
| **Strategy switching by regime** | after every closed trade + daily | Which strategy may trade in which market regime, and at what risk | no |
| **Weekly walk-forward tuning** | Saturday 06:00 UTC | Default stop / target / breakeven for the account | no |
| **Trade log + scorecard** | every trade | The data all of the above learn from | no |

### Market regimes
Every candle is labelled from ADX (trend strength) and where ATR sits in its 20-day range:
`volatile` (ATR in its top 10%), `trend` (ADX ≥ 25), `range` (ADX ≤ 20), otherwise `mixed`. Market Watch shows the label for each symbol.

### Strategies
| Strategy | Fires in | Idea |
|---|---|---|
| `trend_pullback` | any | Pullback to the 20 EMA in a 50 EMA trend, RSI turns back |
| `session_breakout` | any (gold/forex) | First close beyond the Asian-session range in London/NY |
| `news_momentum` | any | Strong AI news bias confirmed by price |
| `range_reversion` | range, mixed | Poke outside a Bollinger band with stretched RSI, close back inside → fade to the middle |
| `momentum_breakout` | trend, volatile | Strong candle closes beyond the 20-candle high/low, with the trend |
| `london_orb` / `ny_orb` | any | Opening-range breakout: first close beyond the first hour of London (08:00–09:00 London) or of the New York open (09:30–10:30 NY), within 3 hours; stop at the middle of the range |
| `pdh_breakout` | any | First close of the day beyond the previous day's high/low with a strong candle (body ≥ 0.5 ATR) |
| `liquidity_sweep` (SMC, **off by default**) | any | First sweep of PDH/PDL, a session high/low or equal highs/lows, closing back inside with a rejection wick. Stop beyond the wick, target the next opposite liquidity |

### Strategy switching (`app/adaptive.py`)
Each trade is stored with its strategy and regime. For every (account, strategy, regime):
- **score = total R ÷ (trades + 10)**. The "+10" makes a handful of lucky trades count for little.
- after *min trades* (15; 12 for the scalper): score < 0 → **cautious** (half risk); score < −0.15 → **off** in that regime for 14 days.
- when "off" expires → **probation** at half risk. After 8 trades: avg R > 0 → **active** again; otherwise off for 28 days.
- When two strategies fire together, the one with the better score for the current regime is taken.
- It **can only lower risk or switch things off**; it never raises risk above the account's setting. You can reset any strategy from the 🧠 Learning tab.

### Weekly walk-forward tuning (`app/tuner.py`)
For each account, on the last 60 days (M15) or 30 days (M5) of history:
1. Run the signal engine once.
2. On the **older 70%**, try ~30 nearby combinations of default stop (±0.3 ATR), target (×0.75 / ×1 / ×1.25) and breakeven (off / 1R / 1.5R), and pick the best by total R.
3. On the **newest 30%**, which the search never saw, compare that winner with the current settings.
4. Apply it **only if** it's profitable on that unseen data and beats the current settings there by ≥ 1R. Otherwise keep the current settings and log why.
The result appears in the 🧠 Learning tab, and the change is saved as a settings version (`tuner:<mode>`) you can revert. Haiku's prompt includes these tested defaults. Run it any time with "Run tuner now", or `python -m app.tuner --mode aggressive`.

### Playbook loop
Daily, Haiku turns each account's trades and skipped signals into lessons. On Sunday **Sonnet** rewrites each account's playbook from those lessons and the scorecard (falling back to Haiku if Sonnet is over budget). Haiku and Sonnet read the playbook before every decision.

## 7a. "Human instinct" exit (`app/exits.py`)
A discretionary trader who shorted gold at 4070 for 4050 and sees it stall around 4060 takes the 10 points and looks for the next setup. The bot does the same, on every closed candle:
- the trade has covered at least **35%** of the way to its target;
- the last **3** candles made no new favourable extreme (no lower low for a short, no higher high for a long);
- those candles are tight, within **0.8 × ATR** (a consolidation, not a sharp move against the trade, which is the stop's job);
- then it closes at market with "momentum stalled at X% of target".

In the 2-year test across 637 strategy/symbol/mode combinations, it improved results slightly on average (+0.02R per trade). It helped most on the gold session breakout (+21.7R becomes +25.4R for Standard). The same function runs in the backtest and live. Each mode's settings: *Take profit when momentum stalls*, and the three thresholds.

## 7b. Smart Money Concepts (`app/smc.py`)
The bot codes SMC as objective rules, so it never relies on hand-drawn judgement:

| Concept | Rule |
|---|---|
| **PDH / PDL** | High/low of the previous UTC day that has data (Friday's on Monday) |
| **Session highs/lows** | Asian 00–07, London 07–12, New York 12–17 UTC; only once the session has finished |
| **Liquidity pools** | Unswept swing highs/lows from the last 3 days (fractal: higher/lower than 3 candles each side). Swings within 0.1 ATR of each other merge into **equal highs/lows**, where stops cluster |
| **Swept** | Price has traded through the level after it formed |
| **Fair value gap** | Candle 1's high is below candle 3's low (bullish), or the mirror; size ≥ 0.15 ATR; dropped once fully filled |
| **Order block** | The last opposite candle before a displacement candle (body ≥ 1 ATR) that breaks the last 10 candles' high/low; dropped once price closes through it |

**How the bot uses it:**
1. **Location filter (on by default):** skip any signal whose path is blocked by unswept liquidity or an opposite order block closer than the stop distance, e.g. buying 3 pips under an untouched PDH.
2. **Context for the AI:** every gate prompt includes the 8 nearest levels and 4 nearest zones, with their distance in ATR. Haiku and Sonnet are told to prefer trades that start after liquidity was taken and have room to the next pool.
3. **Chart:** levels are drawn as lines (gold = PDH/PDL and equal highs/lows, blue = sessions, faded ✓ = swept), and FVGs and OBs as shaded zones. You can toggle each on the chart.
4. **`liquidity_sweep` strategy:** coded and tested, but off by default (see below).

**First SMC backtest (18 Oct 2026; Standard and Moderate on 60 days of M15, Aggressive on 30 days of M5, XM spreads):**

| | Without SMC | With the location filter | Sweep strategy alone |
|---|---|---|---|
| Total over all tests | −69R | **−57R** (fewer, slightly better trades) | −39R over 73 trades (≈ −0.5R per trade) |
| Best case | | Aggressive BTC M5: +4.8R → **+8.5R** (+7.3%, 26 trades, 6% max drawdown) | |
| Worst case | | Standard EURUSD: −0.8R → −4.7R | Moderate BTC: −23R over 44 trades |

So the filter is on by default, and the sweep strategy is off until a longer test (2 years) shows an edge.

**Bigger picture:** over 60 days, most of the older strategies lose after XM costs.
- **Standard** is roughly flat: −2% to +1% per symbol.
- **Moderate** loses 9–20%.
- **Aggressive on M5** cannot trade EURUSD or GBPUSD at all, because the cost filter blocks every signal: a 2.3-pip spread is about half of a 5-pip M5 stop.
- **Aggressive gold** loses, and **Aggressive BTC** is the only clearly positive result.

This is a small sample, which is exactly why the bot is paper-trading.

## 7c. The 2-year study (18 Oct 2026)
Method:
1. Record every signal of 8 strategies over 2 years of M5 and M15 history for GOLD, EURUSD, GBPUSD and BTCUSD (~80,000 signals).
2. Replay them under each account's rules with XM Standard costs: 4 filter variants (none, SMC, H1 trend, both) and 3 exit styles (no stall exit, stall, fast stall).
3. Split the trades into year 1 and year 2. **A combination only counts if it made money in both years**, with at least 20 trades in each.
4. Re-test the finalists through the full live pipeline with $250 sizing and the 0.01-lot minimum.

| Result | |
|---|---|
| Combinations tested | ~2,200 |
| Passed both years | 29, almost all **GOLD session breakout M15** in some variant |
| Best | GOLD session breakout + H1 + SMC: +0.14 to +0.18R per trade, in every account style |
| Forex (EURUSD, GBPUSD) | every strategy loses after costs, and still loses with raw-spread costs |
| M5 | the M5 "edges" (gold breakout, BTC NY ORB) disappeared with real $250 sizing (−11% and −13%) |
| Busiest strategies | the biggest losers, e.g. BTC M5 trend pullback: 2.7 trades a day, −300R over 2 years |
| New strategies | London ORB, NY ORB and PDH breakout: none robust; BTC London ORB is about flat (on trial in Moderate) |
| Final accounts from $250 | Standard +29%, Moderate +45%, Aggressive +27% (2 years, ~1 trade a week each) |

Rerun it with `python -m app.backtest --mode <mode> --symbol all --days 730`. The study scripts were one-off; to repeat the full grid, ask for it to be added to `app/` as a tool.

## 8. First backtest result (honest)
Run on 17 Oct 2026 on BTC:

```
BTC_USDT · 120 days · 1% risk (before the XM/3-mode rebuild)
Trades: 244 | win rate 31.6% | avg R -0.273
Final equity $125.35 (-49.9%) | fees $93.38
Buy & hold (unleveraged): +30.3%
```

The raw `trend_pullback` signal on BTC 15m **has no edge**, and costs make it worse. With 1% risk and a 15m ATR stop, BTC positions are small, so fees plus slippage come to about 0.3R per round trip. The AI gate can only filter trades; it can't turn a losing signal into a winning one. Before trusting BTC paper results, see NEXT_STEPS.md → "Strategy work". Gold and forex haven't been backtested yet. Run `python -m app.backtest --symbol all --days 730` (the first run downloads the history).
