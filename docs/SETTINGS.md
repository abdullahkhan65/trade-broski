# Settings: modes and rules

Everything on `/settings` applies **within a few seconds**, with no restart. Every save is kept as a version, so you can revert from "Change history" at the bottom of the page.

## The three accounts (modes)

The bot runs **three separate $250 paper accounts at the same time**, on the same live prices. Each has its own rules, its own trades and its **own AI playbook**. Compare them on the dashboard's **⚖ Compare** tab.

| | Standard: "marathon" | Moderate: "professional swing/day" | Aggressive: "small-account scaler" |
|---|---|---|---|
| Idea | Wall Street-style: trusts the process, no greed | Steady growth over months | Hunts clean entries, quick profits, scales a small account without blowing it |
| Timeframe | M15, **only with the H1 trend** | M15 | **M5** + cost filter |
| Risk per trade | 0.75% | 1% | 2% |
| If 0.01 lot risks more than that | **skip** | allow up to 2% | allow up to 5% |
| **Stop for the day at profit** | **+1.5%** | **+2.5%** | **+5%** (risk halves after +3%) |
| **Stop for the day at loss** | −2% | −2.5% | −5% |
| Max new trades per day | 3 | 4 | 8 |
| After 2 losses in a row (today) | **done for the day** | half risk until a win | half risk until a win |
| Cost filter (skip if spread > % of stop) | off | off | 15% |
| Weekly loss stop | 5% | 7% | 12% |
| Kill switch (drawdown from peak) | 15% | 20% | 25% |
| Max effective leverage forex / gold / BTC | 10 / 5 / 3 | 20 / 10 / 5 | 30 / 20 / 10 |
| Max open positions (USD forex) | 2 (1) | 3 (2) | 4 (3) |
| Stop / target (× ATR) | 1.5 / 3.0 | 1.5 / 2.5 | 1.2 / 1.8 |
| Min reward:risk | 2.0 | 1.5 | 1.2 |
| Stop to breakeven at | off | +1.5R | **+1R** |
| News momentum signal | off | on | on |
| Min AI confidence | 0.60 | 0.55 | 0.50 |
| Trade without AI | no | rules only | rules only |
| Friday close (gold/forex) | close | halve | close |

**The daily % numbers are caps, not quotas.** When a mode reaches its daily profit or loss level it stops opening trades until the next UTC day; open trades keep their stops. A quiet day at 0% is fine. Forcing trades to hit a number is how accounts get blown. For scale: 1% a day for a year is about 12× the account, and the best funds make 10–30% a *year*.

Risk is a % of current equity, so lots grow as the account grows (compounding) and shrink after losses.

**"Reset to default"** under each column restores that mode's defaults. **Account running** turns a mode off without touching its history. On the dashboard's **Account** tab you can **reset an account to $250** to start a mode's test over.

### The 0.01-lot problem on a $250 XM Standard account
XM's minimum is **0.01 lot**: 1,000 units of EURUSD, **1 oz of gold**, or 0.01 BTC. With normal stops:

| Symbol | Typical M15 stop | Loss at 0.01 lot | % of $250 |
|---|---|---|---|
| EURUSD | ~12 pips | ~$1.20 | ~0.5% ✅ |
| GBPUSD | ~16 pips | ~$1.60 | ~0.6% ✅ |
| GOLD | ~$12 | **~$12** | **~4.8%** |
| BTCUSD | ~$900 | **~$9** | **~3.6%** |

So **Standard and Moderate will mostly skip gold and BTC**. The Journal tab shows "0.01 lot would risk 4.8%", and Market Watch shows the current 0.01-lot risk for each symbol. Aggressive takes them, at up to 5% risk per trade. That's the truth of a $250 Standard account, not a bug. To trade gold on the safer modes you need a bigger balance (about $1,200+ for 1% risk) or an **XM Micro account**, where lots are 100× smaller.

