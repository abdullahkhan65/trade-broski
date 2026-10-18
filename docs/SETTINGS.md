# Settings: modes and rules

Everything on `/settings` applies **within a few seconds**, with no restart. Every save is kept as a version, so you can revert from "Change history" at the bottom of the page.

## The accounts (modes)

The bot runs **four separate $250 paper accounts at the same time** on the same live prices: three real strategies and a Lab. Each has its own rules, trades and **AI playbook**. Compare them on the dashboard's **Compare** tab.

The rules come from a **2-year backtest** (Oct 2024 – Oct 2026, XM Standard costs, $250 sizing). Of about 2,200 strategy × symbol × filter × exit combinations, the one that made money in **both** years under every account's rules was **GOLD's Asian-range breakout on M15, traded only with the H1 trend and with the SMC filter**. Everything else, including every forex strategy and everything on M5, lost money after costs. So the three real accounts trade that edge, at different risk levels:

| | Standard | Moderate | Aggressive |
|---|---|---|---|
| Style | Patient professional | Swing/day trader | Small-account scaler |
| Timeframe | M15 | M15 | M15 |
| Allowlist (proven) | GOLD session breakout · US100 London ORB · US30 London ORB · US30 NY ORB · US30 OB/FVG retest (index setups use the `liq+trail1` exit profile) | same | same |
| Exploration (trials) | 1%/week, 1 a day, ≤0.6% risk | 2%/week, 2 a day, ≤0.75% risk | 3%/week, 3 a day, ≤1% risk |
| Risk per trade | 1% | 1.5% | 2.5% |
| 0.01 lot allowed up to | 5% | 6% | 6% |
| Stop / target (× ATR) | 1.5 / 3.0 | 1.5 / 2.5 | 1.5 / 2.5 |
| Breakeven | off | +1.5R | +1R |
| Momentum-stall exit | on (35% of target, 3 candles, 0.8 ATR) | on | on |
| Done for the day at | +4% / −5% | +5% / −6% | +6% / −7% (half risk after +4%) |
| Max trades per day | 2 | 3 | 6 |
| After losses in a row | 2 → done for the day | 2 → half risk | 3 → half risk |
| Weekly loss stop / kill switch | 10% / 25% | 12% / 30% | 15% / 35% |
| Min AI confidence (Haiku, plus Sonnet on big trades) | 0.55 | 0.50 | 0.50 |
| **2-year backtest from $250, per market** | gold +29% · US30 +87% · US100 +22% | gold +45% · US30 +67% · US100 +27% | gold +27% · US30 +58% · US100 +23% |

**Lab:** takes **every** signal from every strategy on every symbol, with no AI, no filters and no daily caps. It runs at 1% risk, allows the 0.01 lot up to 8%, and has a 50% kill switch. It's there so there's always activity and live data on every strategy. Expect it to lose money: that's what the backtest says these strategies do. Don't copy it.

**Expect about 3 proven trades a week per real account** (gold ~1, US30 ~1.7, US100 ~0.4), plus small trial trades from the exploration budget most days. Year 2 of the backtest was weaker than year 1 for gold and US30, so the audit's drift check watches them.

**Exit profiles:** an allowlist entry can carry an exit profile after `@`, e.g. `US30:zone_retest@liq+trail1` = take-profit at the next liquidity level + 1R trailing stop. Profiles: `base`, `liq`, `trail1`, `trail1.5`, `partial1+trail1`, `liq+trail1`.

**The daily % numbers are caps, not quotas.** When an account hits its daily profit or loss level it stops opening trades until the next UTC day.

### The 0.01-lot problem on a $250 XM Standard account
XM's minimum is **0.01 lot**: 1,000 units of EURUSD, **1 oz of gold**, or 0.01 BTC. With normal stops:

| Symbol | Typical M15 stop | Loss at 0.01 lot | % of $250 |
|---|---|---|---|
| EURUSD | ~12 pips | ~$1.20 | ~0.5% ✅ |
| GBPUSD | ~16 pips | ~$1.60 | ~0.6% ✅ |
| GOLD | ~$12 | **~$12** | **~4.8%** |
| BTCUSD | ~$900 | **~$9** | **~3.6%** |

