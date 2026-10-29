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

MODES = ["standard", "moderate", "aggressive", "lab"]
DEFAULTS_VERSION = 7          # bump + add a step in SettingsStore._upgrade_defaults when defaults change
MODE_LABELS = {
    "standard": "Standard",
    "moderate": "Moderate",
    "aggressive": "Aggressive scalper",
    "lab": "Lab",
}
MODE_BLURBS = {
    "standard": "Patient professional: trades only setups that made money in both of the last 2 years in every "
                "account style (GOLD Asian-range breakout, range reversion and previous-day breakout; US30/US100 "
                "opening-range breakouts, order-block/FVG retests and H1 trend pullbacks; DAX retests; FTSE London "
                "breakout and momentum), on M15 with the H1 trend and SMC filters. 1% risk (a 0.01 lot of gold or "
                "0.1 of an index is ~2.5-5% at $250), max 2 trades a day, done for the day after 2 losses.",
    "moderate": "Professional swing/day trader: the same proven setups with 1.5% risk, max 3 trades a day, "
                "half risk after 2 losses; small trial trades on BTC/GBP near-miss setups.",
    "aggressive": "Active trader: ~15 setups across US30, US100, DAX, FTSE, oil, gold and GBPUSD that were "
                  "profitable over 2 years in its own rules (a looser bar than 'proven', ~9 trades a week in the "
                  "backtest). 2.5% risk, quick management (breakeven, trailing, profits taken when momentum stalls), "
                  "up to 6 trades a day. Its AI is advisory: it comments on every trade but the rules decide, so its "
                  "results show what the AI's skips are worth.",
    "lab": "Research account: takes EVERY signal from every strategy with no AI, no filters and no daily caps, "
           "so there is always activity and live data on what works. Refilled to its start balance when it runs "
           "low (a new season); every loss is tagged with its causes. Not a strategy to copy.",
}
SYMBOLS = ["XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY", "BTC_USDT", "US30", "US100", "GER40", "UK100", "WTI"]

DEFAULT_FEEDS = [
    "https://www.investing.com/rss/news_1.rss",
    "https://www.investing.com/rss/news_11.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.fxstreet.com/rss/news",
]

# Built from the 2-year backtest (Oct 2024 - Oct 2026, XM Standard costs): the edge that held in BOTH
# years is GOLD's Asian-range breakout on M15 with the H1-trend + SMC filters (+0.14..0.18R per trade in
# every mode). The M5 "edges" (gold breakout, BTC NY opening range) disappeared with real $250 sizing,
# so all accounts run on M15. Everything else lost money after costs, so each account trades only its
# allowlist; Moderate also trials BTC's London opening-range breakout (about flat over 2 years).
# At $250 a 0.01 lot of gold risks ~3-5%, so every mode must accept the minimum lot on gold.
STALL_FAST = {"stall_exit": True, "stall_min_progress": 0.35, "stall_candles": 3, "stall_range_atr": 0.8,
              "partial_at_r": 0.0, "partial_fraction": 0.5, "trail_r": 0.0}
# Exploration budget: trial trades on unproven-but-promising setups, minimum lot only, capped weekly loss.
# Proven (2-year research, both years positive in all 3 account styles, uniform H1+SMC filters):
# These 7 are ALL the setups on the live markets that passed (~4.6 trades a week per account in the backtest).
PROVEN = ["XAU_USD:session_breakout", "US100:london_orb@liq+trail1", "US30:zone_retest@liq+trail1",
          "US30:ny_orb@liq+trail1", "US30:london_orb@liq+trail1",
          "US100:zone_retest@trail1.5", "XAU_USD:range_reversion@trail1.5",
          # 25 Oct 2026: H1 trend pullback - US30 passed all 9 exit x style combinations (both years); gold and
          # US100 failed. ~0.3 trades a week, only ~30 trades in 2 years: thin evidence, watched by the audit.
          "US30:htf_pullback@liq+trail1"]
# Trials: near misses that can actually trade at $250 within the trial risk cap (0.01 lot of gold or an
# index risks 2.5-5% - far above it, so those can't be trials). Setups that lost in both years were dropped.
TRIALS = ["BTC_USDT:london_orb@trail1.5", "BTC_USDT:range_reversion", "GBP_USD:range_reversion@liq+trail1"]

