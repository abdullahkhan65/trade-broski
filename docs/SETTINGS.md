# Settings: modes and rules

Everything on `/settings` applies **within a few seconds**, with no restart. Every save is kept as a version, so you can revert from "Change history" at the bottom of the page.

## The three accounts (modes)

The bot runs **three separate $250 paper accounts at the same time**, on the same live prices. Each has its own rules, its own trades and its **own AI playbook**. Compare them on the dashboard's **⚖ Compare** tab.

| | Standard | Moderate | Aggressive scalper |
|---|---|---|---|
| Idea | Principled: A-grade setups only, grows slowly | A bit more risk and room | M5 scalper aiming for 1–3% a day |
| Timeframe | M15 | M15 | **M5** |
| Risk per trade | 1% | 1.5% | 2% |
| If 0.01 lot risks more than that | **skip** | allow up to 3% | allow up to 5% |
| Max effective leverage forex / gold / BTC | 10 / 5 / 3 | 20 / 10 / 5 | 30 / 20 / 10 |
| Max open positions (USD forex) | 2 (1) | 3 (2) | 4 (3) |
| Daily / weekly loss stop | 3% / 6% | 4% / 8% | 5% / 12% |
| Kill switch (drawdown from peak) | 20% | 25% | 35% |
| **Daily profit target** (stop for the day) | off | off | **+3%** |
| Stop / target (× ATR) | 1.5 / 3.0 | 1.5 / 2.5 | 1.2 / 1.8 |
| Min reward:risk | 2.0 | 1.5 | 1.2 |
| Stop to breakeven at | off | +1.5R | **+1R** |
| News momentum signal | off | on | on |
| Min AI confidence | 0.60 | 0.55 | 0.50 |
| Trade without AI | no | rules only | rules only |
| Friday close (gold/forex) | close | halve | close |

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
- **Daily profit target**: once equity is up this much today, no new trades until tomorrow (status `target_hit`).
- **Stops & targets**: default stop and target in × ATR. The AI's suggestion is clamped between the tightest and widest stop, and the target is always ≥ min reward:risk.
- **Move stop to breakeven at +R**: at +X R the stop moves to the entry price (0 = off).
- **Signals**: switch trend pullback, session breakout and news momentum on or off, plus the London/NY session filter.
- **AI**: minimum confidence for Haiku's TAKE, and what to do when AI is off or over budget.
- **Weekend**: close, halve or hold gold/forex before Friday's close, and stop opening new trades N minutes before it.

## Global settings (all accounts)
- **Trading mode**: `auto` opens trades. `signals_only` logs and alerts what each account *would* do.
- **Instruments**: GOLD, EURUSD, GBPUSD and BTCUSD are on; USDJPY is off by default (its price source, Yahoo, is the least reliable).
- **Indicators**: EMA/RSI/ATR periods, the spike filter, the dead-market filter, and auto-disabling losing signals after N trades.
- **AI**: on/off, max calls per day (default 120), monthly budget (default $2, **hard max $5**), daily review, weekly playbook.
- **News & events**: calendar blackout, AI news digest, headline feeds.
- **Broker: XM Standard**: copy these from MT5 (**Market Watch → right-click symbol → Specification**):
  - leverage for forex, gold and BTC (margin = notional ÷ leverage), margin call 50%, **stop-out 20%**;
  - **spread in points** (GOLD 35, EURUSD 16, GBPUSD 21, USDJPY 18, BTCUSD 4000 by default). If the live feed's spread is wider, the wider one is used;
  - **swap long/short in points per lot per night**. The defaults are estimates, so replace them with your MT5 values. FX/gold are charged ×3 on Wednesday; BTCUSD every night including weekends.

## Hard limits (can't be exceeded from the UI or the API)

| Setting | Hard max |
|---|---|
| Risk per trade | 5% |
| 0.01-lot "allow up to" | 8% |
| Effective leverage forex / gold / BTC | 50 / 30 / 10 |
| Max margin used | 80% |
| Kill switch | 50% |
| AI monthly budget | $5 |
| AI calls per day | 300 |

## Playbooks
Each mode has its own playbook: a short list of rules that Haiku reads before **every** trade for that account. It's rewritten every Sunday from that account's own lessons. You can edit or roll it back on the dashboard's **Playbook** tab, which shows the playbook of the account you're viewing.
