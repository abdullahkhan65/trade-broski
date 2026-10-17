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


def adx(h: list[float], l: list[float], c: list[float], period: int = 14) -> Series:
    """Wilder's ADX: trend strength 0-100 (above ~25 = trending, below ~20 = ranging)."""
    n = len(c)
    out: Series = [None] * n
    if n < 2 * period + 1:
        return out
    tr = true_range(h, l, c)
    plus_dm = [0.0] + [max(h[i] - h[i - 1], 0) if (h[i] - h[i - 1]) > (l[i - 1] - l[i]) else 0.0 for i in range(1, n)]
    minus_dm = [0.0] + [max(l[i - 1] - l[i], 0) if (l[i - 1] - l[i]) > (h[i] - h[i - 1]) else 0.0 for i in range(1, n)]
    tr_s, p_s, m_s = sum(tr[1:period + 1]), sum(plus_dm[1:period + 1]), sum(minus_dm[1:period + 1])
    dx: list[float] = []
    for i in range(period + 1, n):
        tr_s = tr_s - tr_s / period + tr[i]
        p_s = p_s - p_s / period + plus_dm[i]
        m_s = m_s - m_s / period + minus_dm[i]
        pdi = 100 * p_s / tr_s if tr_s else 0.0
        mdi = 100 * m_s / tr_s if tr_s else 0.0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
        if len(dx) == period:
            adx_val = sum(dx) / period
            out[i] = adx_val
        elif len(dx) > period:
            adx_val = (out[i - 1] * (period - 1) + dx[-1]) / period  # type: ignore[operator]
            out[i] = adx_val
    return out


def bollinger(values: list[float], period: int = 20, mult: float = 2.0) -> tuple[Series, Series, Series]:
    """(middle, upper, lower) bands."""
    mid: Series = [None] * len(values)
    up: Series = [None] * len(values)
    lo: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        m = sum(w) / period
        sd = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        mid[i], up[i], lo[i] = m, m + mult * sd, m - mult * sd
    return mid, up, lo