# 30 Oct 2026 study (app/study.py: every setup x 3 exits x 3 account styles, 2 years to 29 Oct 2026, $250, the
# current exits). New setups that made money in BOTH years in all 3 styles - DAX/FTSE re-tested on cash-hours
# candles only, because that is all the live feed has. Added to every real account.
PROVEN_V7 = ["XAU_USD:pdh_breakout@liq+trail1", "GER40:zone_retest@liq+trail1", "UK100:london_orb@liq+trail1",
             "UK100:momentum_breakout@liq+trail1"]
# The Aggressive account trades for activity: every setup on a live market that clears the AGGRESSIVE bar in its
# own rules (positive over 2 years in R and $, profit factor >= 1.1, 10+ trades a year, weaker year >= -3R),
# best of 3 exit profiles each. ~9 trades a week in the backtest (vs ~2-3 under the proven bar). Looser bar =
# more variance; the audit's drift check and the weekly removals watch every one.
# Gold's Asian breakout, gold range reversion, US30 zone retest and US30 NY ORB (liq) failed it under
# Aggressive's faster management (they stay on Standard/Moderate).
AGGRESSIVE = ["US100:london_orb@liq+trail1", "US100:zone_retest", "US100:htf_pullback",
              "US100:liquidity_sweep@liq+trail1", "US30:london_orb@trail1.5", "US30:htf_pullback@liq+trail1",
              "US30:ny_orb", "XAU_USD:pdh_breakout@liq+trail1", "GBP_USD:ny_orb@liq+trail1",
              "GER40:zone_retest@liq+trail1", "GER40:london_orb@liq+trail1", "GER40:liquidity_sweep@liq+trail1",
              "UK100:london_orb@liq+trail1", "UK100:momentum_breakout@liq+trail1", "WTI:zone_retest@liq+trail1"]


def _explore(budget: float, max_risk: float, per_day: int) -> dict:
    return {"enabled": True, "weekly_budget_pct": budget, "max_risk_pct": max_risk, "max_per_day": per_day,
            "trials": list(TRIALS)}


NEW_SIGNALS = {"setup_rules": [], "zone_retest": True, "htf_pullback": True, "liquidity_targets": False, "asian_range_min_atr": 0.0, "asian_range_max_atr": 0.0}

MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "standard": {
        "enabled": True,
        "timeframe": "15m",
        "risk": {
            "risk_per_trade_pct": 1.0,
            "min_lot_rule": "allow", "min_lot_max_risk_pct": 5.0,
            "max_leverage_forex": 10.0, "max_leverage_metal": 20.0, "max_leverage_crypto": 5.0,
            "max_leverage_index": 25.0, "max_leverage_energy": 10.0,
            "max_margin_used_pct": 50.0,
            "max_open_positions": 2, "max_usd_forex_positions": 1,
            "daily_loss_limit_pct": 5.0, "weekly_loss_limit_pct": 10.0,
            "kill_switch_drawdown_pct": 25.0, "daily_profit_target_pct": 4.0,
            "profit_half_risk_at_pct": 0.0, "max_trades_per_day": 2,
            "loss_streak": 2, "loss_streak_action": "stop_day", "max_spread_stop_pct": 0.0,
        },
        "exits": {"default_stop_atr": 1.5, "default_target_atr": 3.0, "min_stop_atr": 1.0,
                  "max_stop_atr": 3.0, "min_reward_risk": 2.0, "breakeven_at_r": 0.0, **STALL_FAST},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": False,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": True, "liquidity_sweep": False, "smc_filter": True,
                    "london_orb": True, "ny_orb": True, "pdh_breakout": True,
                    "allowlist": PROVEN + PROVEN_V7, **NEW_SIGNALS},
        "adaptive": {"enabled": True, "min_trades": 15, "auto_tune": True, "pool_lab": True},
        "explore": _explore(1.0, 0.6, 1),
        "ai": {"use_ai": True, "veto_proven": True, "min_confidence": 0.55, "when_unavailable": "rules_only"},
        "weekend": {"mode": "close", "minutes_before_close": 30, "no_new_trades_minutes": 120},
    },
    "moderate": {
        "enabled": True,
        "timeframe": "15m",
        "risk": {
            "risk_per_trade_pct": 1.5,
            "min_lot_rule": "allow", "min_lot_max_risk_pct": 6.0,
            "max_leverage_forex": 20.0, "max_leverage_metal": 25.0, "max_leverage_crypto": 5.0,
            "max_leverage_index": 30.0, "max_leverage_energy": 15.0,
            "max_margin_used_pct": 60.0,
            "max_open_positions": 3, "max_usd_forex_positions": 2,
            "daily_loss_limit_pct": 6.0, "weekly_loss_limit_pct": 12.0,
            "kill_switch_drawdown_pct": 30.0, "daily_profit_target_pct": 5.0,
            "profit_half_risk_at_pct": 0.0, "max_trades_per_day": 3,
            "loss_streak": 2, "loss_streak_action": "half_risk", "max_spread_stop_pct": 0.0,
        },
        "exits": {"default_stop_atr": 1.5, "default_target_atr": 2.5, "min_stop_atr": 1.0,
                  "max_stop_atr": 3.0, "min_reward_risk": 1.5, "breakeven_at_r": 1.5, **STALL_FAST},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": True,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": True, "liquidity_sweep": False, "smc_filter": True,
                    "london_orb": True, "ny_orb": True, "pdh_breakout": True,
                    "allowlist": PROVEN + PROVEN_V7, **NEW_SIGNALS},
        "adaptive": {"enabled": True, "min_trades": 15, "auto_tune": True, "pool_lab": True},
        "explore": _explore(2.0, 0.75, 2),
        "ai": {"use_ai": True, "veto_proven": True, "min_confidence": 0.5, "when_unavailable": "rules_only"},
        "weekend": {"mode": "halve", "minutes_before_close": 30, "no_new_trades_minutes": 90},
    },
    "aggressive": {
        "enabled": True,
        "timeframe": "15m",
        "risk": {
            "risk_per_trade_pct": 2.5,
            "min_lot_rule": "allow", "min_lot_max_risk_pct": 6.0,
            "max_leverage_forex": 30.0, "max_leverage_metal": 30.0, "max_leverage_crypto": 10.0,
            "max_leverage_index": 40.0, "max_leverage_energy": 20.0,
            "max_margin_used_pct": 70.0,
            "max_open_positions": 3, "max_usd_forex_positions": 2,
            "daily_loss_limit_pct": 7.0, "weekly_loss_limit_pct": 15.0,
            "kill_switch_drawdown_pct": 35.0, "daily_profit_target_pct": 6.0,
            "profit_half_risk_at_pct": 4.0, "max_trades_per_day": 6,
            "loss_streak": 3, "loss_streak_action": "half_risk", "max_spread_stop_pct": 15.0,
        },
        "exits": {"default_stop_atr": 1.5, "default_target_atr": 2.5, "min_stop_atr": 1.0,
                  "max_stop_atr": 3.0, "min_reward_risk": 1.5, "breakeven_at_r": 1.0, **STALL_FAST},
        "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": True,
                    "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                    "htf_trend_filter": True, "liquidity_sweep": True, "smc_filter": True,
                    "london_orb": True, "ny_orb": True, "pdh_breakout": True,
                    "allowlist": AGGRESSIVE, **NEW_SIGNALS},
        "adaptive": {"enabled": True, "min_trades": 12, "auto_tune": True, "pool_lab": True},
        "explore": _explore(3.0, 1.0, 3),
        "ai": {"use_ai": True, "veto_proven": False, "min_confidence": 0.5, "when_unavailable": "rules_only"},
        "weekend": {"mode": "close", "minutes_before_close": 45, "no_new_trades_minutes": 60},
    },
}

