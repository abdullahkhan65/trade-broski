"""Trade management beyond stop / target: the "human instinct" exit.

A discretionary trader who shorted gold at 4070 for 4050 and sees it stall around 4060
doesn't wait for the target or the stop - they take the 10 points and look for the next
setup. Coded as an objective rule, checked on every CLOSED candle of the account's timeframe:

  * the trade has covered at least `stall_min_progress` of the way to its target, and
  * the last `stall_candles` candles made NO new favourable extreme (no lower low for a
    short, no higher high for a long) compared with everything before them since entry, and
  * those candles are tight: their whole range is below `stall_range_atr` x ATR
    (consolidation, not a sharp pullback - a sharp move against us is the stop's job).
  -> close at market: "momentum stalled".

The same function runs in the backtest (backtest.simulate) and live (engine), so what
the backtest says about it is what the bot does.
"""
from __future__ import annotations


def stall_exit(side: str, entry: float, target: float, bars: list[dict], atr: float, cfg: dict) -> str | None:
    """bars = closed candles from the entry candle to now (oldest first). Returns a reason or None."""
    e = cfg["exits"]
    if not e.get("stall_exit") or atr <= 0:
        return None
    n = int(e.get("stall_candles", 4))
    if len(bars) < n + 1:
        return None
    to_target = abs(target - entry)
    if to_target <= 0:
        return None
    last = bars[-1]["c"]
    progress = (last - entry) / to_target if side == "LONG" else (entry - last) / to_target
    if progress < e.get("stall_min_progress", 0.5):
        return None
    recent, before = bars[-n:], bars[:-n]
    if side == "LONG":
        if max(b["h"] for b in recent) > max(b["h"] for b in before):
            return None                      # still making higher highs: let it run
    else:
        if min(b["l"] for b in recent) < min(b["l"] for b in before):
            return None                      # still making lower lows
    if max(b["h"] for b in recent) - min(b["l"] for b in recent) > e.get("stall_range_atr", 1.0) * atr:
        return None                          # not a consolidation
    return f"momentum stalled at {100 * progress:.0f}% of target - took profit"


def _better(side: str, a: float, b: float) -> float:
    """The more protective of two stops."""
    return max(a, b) if side == "LONG" else min(a, b)


def manage(side: str, entry: float, initial_stop: float, stop: float, best_move: float, lots: float,
           min_lot: float, partial_done: bool, cfg: dict) -> tuple[float, float]:
    """Breakeven, partial take-profit and trailing stop, from the best favourable move so far.

    best_move: the furthest price has gone in the trade's favour since entry (price units, >= 0).
    Returns (new_stop, fraction_to_close_now). The partial close happens once, at +partial_at_r,
    and only if the position is at least 2x the minimum lot (0.01 lot can't be split).
    Trailing: once the trade is +trail_r R, the stop follows the best price at trail_r R behind it."""
    e = cfg["exits"]
    init = abs(entry - initial_stop)
    if init <= 0:
        return stop, 0.0
    sign = 1 if side == "LONG" else -1
    new_stop = stop
    be = e.get("breakeven_at_r", 0)
    if be and best_move >= be * init:
        new_stop = _better(side, new_stop, entry)
    close_frac = 0.0
    pa = e.get("partial_at_r", 0)
    if pa and not partial_done and best_move >= pa * init and lots >= 2 * min_lot - 1e-9:
        close_frac = e.get("partial_fraction", 0.5)
        new_stop = _better(side, new_stop, entry)
    tr = e.get("trail_r", 0)
    if tr and best_move >= tr * init:
        new_stop = _better(side, new_stop, entry + sign * (best_move - tr * init))
    return new_stop, close_frac


# Exit profiles per allowlist entry ("US30:zone_retest@liq+trail1"). Deliberately few, so we
# don't fit each setup to its own lucky variant (2-year research, round 3).
PROFILES: dict[str, dict] = {
    "base": {},
    "liq": {"liquidity_target": True},
    "trail1": {"trail_r": 1.0},
    "trail1.5": {"trail_r": 1.5},
    "partial1+trail1": {"partial_at_r": 1.0, "partial_fraction": 0.5, "trail_r": 1.0},
    "liq+trail1": {"liquidity_target": True, "trail_r": 1.0},
}


def with_profile(cfg: dict, profile: str | None) -> dict:
    """cfg with that profile's exit overrides (unknown/empty profile = cfg unchanged)."""
    p = PROFILES.get(profile or "")
    if not p:
        return cfg
    out = dict(cfg)
    out["exits"] = {**cfg["exits"], **{k: v for k, v in p.items() if k != "liquidity_target"}}
    return out
