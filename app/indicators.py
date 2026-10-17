"""Plain-Python indicators (no numpy/pandas needed on a 1 GB server).

All functions take oldest-first lists and return lists of the same length,
with None where there isn't enough data yet.
"""
from __future__ import annotations

from typing import Optional

Series = list[Optional[float]]


def ema(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def _rsi(g: float, l: float) -> float:
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return 100 - 100 / (1 + g / l)


def true_range(h: list[float], l: list[float], c: list[float]) -> list[float]:
    tr = []
    for i in range(len(c)):
        if i == 0:
            tr.append(h[i] - l[i])
        else:
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def atr(h: list[float], l: list[float], c: list[float], period: int = 14) -> Series:
    """Wilder's ATR."""
    tr = true_range(h, l, c)
    out: Series = [None] * len(c)
    if len(c) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(c)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def percentile(values: list[float], pct: float) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    idx = min(len(vals) - 1, max(0, int(round(pct / 100 * (len(vals) - 1)))))
    return vals[idx]
