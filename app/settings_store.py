"""Rules you can change from the settings page.

Two kinds of settings:
* GLOBAL: instruments, indicator periods, AI budget, news, XM broker specs.
* PER MODE: each of the 3 paper accounts (standard / moderate / aggressive) has
  its own risk, stops, signals, AI confidence and weekend rules.

FIELDS / MODE_FIELDS describe every editable key (type, limits, help text). The
settings page is generated from them and every save is validated against them.
The `max` on each field is a HARD LIMIT that the UI/API can never exceed.

Every save is stored in settings_versions so you can roll back from the UI.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from .db import DB, iso

MODES = ["standard", "moderate", "aggressive"]
DEFAULTS_VERSION = 2          # bump + add a step in SettingsStore._upgrade_defaults when defaults change
MODE_LABELS = {
    "standard": "Standard",
    "moderate": "Moderate",
    "aggressive": "Aggressive scalper",
}
MODE_BLURBS = {
    "standard": "Wall Street-style marathon trader: trusts the process, no greed. Only A-grade setups in the "
                "direction of the H1 trend, 0.75% risk, max 3 trades a day, done for the day at +1.5% or -2% "
                "or after 2 losses in a row. Steady growth of 0.5-1.5% on good days, 0% on quiet ones.",
    "moderate": "Professional swing/day trader, happy with steady growth over months: 1% risk, max 4 trades "
                "a day, done for the day at +2.5% or -2.5%, half risk after 2 losses in a row.",
    "aggressive": "Professional small-account scaler on M5: hunts clean entries, takes quick profits and gets "
                  "out. 2% risk, max 8 trades a day, risk halves after +3% and it stops at +5% or -5%. "
                  "Skips trades where the spread eats too much of the stop. Takes risk, never blows the account.",
}
SYMBOLS = ["XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY", "BTC_USDT"]

DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://www.investing.com/rss/news_11.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.fxstreet.com/rss/news",
]

MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "standard": {
        "enabled": True,
        "timeframe": "15m",
        "risk": {
            "risk_per_trade_pct": 0.75,
            "min_lot_rule": "skip", "min_lot_max_risk_pct": 1.0,
            "max_leverage_forex": 10.0, "max_leverage_metal": 5.0, "max_leverage_crypto": 3.0,
            "max_margin_used_pct": 50.0,
            "max_open_positions": 2, "max_usd_forex_positions": 1,
            "daily_loss_limit_pct": 2.0, "weekly_loss_limit_pct": 5.0,
            "kill_switch_drawdown_pct": 15.0, "daily_profit_target_pct": 1.5,
            "profit_half_risk_at_pct": 0.0, "max_trades_per_day": 3,
            "loss_streak": 2, "loss_streak_action": "stop_day", "max_spread_stop_pct": 0.0,
        },
        "exits": {"default_stop_atr": 1.5, "default_target_atr": 3.0, "min_stop_atr": 1.0,
                  "max_stop_atr": 3.0, "min_reward_risk": 2.0, "breakeven_at_r": 0.0},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": False,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": True, "liquidity_sweep": False, "smc_filter": True},
        "adaptive": {"enabled": True, "min_trades": 15, "auto_tune": True},
        "ai": {"min_confidence": 0.6, "when_unavailable": "skip"},
        "weekend": {"mode": "close", "minutes_before_close": 30, "no_new_trades_minutes": 120},
    },
    "moderate": {
        "enabled": True,
        "timeframe": "15m",
        "risk": {
            "risk_per_trade_pct": 1.0,
            "min_lot_rule": "allow", "min_lot_max_risk_pct": 2.0,
            "max_leverage_forex": 20.0, "max_leverage_metal": 10.0, "max_leverage_crypto": 5.0,
            "max_margin_used_pct": 60.0,
            "max_open_positions": 3, "max_usd_forex_positions": 2,
            "daily_loss_limit_pct": 2.5, "weekly_loss_limit_pct": 7.0,
            "kill_switch_drawdown_pct": 20.0, "daily_profit_target_pct": 2.5,
            "profit_half_risk_at_pct": 0.0, "max_trades_per_day": 4,
            "loss_streak": 2, "loss_streak_action": "half_risk", "max_spread_stop_pct": 0.0,
        },
        "exits": {"default_stop_atr": 1.5, "default_target_atr": 2.5, "min_stop_atr": 1.0,
                  "max_stop_atr": 3.0, "min_reward_risk": 1.5, "breakeven_at_r": 1.5},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": True,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": False, "liquidity_sweep": False, "smc_filter": True},
        "adaptive": {"enabled": True, "min_trades": 15, "auto_tune": True},
        "ai": {"min_confidence": 0.55, "when_unavailable": "rules_only"},
        "weekend": {"mode": "halve", "minutes_before_close": 30, "no_new_trades_minutes": 90},
    },
    "aggressive": {
        "enabled": True,
        "timeframe": "5m",
        "risk": {
            "risk_per_trade_pct": 2.0,
            "min_lot_rule": "allow", "min_lot_max_risk_pct": 5.0,
            "max_leverage_forex": 30.0, "max_leverage_metal": 20.0, "max_leverage_crypto": 10.0,
            "max_margin_used_pct": 70.0,
            "max_open_positions": 4, "max_usd_forex_positions": 3,
            "daily_loss_limit_pct": 5.0, "weekly_loss_limit_pct": 12.0,
            "kill_switch_drawdown_pct": 25.0, "daily_profit_target_pct": 5.0,
            "profit_half_risk_at_pct": 3.0, "max_trades_per_day": 8,
            "loss_streak": 2, "loss_streak_action": "half_risk", "max_spread_stop_pct": 15.0,
        },
        "exits": {"default_stop_atr": 1.2, "default_target_atr": 1.8, "min_stop_atr": 0.8,
                  "max_stop_atr": 2.0, "min_reward_risk": 1.2, "breakeven_at_r": 1.0},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": True,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": False, "liquidity_sweep": False, "smc_filter": True},
        "adaptive": {"enabled": True, "min_trades": 12, "auto_tune": True},
        "ai": {"min_confidence": 0.5, "when_unavailable": "rules_only"},
        "weekend": {"mode": "close", "minutes_before_close": 45, "no_new_trades_minutes": 60},
    },
}

DEFAULTS: dict[str, Any] = {
    "trading_mode": "auto",
    "instruments": {"XAU_USD": True, "EUR_USD": True, "GBP_USD": True, "USD_JPY": False, "BTC_USDT": True},
    "indicators": {
        "ema_fast": 20, "ema_slow": 50, "rsi_period": 14, "atr_period": 14,
        "spike_atr_mult": 3.0, "dead_market_percentile": 20.0, "auto_disable_after_trades": 30,
    },
    "ai": {"enabled": True, "max_calls_per_day": 120, "monthly_budget_usd": 6.0,
           "daily_review": True, "weekly_playbook": True,
           "strong_enabled": True, "strong_budget_usd": 3.0, "strong_calls_per_day": 10,
           "second_opinion": True, "second_opinion_risk_pct": 1.5},
    "news": {
        "enabled": True, "blackout_minutes": 15, "blackout_medium_impact": False,
        "digest_enabled": True, "digest_interval_min": 60, "bias_threshold": 0.6,
        "block_on_extreme_risk": True, "headline_poll_min": 10, "feeds": DEFAULT_FEEDS,
    },
    # XM Standard account (MT5). Spreads and swaps in POINTS, as MT5 shows them.
    "broker": {
        "leverage_forex": 1000.0, "leverage_metal": 1000.0, "leverage_crypto": 50.0,
        "margin_call_pct": 50.0, "stop_out_pct": 20.0,
        "spread_XAU_USD": 50, "spread_EUR_USD": 23, "spread_GBP_USD": 28,
        "spread_USD_JPY": 18, "spread_BTC_USDT": 4000,
        "swap_XAU_USD_long": -50.0, "swap_XAU_USD_short": 25.0,
        "swap_EUR_USD_long": -8.0, "swap_EUR_USD_short": 2.0,
        "swap_GBP_USD_long": -4.0, "swap_GBP_USD_short": -3.0,
        "swap_USD_JPY_long": 12.0, "swap_USD_JPY_short": -30.0,
        "swap_BTC_USDT_long": -4000.0, "swap_BTC_USDT_short": -2000.0,
    },
    "engine": {"stop_check_seconds": 3},
    "modes": copy.deepcopy(MODE_DEFAULTS),
}


@dataclass
class Field:
    key: str
    label: str
    group: str
    type: str                     # float | int | bool | choice | list
    min: float | None = None
    max: float | None = None      # hard limit - can never be exceeded
    step: float | None = None
    choices: list[str] = field(default_factory=list)
    help: str = ""


_SYM_LABEL = {"XAU_USD": "GOLD", "EUR_USD": "EURUSD", "GBP_USD": "GBPUSD", "USD_JPY": "USDJPY", "BTC_USDT": "BTCUSD"}

FIELDS: list[Field] = [
    Field("trading_mode", "Trading mode (all accounts)", "General", "choice", choices=["auto", "signals_only"],
          help="auto: opens paper trades. signals_only: logs what it WOULD do, opens nothing."),
    *[Field(f"instruments.{s}", lbl, "Instruments", "bool",
            help="Trade this symbol in all 3 accounts. Prices keep streaming even when off.")
      for s, lbl in [("XAU_USD", "GOLD - Binance gold perp prices"), ("EUR_USD", "EURUSD - Kraken prices"),
                     ("GBP_USD", "GBPUSD - Kraken prices"),
                     ("USD_JPY", "USDJPY - Yahoo prices, polled every 15s (less reliable)"),
                     ("BTC_USDT", "BTCUSD - Binance prices")]],
    Field("indicators.ema_fast", "Fast EMA", "Indicators (all modes)", "int", 5, 100, 1),
    Field("indicators.ema_slow", "Slow EMA", "Indicators (all modes)", "int", 10, 300, 1),
    Field("indicators.rsi_period", "RSI period", "Indicators (all modes)", "int", 2, 50, 1),
    Field("indicators.atr_period", "ATR period", "Indicators (all modes)", "int", 2, 50, 1),
    Field("indicators.spike_atr_mult", "News-spike filter (candle > x ATR)", "Indicators (all modes)", "float", 1.5, 10, 0.5),
    Field("indicators.dead_market_percentile", "Dead-market filter (ATR percentile)", "Indicators (all modes)",
          "float", 0, 50, 5, help="Skip when ATR is below this percentile of the last ~20 days."),
    Field("indicators.auto_disable_after_trades", "Auto-disable losing signals after N trades", "Indicators (all modes)",
          "int", 10, 500, 5, help="Per account: a signal with negative average R after N trades is switched off."),
    Field("ai.enabled", "Use Claude Haiku to approve trades", "AI (all modes)", "bool"),
    Field("ai.max_calls_per_day", "Max AI calls per day (all purposes)", "AI (all modes)", "int", 0, 300, 5),
    Field("ai.monthly_budget_usd", "AI monthly budget, Haiku + Sonnet ($)", "AI (all modes)", "float", 0, 8, 0.25,
          help="Hard stop for ALL AI calls. Hard limit $8."),
    Field("ai.daily_review", "Daily AI review (per mode, Haiku)", "AI (all modes)", "bool"),
    Field("ai.weekly_playbook", "Weekly playbook rewrite (per mode, Sonnet)", "AI (all modes)", "bool"),
    Field("ai.strong_enabled", "Use Claude Sonnet for high-stakes calls", "AI (all modes)", "bool",
          help="Sonnet costs 20x Haiku per token. Used only for second opinions and the weekly playbook."),
    Field("ai.strong_budget_usd", "Sonnet monthly budget ($, part of the total)", "AI (all modes)", "float", 0, 5, 0.25,
          help="When reached: second opinions stop (Haiku's decision stands) and the playbook falls back to Haiku."),
    Field("ai.strong_calls_per_day", "Max Sonnet calls per day", "AI (all modes)", "int", 0, 40, 1),
    Field("ai.second_opinion", "Sonnet second opinion on high-stakes trades", "AI (all modes)", "bool",
          help="Only after Haiku says TAKE, and only if: the 0.01 lot forces extra risk, risk is at or above "
               "the % below, or Haiku's confidence is borderline. Sonnet can veto."),
    Field("ai.second_opinion_risk_pct", "Second opinion when risk per trade >= (%)", "AI (all modes)", "float", 0.1, 8, 0.1),
    Field("news.enabled", "Use news and economic calendar", "News & events", "bool"),
    Field("news.blackout_minutes", "Blackout around high-impact events (min)", "News & events", "int", 0, 120, 5,
          help="No new gold/forex trades this many minutes before/after a high-impact release."),
    Field("news.blackout_medium_impact", "Also black out medium-impact events", "News & events", "bool"),
    Field("news.digest_enabled", "AI news digest (bias per instrument)", "News & events", "bool"),
    Field("news.digest_interval_min", "News digest every (min)", "News & events", "int", 15, 720, 15),
    Field("news.bias_threshold", "Strong news bias threshold (0-1)", "News & events", "float", 0.2, 1, 0.05),
    Field("news.block_on_extreme_risk", "Block trades when news risk is extreme", "News & events", "bool"),
    Field("news.headline_poll_min", "Poll headlines every (min)", "News & events", "int", 2, 120, 1),
    Field("news.feeds", "Headline RSS feeds (one per line)", "News & events", "list"),
    Field("broker.leverage_forex", "Account leverage - forex (1:x)", "Broker: XM Standard", "float", 1, 1000, 1,
          help="Margin = notional / this. Copy from MT5 Specification -> Margin."),
    Field("broker.leverage_metal", "Leverage - GOLD (1:x)", "Broker: XM Standard", "float", 1, 1000, 1),
    Field("broker.leverage_crypto", "Leverage - BTCUSD (1:x)", "Broker: XM Standard", "float", 1, 1000, 1,
          help="XM limits crypto leverage; check your MT5 Specification."),
    Field("broker.margin_call_pct", "Margin call level (%)", "Broker: XM Standard", "float", 0, 200, 5),
    Field("broker.stop_out_pct", "Stop-out level (%)", "Broker: XM Standard", "float", 0, 100, 5,
          help="Biggest loser is closed when equity / margin falls below this."),
    *[Field(f"broker.spread_{s}", f"Spread {_SYM_LABEL[s]} (points)", "Broker: XM Standard", "int", 0, 100000, 1,
            help="Typical XM Standard spread. The live feed spread is used if it's wider.")
      for s in SYMBOLS],
    *[Field(f"broker.swap_{s}_{d}", f"Swap {_SYM_LABEL[s]} {d} (points / lot / night)", "Broker: XM Standard",
            "float", -100000, 100000, 0.1,
            help="Copy from MT5 Specification. Negative = you pay. Defaults are estimates.")
      for s in SYMBOLS for d in ("long", "short")],
    Field("engine.stop_check_seconds", "Check stops every (sec)", "Engine", "int", 1, 60, 1),
]

MODE_FIELDS: list[Field] = [
    Field("enabled", "Account running", "Account", "bool"),
    Field("timeframe", "Timeframe", "Account", "choice", choices=["5m", "15m"],
          help="Candle size the signals run on. M5 = more trades, more noise, more spread cost."),
    Field("risk.risk_per_trade_pct", "Risk per trade (% of equity)", "Risk", "float", 0.1, 5.0, 0.1),
    Field("risk.min_lot_rule", "If 0.01 lot risks more than that", "Risk", "choice", choices=["skip", "allow"],
          help="skip: don't trade. allow: trade 0.01 lot if its risk is within the limit below."),
    Field("risk.min_lot_max_risk_pct", "...allow 0.01 lot up to (% risk)", "Risk", "float", 0.1, 8.0, 0.1),
    Field("risk.max_leverage_forex", "Max effective leverage - forex", "Risk", "float", 1, 50, 1,
          help="Notional / equity per position. XM allows far more; this is your own cap."),
    Field("risk.max_leverage_metal", "Max effective leverage - GOLD", "Risk", "float", 1, 30, 1),
    Field("risk.max_leverage_crypto", "Max effective leverage - BTCUSD", "Risk", "float", 1, 10, 1),
    Field("risk.max_margin_used_pct", "Max margin used (%)", "Risk", "float", 5, 80, 5),
    Field("risk.max_open_positions", "Max open positions", "Risk", "int", 1, 5, 1),
    Field("risk.max_usd_forex_positions", "Max USD forex positions", "Risk", "int", 1, 3, 1),
    Field("risk.daily_loss_limit_pct", "Daily loss limit (%)", "Limits", "float", 0.5, 15, 0.5),
    Field("risk.weekly_loss_limit_pct", "Weekly loss limit (%)", "Limits", "float", 1, 30, 0.5),
    Field("risk.kill_switch_drawdown_pct", "Kill switch: drawdown from peak (%)", "Limits", "float", 5, 50, 1),
    Field("risk.daily_profit_target_pct", "Daily profit target (%) - stop for the day", "Limits", "float", 0, 20, 0.5,
          help="0 = off. A cap, not a quota: when equity is up this much today, no new trades until tomorrow."),
    Field("risk.profit_half_risk_at_pct", "Halve risk once the day is up (%)", "Limits", "float", 0, 20, 0.5,
          help="0 = off. Protects a good day: after this much profit, new trades use half the risk."),
    Field("risk.max_trades_per_day", "Max new trades per day", "Limits", "int", 1, 50, 1),
    Field("risk.loss_streak", "Losses in a row (today) that trigger the rule below", "Limits", "int", 0, 10, 1,
          help="0 = off. Breakeven exits don't count as losses."),
    Field("risk.loss_streak_action", "...then", "Limits", "choice", choices=["stop_day", "half_risk"],
          help="stop_day: done for today. half_risk: keep trading at half risk until a win."),
    Field("risk.max_spread_stop_pct", "Cost filter: skip if spread > this % of the stop", "Limits", "float", 0, 50, 1,
          help="0 = off. On M5 the spread can eat a big part of a small stop - those trades lose on average."),
    Field("exits.default_stop_atr", "Default stop (x ATR)", "Stops & targets", "float", 0.5, 5, 0.1),
    Field("exits.default_target_atr", "Default target (x ATR)", "Stops & targets", "float", 0.5, 10, 0.1),
    Field("exits.min_stop_atr", "Tightest stop (x ATR)", "Stops & targets", "float", 0.3, 5, 0.1),
    Field("exits.max_stop_atr", "Widest stop (x ATR)", "Stops & targets", "float", 0.5, 6, 0.1),
    Field("exits.min_reward_risk", "Min reward : risk", "Stops & targets", "float", 0.5, 5, 0.1),
    Field("exits.breakeven_at_r", "Move stop to breakeven at +R", "Stops & targets", "float", 0, 5, 0.1,
          help="0 = off. E.g. 1.0: at +1R the stop moves to entry, so the trade can't become a loss."),
    Field("signals.trend_pullback", "Trend pullback signal", "Signals", "bool"),
    Field("signals.session_breakout", "Session breakout signal", "Signals", "bool"),
    Field("signals.news_momentum", "News momentum signal", "Signals", "bool"),
    Field("signals.range_reversion", "Range reversion signal", "Signals", "bool",
          help="Ranging markets: fade a poke outside the Bollinger band back to the middle."),
    Field("signals.momentum_breakout", "Momentum breakout signal", "Signals", "bool",
          help="Trending/volatile markets: strong close beyond the 20-candle high/low."),
    Field("signals.liquidity_sweep", "Liquidity sweep signal (SMC)", "Signals", "bool",
          help="A previous-day/session high or low (or equal highs/lows) is swept and the candle closes back "
               "inside with a rejection wick. Stop beyond the wick, target the opposite liquidity. Off by default: "
               "in the first 60-day backtest it lost about 0.5R per trade (73 trades)."),
    Field("signals.smc_filter", "SMC location filter", "Signals", "bool",
          help="Skip any signal whose path is blocked by unswept liquidity (PDH/PDL, session highs/lows, "
               "equal highs/lows) or an opposite order block closer than the stop distance."),
    Field("signals.session_filter", "Gold/forex only in London + NY hours", "Signals", "bool"),
    Field("signals.htf_trend_filter", "Only trade with the H1 trend", "Signals", "bool",
          help="Longs only when H1 price > EMA50 and EMA20 > EMA50 (shorts the opposite). Flat H1 = no trade."),
    Field("adaptive.enabled", "Strategy switching by market regime", "Self-learning", "bool",
          help="Learns which strategy works in trend / range / volatile markets for this account; "
               "switches losers off (with probation later) and halves risk on weak ones."),
    Field("adaptive.min_trades", "Trades needed before judging a strategy", "Self-learning", "int", 5, 100, 1,
          help="Per strategy per regime. Fewer = adapts faster but reacts to luck."),
    Field("adaptive.auto_tune", "Weekly self-tuning of stops/targets (tested first)", "Self-learning", "bool",
          help="Every Saturday, tries nearby stop/target/breakeven settings on recent history and applies "
               "the best ONLY if it also wins on newer data it wasn't tuned on."),
    Field("ai.min_confidence", "Min AI confidence", "AI", "float", 0, 1, 0.05),
    Field("ai.when_unavailable", "When AI is off / over budget", "AI", "choice", choices=["rules_only", "skip"]),
    Field("weekend.mode", "Friday close: gold/forex positions", "Weekend", "choice", choices=["close", "halve", "hold"]),
    Field("weekend.minutes_before_close", "Act N minutes before Friday close", "Weekend", "int", 5, 240, 5),
    Field("weekend.no_new_trades_minutes", "No new gold/forex trades in last N min", "Weekend", "int", 0, 480, 15),
]
FIELD_BY_KEY = {f.key: f for f in FIELDS}
MODE_FIELD_BY_KEY = {f.key: f for f in MODE_FIELDS}


def get_path(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def coerce(f: Field, value: Any) -> Any:
    """Convert a value from the UI to the right type and clamp to hard limits."""
    if f.type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if f.type == "choice":
        v = str(value)
        if v not in f.choices:
            raise ValueError(f"{f.label}: must be one of {f.choices}")
        return v
    if f.type == "list":
        if isinstance(value, str):
            value = value.splitlines()
        return [str(x).strip() for x in value if str(x).strip()]
    num = float(value)
    if f.min is not None:
        num = max(f.min, num)
    if f.max is not None:
        num = min(f.max, num)
    return int(round(num)) if f.type == "int" else round(num, 6)


def _validate_mode(name: str, data: dict | None) -> dict:
    out = copy.deepcopy(MODE_DEFAULTS[name])
    for f in MODE_FIELDS:
        try:
            raw = get_path(data or {}, f.key)
        except (KeyError, TypeError):
            continue
        set_path(out, f.key, coerce(f, raw))
    e = out["exits"]
    if e["min_stop_atr"] > e["max_stop_atr"]:
        e["min_stop_atr"], e["max_stop_atr"] = e["max_stop_atr"], e["min_stop_atr"]
    return out


def validate(data: dict) -> dict:
    """Full, clean settings: defaults + every known key coerced and clamped."""
    out = copy.deepcopy(DEFAULTS)
    for f in FIELDS:
        try:
            raw = get_path(data, f.key)
        except (KeyError, TypeError):
            continue
        set_path(out, f.key, coerce(f, raw))
    out["modes"] = {m: _validate_mode(m, (data.get("modes") or {}).get(m)) for m in MODES}
    i = out["indicators"]
    if i["ema_fast"] >= i["ema_slow"]:
        raise ValueError("Fast EMA must be smaller than slow EMA")
    return out


def cfg_for(settings: dict, mode: str) -> dict:
    """Flatten global + one mode's settings into the shape the engine uses."""
    m = settings["modes"][mode]
    return {
        "mode": mode,
        "trading_mode": settings["trading_mode"],
        "instruments": settings["instruments"],
        "timeframe": m["timeframe"],
        "risk": m["risk"],
        "exits": m["exits"],
        "signals": {**settings["indicators"], **m["signals"]},
        "adaptive": m["adaptive"],
        "ai": {**settings["ai"], **m["ai"]},
        "news": settings["news"],
        "weekend": m["weekend"],
        "broker": settings["broker"],
        "engine": settings["engine"],
    }


