"""Trading rules you can change from the settings page.

* DEFAULTS is the "balanced" profile from the spec.
* PRESETS override a subset of keys for conservative / aggressive trading.
* FIELDS describes every editable key (type, limits, help text). The settings
  page is generated from it, and every save is validated against it.
* The `max` on each field is a HARD LIMIT: the UI, the API and the engine can
  never go past it. Change it here, in code, if you really mean to.

Every save is stored as a new row in settings_versions, so you can see what
changed and roll back from the UI.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from .db import DB, iso

DEFAULT_FEEDS = [
    "https://www.fxstreet.com/rss/news",
    "https://www.investing.com/rss/news_1.rss",
    "https://www.investing.com/rss/news_11.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

DEFAULTS: dict[str, Any] = {
    "profile": "balanced",
    "trading_mode": "auto",
    "instruments": {"XAU_USD": True, "EUR_USD": True, "GBP_USD": True, "USD_JPY": False, "BTC_USDT": True},
    "risk": {
        "risk_per_trade_pct": 1.0,
        "max_leverage_forex": 10.0,
        "max_leverage_metal": 5.0,
        "max_leverage_crypto": 3.0,
        "max_margin_used_pct": 50.0,
        "max_open_positions": 3,
        "max_usd_forex_positions": 2,
        "oversize_action": "skip",
        "daily_loss_limit_pct": 3.0,
        "weekly_loss_limit_pct": 6.0,
        "kill_switch_drawdown_pct": 20.0,
        "stop_out_margin_level_pct": 50.0,
    },
    "exits": {
        "default_stop_atr": 1.5,
        "default_target_atr": 3.0,
        "min_stop_atr": 1.0,
        "max_stop_atr": 3.0,
        "min_reward_risk": 1.5,
    },
    "signals": {
        "trend_pullback": True,
        "session_breakout": True,
        "news_momentum": False,
        "session_filter": True,
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_period": 14,
        "atr_period": 14,
        "spike_atr_mult": 3.0,
        "dead_market_percentile": 20.0,
        "auto_disable_after_trades": 30,
    },
    "ai": {
        "enabled": True,
        "min_confidence": 0.55,
        "max_calls_per_day": 60,
        "monthly_budget_usd": 2.0,
        "when_unavailable": "rules_only",
        "daily_review": True,
        "weekly_playbook": True,
    },
    "news": {
        "enabled": True,
        "blackout_minutes": 15,
        "blackout_medium_impact": False,
        "digest_enabled": True,
        "digest_interval_min": 60,
        "bias_threshold": 0.6,
        "block_on_extreme_risk": True,
        "headline_poll_min": 10,
        "feeds": DEFAULT_FEEDS,
    },
    "weekend": {
        "mode": "close",
        "minutes_before_close": 30,
        "no_new_trades_minutes": 90,
    },
    "costs": {
        "btc_taker_fee_pct": 0.05,
        "btc_slippage_pct": 0.02,
        "min_spread_XAU_USD": 0.30, "min_spread_EUR_USD": 0.00008,
        "min_spread_GBP_USD": 0.00012, "min_spread_USD_JPY": 0.012,
        "swap_XAU_USD_long": -6.5, "swap_XAU_USD_short": 1.5,
        "swap_EUR_USD_long": -3.5, "swap_EUR_USD_short": 0.8,
        "swap_GBP_USD_long": -2.5, "swap_GBP_USD_short": -0.3,
        "swap_USD_JPY_long": 2.5, "swap_USD_JPY_short": -5.5,
    },
    "engine": {
        "stop_check_seconds": 5,
    },
}

PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "risk.risk_per_trade_pct": 0.5,
        "risk.max_leverage_forex": 5.0,
        "risk.max_leverage_metal": 3.0,
        "risk.max_leverage_crypto": 2.0,
        "risk.max_margin_used_pct": 35.0,
        "risk.max_open_positions": 2,
        "risk.max_usd_forex_positions": 1,
        "risk.oversize_action": "skip",
        "risk.daily_loss_limit_pct": 2.0,
        "risk.weekly_loss_limit_pct": 4.0,
        "risk.kill_switch_drawdown_pct": 15.0,
        "exits.min_reward_risk": 2.0,
        "signals.news_momentum": False,
        "signals.session_filter": True,
        "ai.min_confidence": 0.65,
        "ai.when_unavailable": "skip",
        "news.blackout_minutes": 30,
        "news.blackout_medium_impact": True,
        "weekend.mode": "close",
    },
    "balanced": {},   # = DEFAULTS
    "aggressive": {
        "risk.risk_per_trade_pct": 2.0,
        "risk.max_leverage_forex": 20.0,
        "risk.max_leverage_metal": 10.0,
        "risk.max_leverage_crypto": 5.0,
        "risk.max_margin_used_pct": 70.0,
        "risk.max_open_positions": 4,
        "risk.max_usd_forex_positions": 3,
        "risk.oversize_action": "shrink",
        "risk.daily_loss_limit_pct": 5.0,
        "risk.weekly_loss_limit_pct": 10.0,
        "risk.kill_switch_drawdown_pct": 30.0,
        "exits.min_reward_risk": 1.3,
        "signals.news_momentum": True,
        "signals.session_filter": True,
        "ai.min_confidence": 0.5,
        "ai.when_unavailable": "rules_only",
        "news.blackout_minutes": 10,
        "news.blackout_medium_impact": False,
        "news.bias_threshold": 0.5,
        "weekend.mode": "halve",
    },
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


FIELDS: list[Field] = [
    Field("trading_mode", "Trading mode", "General", "choice",
          choices=["auto", "signals_only"],
          help="auto: opens paper trades by itself. signals_only: logs + alerts what it WOULD do, opens nothing."),
    # instruments
    *[Field(f"instruments.{s}", label, "Instruments", "bool",
            help="Trade this instrument. Prices keep streaming even when off.")
      for s, label in [("XAU_USD", "XAU/USD (gold) - Binance gold perp prices"),
                       ("EUR_USD", "EUR/USD - Kraken prices"),
                       ("GBP_USD", "GBP/USD - Kraken prices"),
                       ("USD_JPY", "USD/JPY - Yahoo prices, polled every 15s (less reliable)"),
                       ("BTC_USDT", "BTC/USDT perp - Binance prices")]],
    # risk
    Field("risk.risk_per_trade_pct", "Risk per trade (% of equity)", "Risk", "float", 0.1, 3.0, 0.1,
          help="How much you lose if the stop is hit. Size is calculated from this, not from leverage."),
    Field("risk.max_leverage_forex", "Max leverage - forex", "Risk", "float", 1, 30, 1,
          help="Cap for EUR/USD, GBP/USD, USD/JPY. Hard limit 30:1 (EU/UK retail cap)."),
    Field("risk.max_leverage_metal", "Max leverage - gold", "Risk", "float", 1, 20, 1,
          help="Cap for XAU/USD. Hard limit 20:1."),
    Field("risk.max_leverage_crypto", "Max leverage - BTC", "Risk", "float", 1, 10, 1,
          help="Cap for BTC perp. Hard limit 10:1 - BTC can move 10% in hours."),
    Field("risk.max_margin_used_pct", "Max total margin used (% of equity)", "Risk", "float", 5, 80, 5),
    Field("risk.max_open_positions", "Max open positions", "Risk", "int", 1, 5, 1,
          help="Never more than one position per instrument."),
    Field("risk.max_usd_forex_positions", "Max USD forex positions at once", "Risk", "int", 1, 3, 1,
          help="EUR/USD, GBP/USD and USD/JPY are all USD bets; this stops you stacking the same bet."),
    Field("risk.oversize_action", "If size needs more leverage than the cap", "Risk", "choice",
          choices=["skip", "shrink"],
          help="skip: don't take the trade (spec default). shrink: take a smaller size that fits the cap."),
    Field("risk.daily_loss_limit_pct", "Daily loss limit (%)", "Risk", "float", 0.5, 10, 0.5,
          help="Pause new trades until the next UTC day."),
    Field("risk.weekly_loss_limit_pct", "Weekly loss limit (%)", "Risk", "float", 1, 20, 0.5,
          help="Pause new trades until Monday."),
    Field("risk.kill_switch_drawdown_pct", "Kill switch: drawdown from peak (%)", "Risk", "float", 5, 40, 1,
          help="Closes everything and halts until you resume manually."),
    Field("risk.stop_out_margin_level_pct", "Stop-out margin level (%)", "Risk", "float", 20, 100, 5,
          help="If equity / margin used falls below this, the biggest loser is closed."),
    # exits
    Field("exits.default_stop_atr", "Default stop (x ATR)", "Stops & targets", "float", 0.5, 5, 0.1),
    Field("exits.default_target_atr", "Default target (x ATR)", "Stops & targets", "float", 0.5, 10, 0.1),
    Field("exits.min_stop_atr", "Tightest stop allowed (x ATR)", "Stops & targets", "float", 0.5, 5, 0.1),
    Field("exits.max_stop_atr", "Widest stop allowed (x ATR)", "Stops & targets", "float", 0.5, 6, 0.1),
    Field("exits.min_reward_risk", "Min reward : risk", "Stops & targets", "float", 0.5, 5, 0.1,
          help="Target distance must be at least this many times the stop distance."),
    # signals
    Field("signals.trend_pullback", "Trend pullback signal", "Signals", "bool"),
    Field("signals.session_breakout", "Session breakout signal", "Signals", "bool",
          help="Break of the Asian-session range during London/NY (gold + forex)."),
    Field("signals.news_momentum", "News momentum signal", "Signals", "bool",
          help="Trade in the direction of a strong AI news bias when price confirms it."),
    Field("signals.session_filter", "Only trade gold/forex in London + NY hours", "Signals", "bool"),
    Field("signals.ema_fast", "Fast EMA", "Signals", "int", 5, 100, 1),
    Field("signals.ema_slow", "Slow EMA", "Signals", "int", 10, 300, 1),
    Field("signals.rsi_period", "RSI period", "Signals", "int", 2, 50, 1),
    Field("signals.atr_period", "ATR period", "Signals", "int", 2, 50, 1),
    Field("signals.spike_atr_mult", "News-spike filter (candle > x ATR)", "Signals", "float", 1.5, 10, 0.5),
    Field("signals.dead_market_percentile", "Dead-market filter (ATR percentile)", "Signals", "float", 0, 50, 5,
          help="Skip when current ATR is below this percentile of the last ~20 days."),
    Field("signals.auto_disable_after_trades", "Auto-disable losing signals after N trades", "Signals", "int", 10, 500, 5,
          help="A signal type with negative expectancy after N trades on an instrument is switched off."),
    # ai
    Field("ai.enabled", "Use Claude Haiku to approve trades", "AI", "bool"),
    Field("ai.min_confidence", "Min AI confidence to take a trade", "AI", "float", 0, 1, 0.05),
    Field("ai.max_calls_per_day", "Max AI calls per day (all purposes)", "AI", "int", 0, 150, 1),
    Field("ai.monthly_budget_usd", "AI monthly budget ($)", "AI", "float", 0, 5, 0.25,
          help="Hard stop. Hard limit $5 keeps the whole bill under $8 with the server."),
    Field("ai.when_unavailable", "When AI is off or over budget", "AI", "choice",
          choices=["rules_only", "skip"],
          help="rules_only: trade on coded signals alone. skip: don't trade without AI. API errors always skip."),
    Field("ai.daily_review", "Daily AI review", "AI", "bool"),
    Field("ai.weekly_playbook", "Weekly AI playbook rewrite", "AI", "bool"),
    # news
    Field("news.enabled", "Use news and economic calendar", "News & events", "bool"),
    Field("news.blackout_minutes", "Blackout around high-impact events (min)", "News & events", "int", 0, 120, 5,
          help="No new gold/forex trades this many minutes before and after a high-impact release."),
    Field("news.blackout_medium_impact", "Also black out medium-impact events", "News & events", "bool"),
    Field("news.digest_enabled", "AI news digest (bias per instrument)", "News & events", "bool"),
    Field("news.digest_interval_min", "News digest every (min)", "News & events", "int", 15, 720, 15,
          help="Each digest is one cheap Haiku call (~$0.0005). Only runs when there are new headlines."),
    Field("news.bias_threshold", "Strong news bias threshold (0-1)", "News & events", "float", 0.2, 1, 0.05,
          help="Used by the news momentum signal."),
    Field("news.block_on_extreme_risk", "Block trades when news risk is extreme", "News & events", "bool"),
    Field("news.headline_poll_min", "Poll headlines every (min)", "News & events", "int", 2, 120, 1),
    Field("news.feeds", "Headline RSS feeds (one per line)", "News & events", "list"),
    # weekend
    Field("weekend.mode", "Friday close: gold/forex positions", "Weekend", "choice",
          choices=["close", "halve", "hold"],
          help="Monday gaps can skip your stop. close is safest."),
    Field("weekend.minutes_before_close", "Act this many minutes before Friday close", "Weekend", "int", 5, 240, 5),
    Field("weekend.no_new_trades_minutes", "No new gold/forex trades in last N min of the week", "Weekend", "int", 0, 480, 15),
    # costs
    Field("costs.btc_taker_fee_pct", "BTC taker fee per side (%)", "Costs (simulation)", "float", 0, 0.2, 0.01),
    Field("costs.btc_slippage_pct", "BTC slippage (%)", "Costs (simulation)", "float", 0, 0.5, 0.01),
    *[Field(f"costs.min_spread_{s}", f"Min spread {s.replace('_', '/')} (price units)", "Costs (simulation)",
            "float", 0, mx, mx / 100,
            help="Exchange quotes are tighter than a retail broker's. If the live spread is smaller, "
                 "it's widened to this around the mid price, so paper results aren't flattered.")
      for s, mx in [("XAU_USD", 2.0), ("EUR_USD", 0.001), ("GBP_USD", 0.001), ("USD_JPY", 0.1)]],
    *[Field(f"costs.swap_{s}_{d}", f"Swap {s.replace('_', '/')} {d} (% per year)", "Costs (simulation)",
            "float", -20, 20, 0.1, help="Negative = you pay. Copy current rates from your (future) broker's swap table.")
      for s in ["XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY"] for d in ["long", "short"]],
    Field("engine.stop_check_seconds", "Check stops every (sec)", "Engine", "int", 1, 60, 1),
]
FIELD_BY_KEY = {f.key: f for f in FIELDS}


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


def validate(data: dict) -> dict:
    """Return a full, clean settings dict: defaults + every known key coerced."""
    out = copy.deepcopy(DEFAULTS)
    out["profile"] = str(data.get("profile", "custom"))
    for f in FIELDS:
        try:
            raw = get_path(data, f.key)
        except (KeyError, TypeError):
            continue
        set_path(out, f.key, coerce(f, raw))
    e = out["exits"]
    if e["min_stop_atr"] > e["max_stop_atr"]:
        e["min_stop_atr"], e["max_stop_atr"] = e["max_stop_atr"], e["min_stop_atr"]
    s = out["signals"]
    if s["ema_fast"] >= s["ema_slow"]:
        raise ValueError("Fast EMA must be smaller than slow EMA")
    return out


def preset(name: str) -> dict:
    if name not in PRESETS:
        raise ValueError(f"Unknown preset {name}")
    out = copy.deepcopy(DEFAULTS)
    for k, v in PRESETS[name].items():
        set_path(out, k, v)
    out["profile"] = name
    return validate(out)


class SettingsStore:
    """Cached current settings. The engine reads `store.current` every tick,
    so a save from the UI takes effect within seconds, no restart needed."""

    def __init__(self, db: DB):
        self.db = db
        row = db.one("SELECT data FROM settings_versions ORDER BY id DESC LIMIT 1")
        if row:
            self.current = validate(json.loads(row["data"]))
        else:
            self.current = validate(copy.deepcopy(DEFAULTS))
            self._persist(self.current, "default")

    def _persist(self, data: dict, source: str) -> None:
        self.db.execute("INSERT INTO settings_versions(at, source, data) VALUES (?,?,?)",
                        (iso(), source, json.dumps(data)))

    def save(self, data: dict, source: str = "ui") -> dict:
        clean = validate(data)
        if source == "ui":
            clean["profile"] = data.get("profile") or "custom"
        self.current = clean
        self._persist(clean, source)
        return clean

    def apply_preset(self, name: str) -> dict:
        data = preset(name)
        # keep your instrument choices and news feeds when switching profile
        data["instruments"] = self.current["instruments"]
        data["news"]["feeds"] = self.current["news"]["feeds"]
        data["costs"] = self.current["costs"]
        return self.save(data, f"preset:{name}")

    def revert(self, version_id: int) -> dict:
        row = self.db.one("SELECT data FROM settings_versions WHERE id=?", (version_id,))
        if not row:
            raise ValueError("No such version")
        return self.save(json.loads(row["data"]), "revert")

    def history(self, limit: int = 30) -> list[dict]:
        return self.db.all("SELECT id, at, source FROM settings_versions ORDER BY id DESC LIMIT ?", (limit,))

    def max_leverage(self, asset_class: str) -> float:
        return float(self.current["risk"][f"max_leverage_{asset_class}"])

    @staticmethod
    def schema() -> list[dict]:
        return [f.__dict__ for f in FIELDS]