# Lab: takes every signal (no AI, no filters, no daily caps) - activity + live data on every strategy.
MODE_DEFAULTS["lab"] = {
    "enabled": True,
    "timeframe": "15m",
    "risk": {
        "risk_per_trade_pct": 1.0,
        "min_lot_rule": "allow", "min_lot_max_risk_pct": 8.0,
        "max_leverage_forex": 50.0, "max_leverage_metal": 30.0, "max_leverage_crypto": 10.0,
        "max_leverage_index": 50.0, "max_leverage_energy": 30.0,
        "max_margin_used_pct": 80.0,
        "max_open_positions": 6, "max_usd_forex_positions": 3,
        "daily_loss_limit_pct": 15.0, "weekly_loss_limit_pct": 30.0,
        "kill_switch_drawdown_pct": 50.0, "daily_profit_target_pct": 0.0,
        "profit_half_risk_at_pct": 0.0, "max_trades_per_day": 50,
        "loss_streak": 0, "loss_streak_action": "half_risk", "max_spread_stop_pct": 0.0,
    },
    "exits": {"default_stop_atr": 1.5, "default_target_atr": 2.5, "min_stop_atr": 1.0,
              "max_stop_atr": 3.0, "min_reward_risk": 1.5, "breakeven_at_r": 1.0,
              "stall_exit": True, "stall_min_progress": 0.5, "stall_candles": 4, "stall_range_atr": 1.0,
              "partial_at_r": 0.0, "partial_fraction": 0.5, "trail_r": 0.0},
    "signals": {"trend_pullback": True, "session_breakout": True, "news_momentum": True,
                "range_reversion": True, "momentum_breakout": True, "session_filter": True,
                "htf_trend_filter": False, "liquidity_sweep": True, "smc_filter": False,
                "london_orb": True, "ny_orb": True, "pdh_breakout": True, "allowlist": [],
                **NEW_SIGNALS, "zone_retest": True},
    "adaptive": {"enabled": False, "min_trades": 15, "auto_tune": False, "pool_lab": False},
    "explore": {"enabled": False, "weekly_budget_pct": 0.0, "max_risk_pct": 0.5, "max_per_day": 0, "trials": []},
    "ai": {"use_ai": False, "veto_proven": True, "min_confidence": 0.0, "when_unavailable": "rules_only"},
    "weekend": {"mode": "close", "minutes_before_close": 30, "no_new_trades_minutes": 60},
}

