# Settings: changing the rules

Everything on the `/settings` page applies **within a few seconds**, with no restart. Every save is stored as a new version, and the "Change history" table at the bottom lets you revert to any of them.

## Profiles: how aggressive should it be?

Pick a profile first, then fine-tune any single value. Once you change something the profile shows as `custom`. Switching profile **keeps** your instrument on/off choices, news feeds and swap rates.

| Setting | 🛡 Conservative | ⚖️ Balanced (spec default) | 🔥 Aggressive |
|---|---|---|---|
| Risk per trade | 0.5% | 1% | 2% |
| Max leverage forex / gold / BTC | 5 / 3 / 2 | 10 / 5 / 3 | 20 / 10 / 5 |
| Max margin used | 35% | 50% | 70% |
| Max open positions | 2 | 3 | 4 |
| Max USD forex positions at once | 1 | 2 | 3 |
| If the size needs more leverage than the cap | skip | skip | **shrink** to fit |
| Daily / weekly loss limit | 2% / 4% | 3% / 6% | 5% / 10% |
| Kill switch (drawdown from peak) | 15% | 20% | 30% |
| Min reward:risk | 2.0 | 1.5 | 1.3 |
| Min AI confidence | 0.65 | 0.55 | 0.50 |
| Trade when AI is unavailable | no | rules only | rules only |
| News blackout | 30 min, high + medium impact | 15 min, high | 10 min, high |
| News momentum signal | off | off | **on** |
| Gold/forex over the weekend | close | close | halve |

**What "aggressive" really means.** More risk per trade makes the size bigger; more leverage room means fewer trades get skipped for "needs too much leverage"; looser filters mean more trades. All three increase drawdowns. With 2% risk, 10 losses in a row costs about 18% of the account, and losing streaks of 8–10 are normal for a strategy with a 35–45% win rate. Move up one step at a time, and only after the scorecard shows a positive average R on 30+ trades.

## Hard limits (can't be exceeded from the UI or the API)

These are the `max` values in `app/settings_store.py`. Anything higher is clamped silently on save.

| Setting | Hard max |
|---|---|
| Risk per trade | 3% |
| Leverage forex / gold / BTC | 30 / 20 / 10 |
| Max margin used | 80% |
| Kill switch | 40% |
| Daily loss limit | 10% |
| AI monthly budget | **$5** (keeps the total bill under $8 with the server) |
| AI calls per day | 150 |

To change a hard limit you must edit the code. That is deliberate.

## Every setting

### General
- **Trading mode**: `auto` opens paper trades. `signals_only` runs everything, including the AI, and alerts you with "👀 Would open…" but opens nothing. Good for the first days, or after you change strategy settings.

### Instruments
Turn each of the 5 instruments on or off. Prices keep streaming for all of them; this only controls new trades. **USD/JPY is off by default**: it wasn't in your list, and its free price source (Yahoo, polled) is the least reliable.

### Risk
- **Risk per trade (%)**: the loss if the stop is hit. Position size = (equity × risk%) ÷ stop distance.
- **Max leverage (forex, gold, BTC)**: caps each position's *effective* leverage (notional ÷ equity). If a trade needs more, it's skipped or shrunk (see below).
- **Max total margin used (%)**: margin is charged like a real broker: notional ÷ 30 for forex, ÷ 20 for gold and BTC. New trades that would push total margin past this are skipped.
- **Max open positions**: across all instruments. There is never more than one per instrument.
- **Max USD forex positions**: EUR/USD, GBP/USD and USD/JPY are all bets on the dollar. This stops you holding three versions of the same trade.
- **If size needs more leverage than the cap**: `skip` (spec default) or `shrink` (take a smaller position that fits; you risk less than the full %).
- **Daily / weekly loss limit**: pauses new trades. Open positions keep their stops. The pause lifts automatically at the next UTC day, or on Monday for the weekly limit.
- **Kill switch**: closes everything and halts. Only **Resume** on the dashboard (or `/resume` in Telegram) restarts it, and resuming resets the peak to current equity.
- **Stop-out margin level**: if equity ÷ margin used falls below this, the biggest losing position is closed, like a broker margin call.

### Stops & targets
- **Default stop / target (× ATR)**: used when the AI gives no values, or when the bot runs on rules only.
- **Tightest / widest stop**: the AI's suggested stop is clamped into this range.
- **Min reward:risk**: the target distance is always at least this multiple of the stop distance.

### Signals
- **Trend pullback / Session breakout / News momentum**: turn each signal type on or off. See STRATEGY.md for what each one does.
- **Only trade gold/forex in London + NY hours**: the session filter. BTC is never filtered by session.
- **EMA, RSI and ATR periods**: indicator settings. Change them only after backtesting (`python -m app.backtest`).
- **News-spike filter**: skips signals when the last candle is larger than X × ATR.
- **Dead-market filter**: skips signals when volatility is in the bottom N% of the last 20 days.
- **Auto-disable after N trades**: a signal type with a negative average R after N trades on an instrument is switched off. Re-enable it from the dashboard scorecard.

### AI
- **Use Claude Haiku to approve trades**: off means every signal goes straight to the risk engine (if "When AI is off" = rules_only).
- **Min AI confidence**: Haiku's TAKE is ignored if its confidence is below this.
- **Max AI calls per day / monthly budget**: hard spending guards. When either is hit, the bot follows the next setting and sends you a warning.
- **When AI is off or over budget**: `rules_only` trades on coded signals; `skip` doesn't trade. API errors and invalid replies **always** skip.
- **Daily review / weekly playbook**: the learning-loop jobs (about $0.15/month together).

### News & events
- **Use news and economic calendar**: the master switch for everything below.
- **Blackout**: no new gold/forex trades this many minutes before and after a high-impact release for that instrument's currencies (USD affects all five). Optionally includes medium-impact events.
- **AI news digest**: one Haiku call that turns the latest headlines and calendar into a bias (−1..+1) and a risk level for each instrument. It runs every N minutes, and only when new headlines have arrived.
- **Strong bias threshold**: how strong the bias must be for the news momentum signal.
- **Block trades when news risk is extreme**: for example during a surprise central-bank move or a geopolitical shock.
- **Headline feeds**: one RSS/Atom URL per line. Add or remove sources freely.

### Weekend
- **Friday close**: what happens to gold/forex positions before the market closes on Friday at 5pm New York: `close` (safest), `halve`, or `hold`. Monday gaps can jump straight past your stop.
- **No new gold/forex trades in the last N minutes of the week.**

### Costs (simulation)
- **BTC taker fee and slippage**: the defaults match Binance's standard taker fee (0.05%).
- **Min spread (gold, EUR, GBP, JPY)**: exchange feeds quote tighter spreads than a retail broker. Quotes are widened to at least this, so fills aren't flattered.
- **Swap rates (% per year, long and short)**: negative means you pay. The defaults are estimates; **copy the current numbers from the swap table of the broker you'd use live** roughly once a month.

### Engine
- **Check stops every N seconds**: the default of 5 is plenty. Lower values use slightly more CPU.

## Playbook

The playbook is the short list of rules Haiku reads before every decision. It is rewritten by AI every Sunday from the week's lessons. You can **edit it yourself** or **roll back** to any version from the dashboard.