class SettingsStore:
    """Cached current settings. The engine reads `store.current` every tick,
    so a save from the UI takes effect within seconds, no restart needed."""

    def __init__(self, db: DB):
        self.db = db
        row = db.one("SELECT data FROM settings_versions ORDER BY id DESC LIMIT 1")
        if row:
            self.current = validate(json.loads(row["data"]))
            self._upgrade_defaults(json.loads(row["data"]))
        else:
            self.current = validate(copy.deepcopy(DEFAULTS))
            self._persist(self.current, "default")
            db.kv_set("settings_defaults_version", DEFAULTS_VERSION)

    def _upgrade_defaults(self, saved: dict) -> None:
        """Saved settings override code defaults, so when the defaults change in a way you agreed
        to, apply them once. The old version stays in Change history and can be reverted."""
        version = self.db.kv_get("settings_defaults_version", 1)
        if version >= DEFAULTS_VERSION:
            return
        data = copy.deepcopy(self.current)
        if version < 2:
            # v2: the agreed mode table (daily caps, trade limits, streak rule, cost/H1 filters),
            # XM spreads as reported by the user, and a total AI budget that now includes Sonnet.
            data["modes"] = copy.deepcopy(MODE_DEFAULTS)
            for sym, old in (("XAU_USD", 35), ("EUR_USD", 16), ("GBP_USD", 21)):
                if data["broker"].get(f"spread_{sym}") == old:
                    data["broker"][f"spread_{sym}"] = DEFAULTS["broker"][f"spread_{sym}"]
            if data["ai"]["monthly_budget_usd"] <= 2.0:
                data["ai"]["monthly_budget_usd"] = DEFAULTS["ai"]["monthly_budget_usd"]
        self.save(data, f"upgrade:defaults-v{DEFAULTS_VERSION}")
        self.db.kv_set("settings_defaults_version", DEFAULTS_VERSION)

    def _persist(self, data: dict, source: str) -> None:
        self.db.execute("INSERT INTO settings_versions(at, source, data) VALUES (?,?,?)",
                        (iso(), source, json.dumps(data)))

    def save(self, data: dict, source: str = "ui") -> dict:
        clean = validate(data)
        self.current = clean
        self._persist(clean, source)
        return clean

    def reset_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode}")
        data = copy.deepcopy(self.current)
        data["modes"][mode] = copy.deepcopy(MODE_DEFAULTS[mode])
        return self.save(data, f"reset:{mode}")

    def revert(self, version_id: int) -> dict:
        row = self.db.one("SELECT data FROM settings_versions WHERE id=?", (version_id,))
        if not row:
            raise ValueError("No such version")
        return self.save(json.loads(row["data"]), "revert")

    def history(self, limit: int = 30) -> list[dict]:
        return self.db.all("SELECT id, at, source FROM settings_versions ORDER BY id DESC LIMIT ?", (limit,))

    def cfg(self, mode: str) -> dict:
        return cfg_for(self.current, mode)

    def active_modes(self) -> list[str]:
        return [m for m in MODES if self.current["modes"][m]["enabled"]]

    @staticmethod
    def schema() -> dict:
        return {"global": [f.__dict__ for f in FIELDS], "mode": [f.__dict__ for f in MODE_FIELDS],
                "modes": MODES, "labels": MODE_LABELS, "blurbs": MODE_BLURBS}