DEFAULTS: dict[str, Any] = {
    "trading_mode": "auto",
    "instruments": {"XAU_USD": True, "EUR_USD": True, "GBP_USD": True, "USD_JPY": False, "BTC_USDT": True,
                    "US30": True, "US100": True, "GER40": True, "UK100": True, "WTI": True},
    "indicators": {
        "ema_fast": 20, "ema_slow": 50, "rsi_period": 14, "atr_period": 14,
        "spike_atr_mult": 3.0, "dead_market_percentile": 20.0,
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
        "leverage_forex": 1000.0, "leverage_metal": 1000.0, "leverage_crypto": 50.0, "leverage_index": 100.0,
        "leverage_energy": 100.0,
        "margin_call_pct": 50.0, "stop_out_pct": 20.0,
        "spread_XAU_USD": 50, "spread_EUR_USD": 23, "spread_GBP_USD": 28,
        "spread_USD_JPY": 18, "spread_BTC_USDT": 4000, "spread_US30": 250, "spread_US100": 150,
        "spread_GER40": 150, "spread_UK100": 150, "spread_WTI": 5,
        "swap_XAU_USD_long": -50.0, "swap_XAU_USD_short": 25.0,
        "swap_EUR_USD_long": -8.0, "swap_EUR_USD_short": 2.0,
        "swap_GBP_USD_long": -4.0, "swap_GBP_USD_short": -3.0,
        "swap_USD_JPY_long": 12.0, "swap_USD_JPY_short": -30.0,
        "swap_BTC_USDT_long": -4000.0, "swap_BTC_USDT_short": -2000.0,
        "swap_US30_long": -800.0, "swap_US30_short": -300.0,
        "swap_US100_long": -600.0, "swap_US100_short": -200.0,
        "swap_GER40_long": -500.0, "swap_GER40_short": -100.0,
        "swap_UK100_long": -400.0, "swap_UK100_short": -100.0,
        "swap_WTI_long": -60.0, "swap_WTI_short": -20.0,
    },
    "engine": {"stop_check_seconds": 3},
    "research": {"enabled": True, "min_lab_trades": 10, "min_lab_avg_r": 0.05, "ai_review": True, "hypotheses": True,
                 "weekly_outlook": True, "outlook_web_search": True, "outlook_searches": 8},
    "audit": {"enabled": True, "post_mortems": True, "coaching": True},
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


_SYM_LABEL = {"XAU_USD": "GOLD", "EUR_USD": "EURUSD", "GBP_USD": "GBPUSD", "USD_JPY": "USDJPY", "BTC_USDT": "BTCUSD",
              "US30": "US30Cash", "US100": "US100Cash", "GER40": "GER40Cash", "UK100": "UK100Cash", "WTI": "OILCash"}

FIELDS: list[Field] = [
    Field("trading_mode", "Trading mode (all accounts)", "General", "choice", choices=["auto", "signals_only"],
          help="auto: opens paper trades. signals_only: logs what it WOULD do, opens nothing."),
    *[Field(f"instruments.{s}", lbl, "Instruments", "bool",
            help="Trade this symbol in all 3 accounts. Prices keep streaming even when off.")
      for s, lbl in [("XAU_USD", "GOLD - Binance gold perp prices"), ("EUR_USD", "EURUSD - Kraken prices"),
                     ("GBP_USD", "GBPUSD - Kraken prices"),
                     ("USD_JPY", "USDJPY - Yahoo prices, polled every 15s (less reliable)"),
                     ("BTC_USDT", "BTCUSD - Binance prices"),
                     ("US30", "US30Cash (Dow) - Yahoo futures prices, polled every 15s"),
                     ("US100", "US100Cash (Nasdaq) - Yahoo futures prices, polled every 15s"),
                     ("GER40", "GER40Cash (DAX) - Yahoo cash-index prices, 08:00-16:30 London only"),
                     ("UK100", "UK100Cash (FTSE) - Yahoo cash-index prices, 08:00-16:30 London only"),
                     ("WTI", "OILCash (WTI crude) - Yahoo futures prices, polled every 15s")]],
    Field("indicators.ema_fast", "Fast EMA", "Indicators (all modes)", "int", 5, 100, 1),
    Field("indicators.ema_slow", "Slow EMA", "Indicators (all modes)", "int", 10, 300, 1),
    Field("indicators.rsi_period", "RSI period", "Indicators (all modes)", "int", 2, 50, 1),
    Field("indicators.atr_period", "ATR period", "Indicators (all modes)", "int", 2, 50, 1),
    Field("indicators.spike_atr_mult", "News-spike filter (candle > x ATR)", "Indicators (all modes)", "float", 1.5, 10, 0.5),
    Field("indicators.dead_market_percentile", "Dead-market filter (ATR percentile)", "Indicators (all modes)",
          "float", 0, 50, 5, help="Skip when ATR is below this percentile of the last ~20 days."),
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
    Field("broker.leverage_energy", "Leverage - OILCash (1:x)", "Broker: XM Standard", "float", 1, 1000, 1,
          help="Estimate - check MT5 Specification for OILCash."),
    Field("broker.leverage_index", "Leverage - indices (1:x)", "Broker: XM Standard", "float", 1, 1000, 1,
          help="Estimate - check MT5 Specification for US30Cash / US100Cash."),
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
    Field("audit.enabled", "Daily audit (floor manager)", "Audit", "bool",
          help="00:15 UTC: for each account - why no trades (signal funnel), is a losing streak normal or not, "
               "are live results inside the backtest's range, engine/AI/price health, exploration budget."),
    Field("audit.post_mortems", "Post-mortem every losing trade (Haiku)", "Audit", "bool",
          help="Bad luck or bad decision? Only bad-decision lessons go into the playbook. ~$0.0005 per trade."),
    Field("audit.coaching", "Coaching: review skipped winners and winning trades, daily note, weekly lesson clean-up",
          "Audit", "bool",
          help="Skipped trades that would have won: fair skip or too cautious? Winners: good process or lucky? "
               "A daily coaching note per trader, and every Sunday Sonnet keeps the 10 best lessons, which the AI "
               "reads before every trade. ~$0.30-0.50 a month."),
    Field("research.enabled", "Weekly Lab -> allowlist research", "Research (weekly)", "bool",
          help="Sunday 04:00 UTC: Lab pairs doing well are re-tested on 2 years of history; ones that win in both "
               "years are PROPOSED for an account's allowlist. Losing allowlisted pairs are proposed for removal. "
               "Nothing changes until you approve it in the Learning tab."),
    Field("research.min_lab_trades", "Lab trades needed before a pair is considered", "Research (weekly)", "int", 5, 200, 1),
    Field("research.min_lab_avg_r", "...with an average of at least (R)", "Research (weekly)", "float", -1, 2, 0.05),
    Field("research.hypotheses", "Weekly hypotheses: AI proposes rules, unseen data tests them", "Research (weekly)", "bool",
          help="Sunday, with the research job: Sonnet reads the loss causes and year 1 of each setup's 2-year replay "
               "and proposes up to 3 simple rules (skip an hour, a regime, a volatility band...). Each is tested on "
               "year 2, which it never saw; only rules that improve it become proposals. ~$0.05 a week."),
    Field("research.weekly_outlook", "Weekly outlook every Sunday (plan per instrument)", "Research (weekly)", "bool",
          help="Sunday 18:00 UTC, before the markets open: Sonnet scans the week ahead (central banks, US data, "
               "earnings, geopolitics, crypto) and writes a plan per instrument - bias, key events with times, "
               "levels, which setups suit the week, days to stand aside. Floor page + Telegram; a one-line summary "
               "is given to the AI gate as context. ~$0.15-0.40 a week."),
    Field("research.outlook_web_search", "...let it search the web", "Research (weekly)", "bool",
          help="Anthropic's web search tool ($0.01 per search). Off = our own calendar and headlines only."),
    Field("research.outlook_searches", "...max web searches", "Research (weekly)", "int", 1, 20, 1),
    Field("research.ai_review", "Weekly research review by Sonnet", "Research (weekly)", "bool",
          help="Sonnet reads the week's results, Lab league table, skipped-signal outcomes and proposals, and "
               "writes a short review with recommendations (~$0.05 a week)."),
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
    Field("risk.max_leverage_energy", "Max effective leverage - OILCash", "Risk", "float", 1, 50, 1),
    Field("risk.max_leverage_index", "Max effective leverage - indices", "Risk", "float", 1, 50, 1,
          help="0.1 lot of US30 is ~$5,000 notional = ~20x a $250 account."),
    Field("risk.max_leverage_crypto", "Max effective leverage - BTCUSD", "Risk", "float", 1, 10, 1),
    Field("risk.max_margin_used_pct", "Max margin used (%)", "Risk", "float", 5, 80, 5),
    Field("risk.max_open_positions", "Max open positions", "Risk", "int", 1, 6, 1),
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
    Field("exits.partial_at_r", "Partial take-profit at +R (0 = off)", "Stops & targets", "float", 0, 5, 0.1,
          help="Close part of the position here and move the stop to entry. Needs 2x the minimum lot "
               "(a 0.01 lot can't be split), so it starts working as the account grows."),
    Field("exits.partial_fraction", "...close this share", "Stops & targets", "float", 0.1, 0.9, 0.05),
    Field("exits.trail_r", "Trailing stop: N R behind the best price (0 = off)", "Stops & targets", "float", 0, 5, 0.1,
          help="Once the trade is +N R, the stop follows the best price at N R behind it."),
    Field("exits.stall_exit", "Take profit when momentum stalls", "Stops & targets", "bool",
          help="Human instinct: in profit, no new high/low for N candles and price is just consolidating -> "
               "take what we have and look for the next setup."),
    Field("exits.stall_min_progress", "...once this share of the target is reached", "Stops & targets", "float", 0.1, 0.95, 0.05,
          help="0.5 = halfway to the take-profit."),
    Field("exits.stall_candles", "...and no progress for N candles", "Stops & targets", "int", 2, 12, 1),
    Field("exits.stall_range_atr", "...while those candles stay within x ATR", "Stops & targets", "float", 0.3, 3, 0.1,
          help="Tight range = consolidation. A sharp move against the trade is left to the stop."),
    Field("signals.trend_pullback", "Trend pullback signal", "Signals", "bool"),
    Field("signals.session_breakout", "Session breakout signal", "Signals", "bool"),
    Field("signals.news_momentum", "News momentum signal", "Signals", "bool"),
    Field("signals.range_reversion", "Range reversion signal", "Signals", "bool",
          help="Ranging markets: fade a poke outside the Bollinger band back to the middle."),
    Field("signals.momentum_breakout", "Momentum breakout signal", "Signals", "bool",
          help="Trending/volatile markets: strong close beyond the 20-candle high/low."),
    Field("signals.london_orb", "London opening-range breakout", "Signals", "bool",
          help="First close beyond the first hour of London (08:00-09:00 London time), within 3 hours."),
    Field("signals.ny_orb", "New York opening-range breakout", "Signals", "bool",
          help="First close beyond the first hour after the NY open (09:30-10:30 NY time), within 3 hours."),
    Field("signals.pdh_breakout", "Previous-day high/low breakout", "Signals", "bool",
          help="First close of the day beyond yesterday's high/low with a strong candle."),
    Field("signals.htf_pullback", "H1 trend pullback into a fresh H1 order block / FVG", "Signals", "bool",
          help="Trend continuation: in an H1 up-trend, price dips into a fresh bullish H1 zone and the M15 candle "
               "closes back above it (mirror for down-trends). Proven on US30 only."),
    Field("signals.zone_retest", "Order block / FVG retest (SMC)", "Signals", "bool",
          help="First return into a fresh order block or fair value gap that closes back out of it. Stop beyond the zone."),
    Field("signals.liquidity_targets", "Target the next liquidity level", "Signals", "bool",
          help="Take-profit at the nearest untouched opposite high/low (PDH/PDL, session, equal highs/lows) at "
               "least min reward:risk away, instead of a fixed ATR multiple."),
    Field("signals.asian_range_min_atr", "Session breakout: min Asian range (x ATR, 0 = off)", "Signals", "float", 0, 10, 0.1),
    Field("signals.asian_range_max_atr", "Session breakout: max Asian range (x ATR, 0 = off)", "Signals", "float", 0, 20, 0.1,
          help="A very wide Asian range often means the day's move already happened."),
    Field("signals.allowlist", "Only these symbol:strategy pairs (one per line)", "Signals", "list",
          help="Empty = every enabled strategy on every symbol. Otherwise e.g. XAU_USD:session_breakout, "
               "BTC_USDT:*, *:ny_orb. Built from the 2-year backtest: pairs that made money in both years."),
    Field("signals.setup_rules", "Per-setup rules (one per line)", "Signals", "list",
          help="Extra conditions for one setup, e.g. 'XAU_USD:session_breakout skip_hours=13,14'. Knobs: "
               "skip_hours, skip_days (Mon..Fri), regimes (trend,range,volatile,mixed), sides (LONG/SHORT), "
               "min_atr_pct / max_atr_pct (volatility percentile 0-100), min_adx / max_adx. Added when you approve "
               "a hypothesis that passed the walk-forward test (Learning tab)."),
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
    Field("adaptive.pool_lab", "Also learn from the Lab's trades (half weight)", "Self-learning", "bool",
          help="The Lab trades every signal, so it collects evidence much faster. Counts its trades of the same "
               "strategy, regime and allowed symbols at half weight."),
    Field("adaptive.auto_tune", "Weekly self-tuning of stops/targets (tested first)", "Self-learning", "bool",
          help="Every Saturday, tries nearby stop/target/breakeven settings on recent history and applies "
               "the best ONLY if it also wins on newer data it wasn't tuned on."),
    Field("explore.enabled", "Exploration budget (trial trades)", "Exploration", "bool",
          help="Besides proven setups, take trial trades on promising unproven ones - minimum lot only, "
               "with a capped weekly loss - so the account keeps learning every day."),
    Field("explore.weekly_budget_pct", "Max trial losses per week (% of equity)", "Exploration", "float", 0, 5, 0.25,
          help="When trial trades have lost this much since Monday, exploration pauses until next week."),
    Field("explore.max_risk_pct", "Trial trade: skip if 0.01 lot risks more than (%)", "Exploration", "float", 0.1, 1.5, 0.05,
          help="Trials always use the minimum lot. Gold at $250 (~4%) is too big for a trial; forex/BTC fit."),
    Field("explore.max_per_day", "Max trial trades per day", "Exploration", "int", 0, 10, 1),
    Field("explore.trials", "Trial setups (symbol:strategy, one per line)", "Exploration", "list",
          help="Promising but unproven: the Lab's best and the 2-year near-misses. Trials that keep winning get "
               "proposed for the allowlist by the weekly research job."),
    Field("ai.use_ai", "Ask Claude before trades", "AI", "bool",
          help="Off = rules only, no AI cost (the Lab account). The daily review and playbook are skipped too."),
    Field("ai.veto_proven", "AI can veto proven setups", "AI", "bool",
          help="Off = advisory: on allowlisted (proven) setups the AI still reads every trade and writes its view, "
               "but the coded rules decide, with the coded stops, and no Sonnet second opinion. Trades the AI "
               "wanted to skip are labelled '[AI would skip: ...]', so their results show whether its skips help. "
               "Trials always need the AI's approval."),
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
        "explore": m["explore"],
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
        if version < 3:
            # v3: modes rebuilt from the 2-year backtest (allowlists, gold min-lot, stall exit), Lab account added
            data["modes"] = copy.deepcopy(MODE_DEFAULTS)
            from .ai.review import SEED_PLAYBOOKS
            for mode, text in SEED_PLAYBOOKS.items():
                self.db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
                                (mode, iso(), text, "seed-v3"))
        if version < 4:
            # v4: US30/US100 live; index setups on the allowlists with exit profiles; exploration budget
            data["modes"] = copy.deepcopy(MODE_DEFAULTS)
            for sym in ("US30", "US100"):
                data["instruments"][sym] = True
            from .ai.review import SEED_PLAYBOOKS
            for mode, text in SEED_PLAYBOOKS.items():
                self.db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
                                (mode, iso(), text, "seed-v4"))
        if version < 5:
            # v5 (21 Oct 2026): all 7 robust setups on the allowlists, trials cut to ones that can trade at
            # $250 and didn't lose in both years. Only these two lists change; your other settings stay.
            for m in ("standard", "moderate", "aggressive"):
                data["modes"][m]["signals"]["allowlist"] = list(PROVEN)
                data["modes"][m]["signals"]["range_reversion"] = True
                data["modes"][m]["signals"]["zone_retest"] = True
                data["modes"][m]["explore"]["trials"] = list(TRIALS)
            from .ai.review import SEED_PLAYBOOKS
            for mode, text in SEED_PLAYBOOKS.items():
                self.db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
                                (mode, iso(), text, "seed-v5"))
        if version < 6:
            # v6 (25 Oct 2026): US30 H1 trend pullback joins the allowlists; the Lab trades it too (live data)
            for m in ("standard", "moderate", "aggressive"):
                if "US30:htf_pullback@liq+trail1" not in data["modes"][m]["signals"]["allowlist"]:
                    data["modes"][m]["signals"]["allowlist"].append("US30:htf_pullback@liq+trail1")
            for m in MODES:
                data["modes"][m]["signals"]["htf_pullback"] = True
        if version < 7:
            # v7 (30 Oct 2026): GER40, UK100 and oil live; 4 new proven setups for every real account; the
            # Aggressive account trades the AGGRESSIVE list with an advisory AI; Lab trades the new markets too
            for sym in ("GER40", "UK100", "WTI"):
                data["instruments"][sym] = True
            for m in ("standard", "moderate"):
                sig = data["modes"][m]["signals"]
                sig["allowlist"] = sig["allowlist"] + [x for x in PROVEN_V7 if x not in sig["allowlist"]]
                sig["pdh_breakout"] = True
            ag = data["modes"]["aggressive"]
            ag["signals"].update(allowlist=list(AGGRESSIVE), pdh_breakout=True, liquidity_sweep=True,
                                 momentum_breakout=True)
            ag["ai"]["veto_proven"] = False
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