## Every per-mode setting
- **Timeframe**: M5 or M15 candles for that account's signals. M5 means more trades, more noise and more spread cost per trade.
- **Risk per trade**: the loss in % of equity if the stop is hit. Lots = risk $ ÷ (stop distance × contract size), rounded **down** to 0.01.
- **0.01-lot rule**: `skip`, or `allow` the minimum lot if its risk is ≤ the "allow up to" %.
- **Max effective leverage**: notional ÷ equity per position. XM allows 1:1000, but this is your own cap; lots are reduced to fit it.
- **Max margin used**: total margin (notional ÷ XM leverage) as % of equity.
- **Max open positions / USD forex positions**: EURUSD, GBPUSD and USDJPY are all dollar trades, so this stops you stacking the same bet.
- **Daily / weekly loss limit**: no new trades until the next UTC day, or until Monday. Open positions keep their stops.
- **Kill switch**: closes everything and halts the account until you press **Resume**.
- **Daily profit target**: once equity is up this much today, no new trades until tomorrow (status `target_hit`). A cap, not a quota.
- **Halve risk once the day is up X%**: protects a good day (Aggressive: after +3%).
- **Max new trades per day**: counts every trade opened since 00:00 UTC.
- **Losses in a row → then**: `stop_day` (done until tomorrow) or `half_risk` (half risk until a win). Breakeven exits don't count.
- **Cost filter**: skip a trade when the spread is more than X% of the stop distance. On M5 with XM Standard spreads this blocks almost every EURUSD/GBPUSD signal: a 2.3-pip spread against a ~5-pip stop means the trade starts nearly half a stop down.
- **Stops & targets**: default stop and target in × ATR. The AI's suggestion is clamped between the tightest and widest stop, and the target is always ≥ min reward:risk.
- **Move stop to breakeven at +R**: at +X R the stop moves to the entry price (0 = off).
- **Signals**: switch trend pullback, session breakout and news momentum on or off, plus the London/NY session filter.
- **SMC location filter** (all modes, on): skip a signal when unswept liquidity (PDH/PDL, session highs/lows, equal highs/lows) or an opposite order block sits closer than the stop distance. See STRATEGY.md §7b.
- **Liquidity sweep signal** (off): the SMC sweep-and-reclaim strategy, with a stop beyond the wick and a target at the next liquidity. It lost in the first backtest, so it's off until it proves itself.
- **Only trade with the H1 trend**: hourly candles are built from the M5/M15 ones. Longs only when the H1 close is above EMA50 and EMA20 is above EMA50; shorts the opposite; a flat H1 means no trade.
- **AI**: minimum confidence for Haiku's TAKE, and what to do when AI is off or over budget.
- **Range reversion / Momentum breakout**: the two regime-specific strategies (see STRATEGY.md §7).
- **Self-learning**: *Strategy switching by regime* on/off; *Trades needed before judging a strategy* (fewer means it adapts faster but reacts to luck); *Weekly self-tuning* on/off. Tuning only ever changes the default stop, target and breakeven, within this mode's own limits.
- **Weekend**: close, halve or hold gold/forex before Friday's close, and stop opening new trades N minutes before it.

## Global settings (all accounts)
- **Trading mode**: `auto` opens trades. `signals_only` logs and alerts what each account *would* do.
- **Instruments**: GOLD, EURUSD, GBPUSD and BTCUSD are on; USDJPY is off by default (its price source, Yahoo, is the least reliable).
- **Indicators**: EMA/RSI/ATR periods, the spike filter, the dead-market filter, and auto-disabling losing signals after N trades.
- **AI**: on/off, max calls per day (default 120), **total** monthly budget for Haiku + Sonnet (default $6, **hard max $8**), daily review, weekly playbook.
- **Sonnet**: on/off, its own monthly budget inside the total (default $3, hard max $5), max Sonnet calls per day (default 10), second opinion on/off and its risk threshold (default 1.5%). See ARCHITECTURE.md → "Two AI models".
- **News & events**: calendar blackout, AI news digest, headline feeds.
- **Broker: XM Standard**: copy these from MT5 (**Market Watch → right-click symbol → Specification**):
  - leverage for forex, gold and BTC (margin = notional ÷ leverage), margin call 50%, **stop-out 20%**;
  - **spread in points** (GOLD 50 = $0.50, EURUSD 23 = 2.3 pips, BTCUSD 4000 = $40, as observed on XM Standard; GBPUSD 28 and USDJPY 18 are estimates). If the live feed's spread is wider, the wider one is used;
  - **swap long/short in points per lot per night**. The defaults are estimates, so replace them with your MT5 values. FX/gold are charged ×3 on Wednesday; BTCUSD every night including weekends.

## Hard limits (can't be exceeded from the UI or the API)

| Setting | Hard max |
|---|---|
| Risk per trade | 5% |
| 0.01-lot "allow up to" | 8% |
| Effective leverage forex / gold / BTC | 50 / 30 / 10 |
| Max margin used | 80% |
| Kill switch | 50% |
| AI monthly budget (total) | $8 |
| Sonnet monthly budget | $5 |
| AI calls per day | 300 |

## Playbooks
Each mode has its own playbook: a short list of rules that Haiku reads before **every** trade for that account. It's rewritten every Sunday from that account's own lessons. You can edit or roll it back on the dashboard's **Playbook** tab, which shows the playbook of the account you're viewing.
