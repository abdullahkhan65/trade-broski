"""Risk engine: lot sizing and account limits. The AI can't override any of this.

Sizing, like you'd do it on MT5:
  risk $          = equity x risk %
  loss per 1 lot  = stop distance x contract size  (in USD)
  lots            = risk $ / loss per 1 lot, rounded DOWN to the 0.01 lot step
If that's below XM's 0.01 minimum, the mode's min-lot rule decides: skip, or
take 0.01 lot if its risk is still within `min_lot_max_risk_pct`.
Effective leverage (notional / equity) must stay under the mode's cap;
margin = notional / XM leverage, and total margin must stay under the cap.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .instruments import Instrument, notional_usd, usd_per_lot


@dataclass
class Order:
    symbol: str
    side: str
    lots: float
    units: float
    entry: float
    stop: float
    target: float
    leverage: float          # effective leverage: notional / equity at entry
    margin: float
    notional: float
    risk_usd: float
    risk_pct: float
    note: str = ""


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


def floor_lots(inst: Instrument, lots: float) -> float:
    steps = math.floor(lots / inst.lot_step + 1e-9)
    return round(min(steps * inst.lot_step, inst.max_lot), 2)


def broker_leverage(cfg: dict, inst: Instrument) -> float:
    return float(cfg["broker"][f"leverage_{inst.asset_class}"])


def size_order(inst: Instrument, cfg: dict, side: str, entry: float, atr: float,
               equity: float, margin_used: float, stop_atr: float | None = None,
               target_atr: float | None = None, risk_mult: float = 1.0) -> Order | Rejection:
    """risk_mult < 1 comes from the adaptive layer (probation / weak strategy): it scales
    both the risk % and the 0.01-lot allowance down. It can never scale them up."""
    risk_mult = min(max(risk_mult, 0.0), 1.0)
    r = cfg["risk"]
    if equity <= 0:
        return Rejection("no equity")
    s_mult, t_mult = clamp_exit_multiples(cfg, stop_atr, target_atr)
    stop, target = plan_exits(side, entry, atr, s_mult, t_mult)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return Rejection("zero stop distance")

    loss_per_lot = usd_per_lot(inst, stop_dist, entry)
    risk_pct = r["risk_per_trade_pct"] * risk_mult
    allow_pct = r["min_lot_max_risk_pct"] * risk_mult
    risk_budget = equity * risk_pct / 100
    lots = floor_lots(inst, risk_budget / loss_per_lot)
    note = ""
    if lots < inst.min_lot:
        min_risk_pct = 100 * loss_per_lot * inst.min_lot / equity
        if r["min_lot_rule"] == "allow" and min_risk_pct <= allow_pct:
            lots = inst.min_lot
            note = f"0.01 lot = {min_risk_pct:.1f}% risk (above {risk_pct:g}%, allowed up to {allow_pct:g}%)"
        else:
            return Rejection(f"0.01 lot would risk {min_risk_pct:.1f}% (${loss_per_lot * inst.min_lot:.2f}); "
                             f"limit {risk_pct if r['min_lot_rule'] == 'skip' else allow_pct:g}%")

    cap = float(r[f"max_leverage_{inst.asset_class}"])
    units = lots * inst.contract_size
    notional = notional_usd(inst, units, entry)
    eff_lev = notional / equity
    if eff_lev > cap:
        lots = floor_lots(inst, lots * cap / eff_lev)
        if lots < inst.min_lot:
            return Rejection(f"0.01 lot = {eff_lev:.1f}x leverage, cap is {cap:.0f}x")
        units = lots * inst.contract_size
        notional = notional_usd(inst, units, entry)
        eff_lev = notional / equity
        note = (note + "; " if note else "") + f"reduced to fit {cap:.0f}x leverage cap"

    margin = notional / broker_leverage(cfg, inst)
    max_margin = equity * r["max_margin_used_pct"] / 100
    if margin_used + margin > max_margin:
        return Rejection(f"margin would be {100 * (margin_used + margin) / equity:.0f}% of equity, max {r['max_margin_used_pct']:.0f}%")
    risk_usd = loss_per_lot * lots
    if risk_mult < 1:
        note = (note + "; " if note else "") + f"risk x{risk_mult:g} (adaptive)"
    return Order(inst.symbol, side, lots, units, entry, stop, target, round(eff_lev, 2), margin, notional,
                 risk_usd, 100 * risk_usd / equity, note)


def min_lot_risk_pct(inst: Instrument, cfg: dict, atr: float, price: float, equity: float) -> float:
    """Risk of a 0.01 lot with the mode's default stop - shown on the dashboard."""
    loss = usd_per_lot(inst, cfg["exits"]["default_stop_atr"] * atr, price) * inst.min_lot
    return 100 * loss / equity if equity > 0 else 0.0


def portfolio_check(inst: Instrument, cfg: dict, open_positions: list[dict]) -> str | None:
    """Pre-AI checks so we never pay for an AI call on a trade we can't take."""
    r = cfg["risk"]
    if any(p["symbol"] == inst.symbol for p in open_positions):
        return "already have a position on this symbol"
    if len(open_positions) >= r["max_open_positions"]:
        return f"max {r['max_open_positions']} open positions"
    if inst.asset_class == "forex":
        n_fx = sum(1 for p in open_positions if p["asset_class"] == "forex")
        if n_fx >= r["max_usd_forex_positions"]:
            return f"max {r['max_usd_forex_positions']} USD forex positions"
    return None


def account_limit_status(cfg: dict, equity: float, peak: float, day_start: float,
                         week_start: float) -> tuple[str, str] | None:
    """Return (new_status, reason) if a loss limit or the daily profit target has been hit."""
    r = cfg["risk"]
    if peak > 0 and (peak - equity) / peak * 100 >= r["kill_switch_drawdown_pct"]:
        return "halted", f"kill switch: equity {equity:.2f} is {100 * (peak - equity) / peak:.1f}% below peak {peak:.2f}"
    if week_start > 0 and (week_start - equity) / week_start * 100 >= r["weekly_loss_limit_pct"]:
        return "paused_weekly", f"weekly loss limit: {100 * (week_start - equity) / week_start:.1f}%"
    if day_start > 0 and (day_start - equity) / day_start * 100 >= r["daily_loss_limit_pct"]:
        return "paused_daily", f"daily loss limit: {100 * (day_start - equity) / day_start:.1f}%"
    target = r.get("daily_profit_target_pct", 0)
    if target and day_start > 0 and (equity - day_start) / day_start * 100 >= target:
        return "target_hit", f"daily profit target reached: +{100 * (equity - day_start) / day_start:.1f}%"
    return None
