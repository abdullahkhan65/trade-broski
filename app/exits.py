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
