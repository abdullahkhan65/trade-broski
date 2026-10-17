"""Risk engine: position sizing and account-level limits. The AI can't override any of this.

Sizing: units = (equity x risk%) / (stop distance in USD per unit).
Leverage needed = notional / equity. If that's above the cap the trade is
skipped (or shrunk, if you chose 'shrink' on the settings page).
Margin = notional / broker margin leverage (30:1 forex, 20:1 gold/BTC).
"""
from __future__ import annotations

from dataclasses import dataclass

from .instruments import BROKER_MARGIN_LEVERAGE, Instrument, notional_usd, risk_per_unit_usd


@dataclass
class Order:
    symbol: str
    side: str
    units: float
    entry: float
    stop: float
    target: float
    leverage: float          # effective leverage: notional / equity at entry
    margin: float
    notional: float
    risk_usd: float
    effective_leverage: float
    shrunk: bool = False


@dataclass
class Rejection:
    reason: str


def plan_exits(side: str, entry: float, atr: float, stop_atr: float, target_atr: float
               ) -> tuple[float, float]:
    sd, td = stop_atr * atr, target_atr * atr
    if side == "LONG":
        return entry - sd, entry + td
    return entry + sd, entry - td


def clamp_exit_multiples(cfg: dict, stop_atr: float | None, target_atr: float | None
                         ) -> tuple[float, float]:
    e = cfg["exits"]
    s = stop_atr if stop_atr and stop_atr > 0 else e["default_stop_atr"]
    t = target_atr if target_atr and target_atr > 0 else e["default_target_atr"]
    s = min(max(s, e["min_stop_atr"]), e["max_stop_atr"])
    t = max(t, s * e["min_reward_risk"])
    return s, t


def size_order(inst: Instrument, cfg: dict, side: str, entry: float, atr: float,
               equity: float, margin_used: float, stop_atr: float | None = None,
               target_atr: float | None = None) -> Order | Rejection:
    r = cfg["risk"]
    if equity <= 0:
        return Rejection("no equity")
    s_mult, t_mult = clamp_exit_multiples(cfg, stop_atr, target_atr)
    stop, target = plan_exits(side, entry, atr, s_mult, t_mult)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return Rejection("zero stop distance")

    risk_usd = equity * r["risk_per_trade_pct"] / 100
    units = risk_usd / risk_per_unit_usd(inst, stop_dist, entry)
    cap = float(r[f"max_leverage_{inst.asset_class}"])
    notional = notional_usd(inst, units, entry)
    eff_lev = notional / equity
    shrunk = False
    if eff_lev > cap:
        if r["oversize_action"] != "shrink":
            return Rejection(f"needs {eff_lev:.1f}:1 leverage, cap is {cap:.0f}:1 (stop too tight for this size)")
        units *= cap / eff_lev
        notional = notional_usd(inst, units, entry)
        eff_lev = notional / equity
        risk_usd = units * risk_per_unit_usd(inst, stop_dist, entry)
        shrunk = True

    # The AI's leverage is advisory only: size always comes from the stop distance,
    # and the position's leverage is whatever that size actually is (<= cap).
    margin = notional / BROKER_MARGIN_LEVERAGE[inst.asset_class]
    max_margin = equity * r["max_margin_used_pct"] / 100
    if margin_used + margin > max_margin:
        return Rejection(f"margin would be {100 * (margin_used + margin) / equity:.0f}% of equity, max {r['max_margin_used_pct']:.0f}%")
    if units <= 0:
        return Rejection("size rounds to zero")
    return Order(inst.symbol, side, units, entry, stop, target, round(eff_lev, 2), margin, notional,
                 risk_usd, eff_lev, shrunk)


def portfolio_check(inst: Instrument, cfg: dict, open_positions: list[dict]) -> str | None:
    """Pre-AI checks so we never pay for an AI call on a trade we can't take."""
    r = cfg["risk"]
    if any(p["symbol"] == inst.symbol for p in open_positions):
        return "already have a position on this instrument"
    if len(open_positions) >= r["max_open_positions"]:
        return f"max {r['max_open_positions']} open positions"
    if inst.asset_class == "forex":
        n_fx = sum(1 for p in open_positions if p["asset_class"] == "forex")
        if n_fx >= r["max_usd_forex_positions"]:
            return f"max {r['max_usd_forex_positions']} USD forex positions"
    return None


def account_limit_status(cfg: dict, equity: float, peak: float, day_start: float,
                         week_start: float) -> tuple[str, str] | None:
    """Return (new_status, reason) if a loss limit has been hit."""
    r = cfg["risk"]
    if peak > 0 and (peak - equity) / peak * 100 >= r["kill_switch_drawdown_pct"]:
        return "halted", f"kill switch: equity {equity:.2f} is {100 * (peak - equity) / peak:.1f}% below peak {peak:.2f}"
    if week_start > 0 and (week_start - equity) / week_start * 100 >= r["weekly_loss_limit_pct"]:
        return "paused_weekly", f"weekly loss limit: {100 * (week_start - equity) / week_start:.1f}%"
    if day_start > 0 and (day_start - equity) / day_start * 100 >= r["daily_loss_limit_pct"]:
        return "paused_daily", f"daily loss limit: {100 * (day_start - equity) / day_start:.1f}%"
    return None