Since the only proven edge is on gold, **all three real accounts now accept gold's 0.01 lot** (up to 5–6% risk per trade). The Journal tab shows "0.01 lot would risk 4.8%", and Market Watch shows the current 0.01-lot risk for each symbol. Aggressive takes them, at up to 5% risk per trade. That's the truth of a $250 Standard account, not a bug. To trade gold on the safer modes you need a bigger balance (about $1,200+ for 1% risk) or an **XM Micro account**, where lots are 100× smaller.

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
- **Allowlist (symbol:strategy, one per line)**: when it isn't empty, an account only trades these pairs, e.g. `XAU_USD:session_breakout`, `BTC_USDT:*` or `*:ny_orb`. The defaults come from the 2-year backtest: only pairs that made money in both years. Empty means every enabled strategy on every symbol.
- **London / New York opening-range breakout, previous-day high/low breakout**: the newer strategies (STRATEGY.md).
- **Partial take-profit at +R / share / trailing stop N R behind**: bank part of a winner and let the rest run with a trailing stop. A partial close needs at least 2× the minimum lot, so at $250 (0.01 lot of gold) only the trailing stop applies; partials start working as the account grows.
- **Order block / FVG retest**: the SMC entry: the first return into a fresh order block or fair value gap that closes back out of it, with the stop beyond the zone.
- **Target the next liquidity level**: the take-profit goes at the nearest untouched opposite high or low (PDH/PDL, session, equal highs/lows) at least min reward:risk away, instead of a fixed ATR multiple.
- **Session breakout: min/max Asian range**: skip a breakout when the Asian range was too tight (noise) or too wide (the move already happened).
- **Take profit when momentum stalls** + 3 thresholds: the "human instinct" exit (STRATEGY.md §7a).
- **Exploration budget** (per account): trial trades on setups that look promising but aren't proven yet (the Lab's best and the 2-year near-misses), so every account trades and learns most days. Trials always use the **minimum lot** and are skipped if that risks more than *max risk* (0.6% / 0.75% / 1%; gold at $250 is too big, forex/BTC fit). Once trials have lost *weekly budget* % of equity since Monday (1% / 2% / 3%), exploration pauses until next week. Trials don't use the account's daily trade count or losing-streak rules, never get a Sonnet second opinion, and show "[trial]" in History. Winning trials become allowlist candidates.
- **Ask Claude before trades** (`ai.use_ai`): off means rules only, with no AI cost and no daily review or playbook (used by the Lab account).
- **SMC location filter** (all modes, on): skip a signal when unswept liquidity (PDH/PDL, session highs/lows, equal highs/lows) or an opposite order block sits closer than the stop distance. See STRATEGY.md §7b.
- **Liquidity sweep signal** (off): the SMC sweep-and-reclaim strategy, with a stop beyond the wick and a target at the next liquidity. It lost in the first backtest, so it's off until it proves itself.
- **Only trade with the H1 trend**: hourly candles are built from the M5/M15 ones. Longs only when the H1 close is above EMA50 and EMA20 is above EMA50; shorts the opposite; a flat H1 means no trade.
- **AI**: minimum confidence for Haiku's TAKE, and what to do when AI is off or over budget.
- **Range reversion / Momentum breakout**: the two regime-specific strategies (see STRATEGY.md §7).
- **Self-learning**: *Also learn from the Lab's trades* (pooled, half weight); *Strategy switching by regime* on/off; *Trades needed before judging a strategy* (fewer means it adapts faster but reacts to luck); *Weekly self-tuning* on/off. Tuning only ever changes the default stop, target and breakeven, within this mode's own limits.
- **Weekend**: close, halve or hold gold/forex before Friday's close, and stop opening new trades N minutes before it.

## Global settings (all accounts)
- **Trading mode**: `auto` opens trades. `signals_only` logs and alerts what each account *would* do.
- **Instruments**: GOLD, EURUSD, GBPUSD and BTCUSD are on; USDJPY is off by default (its price source, Yahoo, is the least reliable).
- **Indicators**: EMA/RSI/ATR periods, the spike filter and the dead-market filter.
- **Audit**: the daily floor-manager report and the post-mortem of every losing trade.
- **Research (weekly)**: the Lab → allowlist pipeline on/off, how many Lab trades and what average R a pair needs before it's re-tested, and the weekly Sonnet research review.
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
