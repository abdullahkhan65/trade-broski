"""Free, coded signal engine. Runs on CLOSED candles (M15, or M5 for the scalper).

Signals:
* trend_pullback  - price on the trend side of the slow EMA pulls back to the fast EMA
                    and RSI turns back in the trend direction.
* session_breakout - (gold/forex) a close beyond the Asian-session high/low during
                    London or New York hours.
* news_momentum   - the AI news digest has a strong bias for the instrument and
                    price confirms it (close beyond fast EMA, fast EMA beyond slow EMA).

Filters (apply to all signals):
* dead market: ATR below its N-th percentile of the last ~20 days
* news spike: last candle range > X * ATR
* session filter: gold/forex only during London + New York
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import indicators as ind
from .instruments import Instrument
from .sessions import asian_range_bounds, in_trading_session, is_forex_like

TWENTY_DAYS_OF_15M = 20 * 24 * 4
TF_SECONDS = {"5m": 300, "15m": 900}


def _tf(cfg: dict) -> int:
    return TF_SECONDS.get(cfg.get("timeframe", "15m"), 900)


@dataclass
class Signal:
    symbol: str
    side: str                 # LONG | SHORT
    type: str                 # trend_pullback | session_breakout | news_momentum
    price: float              # close of the signal candle
    atr: float
    candle_t: int
    note: str
    context: dict = field(default_factory=dict)


@dataclass
class FilterResult:
    ok: bool
    reason: str = ""
    atr: float = 0.0


def check_filters(inst: Instrument, candles: list[dict], cfg: dict, now: datetime,
                  atr_floor: float | None = None) -> FilterResult:
    s = cfg["signals"]
    if len(candles) < max(s["ema_slow"], s["atr_period"]) + 5:
        return FilterResult(False, "not enough candle history")
    h = [c["h"] for c in candles]
    l = [c["l"] for c in candles]
    c = [c["c"] for c in candles]
    atr_series = ind.atr(h, l, c, s["atr_period"])
    cur_atr = atr_series[-1]
    if not cur_atr:
        return FilterResult(False, "ATR not ready")
    if atr_floor is None:
        window = [a for a in atr_series[-(20 * 86400 // _tf(cfg)):] if a]
        floor = ind.percentile(window, s["dead_market_percentile"])
    else:
        floor = atr_floor      # backtest passes a precomputed 20-day floor (much faster)
    if s["dead_market_percentile"] > 0 and cur_atr < floor:
        return FilterResult(False, f"dead market (ATR {cur_atr:.5g} < p{s['dead_market_percentile']:.0f} {floor:.5g})", cur_atr)
    last_range = h[-1] - l[-1]
    if last_range > s["spike_atr_mult"] * cur_atr:
        return FilterResult(False, f"news spike (candle {last_range / cur_atr:.1f}x ATR)", cur_atr)
    if not in_trading_session(inst, now, s["session_filter"]):
        return FilterResult(False, "outside London/NY session", cur_atr)
    return FilterResult(True, "", cur_atr)


def trend_pullback(inst: Instrument, candles: list[dict], cfg: dict, atr: float) -> Signal | None:
    s = cfg["signals"]
    closes = [c["c"] for c in candles]
    fast = ind.ema(closes, s["ema_fast"])
    slow = ind.ema(closes, s["ema_slow"])
    rsi = ind.rsi(closes, s["rsi_period"])
    if None in (fast[-1], slow[-1], rsi[-1], rsi[-2], rsi[-3]):
        return None
    last, prev = candles[-1], candles[-2]
    touch_band = 0.25 * atr                     # "pulls back to" = within a quarter ATR
    # long: uptrend, a recent low touched the fast EMA, RSI turned up, closed back above fast EMA
    if closes[-1] > slow[-1] and fast[-1] > slow[-1]:
        touched = min(last["l"], prev["l"]) <= fast[-1] + touch_band
        rsi_turn = rsi[-2] <= rsi[-3] and rsi[-1] > rsi[-2] and rsi[-1] < 70
        if touched and rsi_turn and closes[-1] > fast[-1]:
            return Signal(inst.symbol, "LONG", "trend_pullback", closes[-1], atr, last["t"],
                          f"Uptrend (close>{s['ema_slow']}EMA), pulled back to {s['ema_fast']}EMA, RSI turned up to {rsi[-1]:.0f}",
                          {"ema_fast": fast[-1], "ema_slow": slow[-1], "rsi": rsi[-1]})
    if closes[-1] < slow[-1] and fast[-1] < slow[-1]:
        touched = max(last["h"], prev["h"]) >= fast[-1] - touch_band
        rsi_turn = rsi[-2] >= rsi[-3] and rsi[-1] < rsi[-2] and rsi[-1] > 30
        if touched and rsi_turn and closes[-1] < fast[-1]:
            return Signal(inst.symbol, "SHORT", "trend_pullback", closes[-1], atr, last["t"],
                          f"Downtrend (close<{s['ema_slow']}EMA), pulled back to {s['ema_fast']}EMA, RSI turned down to {rsi[-1]:.0f}",
                          {"ema_fast": fast[-1], "ema_slow": slow[-1], "rsi": rsi[-1]})
    return None


def session_breakout(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                     now: datetime) -> Signal | None:
    if not is_forex_like(inst):
        return None
    start, end = asian_range_bounds(now)
    if now < end:
        return None
    st, et = int(start.timestamp()), int(end.timestamp())
    asian = [c for c in candles if st <= c["t"] < et]
    if len(asian) < 0.7 * (et - st) / _tf(cfg):   # need most of the Asian session's candles
        return None
    hi, lo = max(c["h"] for c in asian), min(c["l"] for c in asian)
    after = [c for c in candles if c["t"] >= et]
    if not after:
        return None
    last = after[-1]
    earlier = after[:-1]
    # only the FIRST close beyond the range counts
    if last["c"] > hi and all(c["c"] <= hi for c in earlier):
        return Signal(inst.symbol, "LONG", "session_breakout", last["c"], atr, last["t"],
                      f"Closed above Asian high {hi:.{inst.decimals}f}", {"asian_high": hi, "asian_low": lo})
    if last["c"] < lo and all(c["c"] >= lo for c in earlier):
        return Signal(inst.symbol, "SHORT", "session_breakout", last["c"], atr, last["t"],
                      f"Closed below Asian low {lo:.{inst.decimals}f}", {"asian_high": hi, "asian_low": lo})
    return None


def news_momentum(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                  news_bias: dict | None) -> Signal | None:
    if not news_bias:
        return None
    bias = float(news_bias.get("bias", 0))
    if abs(bias) < cfg["news"]["bias_threshold"]:
        return None
    s = cfg["signals"]
    closes = [c["c"] for c in candles]
    fast = ind.ema(closes, s["ema_fast"])
    slow = ind.ema(closes, s["ema_slow"])
    if fast[-1] is None or slow[-1] is None:
        return None
    last = candles[-1]
    summary = news_bias.get("summary", "")
    if bias > 0 and closes[-1] > fast[-1] > slow[-1]:
        return Signal(inst.symbol, "LONG", "news_momentum", closes[-1], atr, last["t"],
                      f"Bullish news bias {bias:+.2f} confirmed by price: {summary}"[:300], {"bias": bias})
    if bias < 0 and closes[-1] < fast[-1] < slow[-1]:
        return Signal(inst.symbol, "SHORT", "news_momentum", closes[-1], atr, last["t"],
                      f"Bearish news bias {bias:+.2f} confirmed by price: {summary}"[:300], {"bias": bias})
    return None


def evaluate(inst: Instrument, candles: list[dict], cfg: dict, now: datetime | None = None,
             news_bias: dict | None = None, disabled: set[str] | None = None,
             atr_floor: float | None = None) -> tuple[list[Signal], str]:
    """Return (signals, filter_reason). filter_reason is '' when filters passed."""
    now = now or datetime.now(timezone.utc)
    disabled = disabled or set()
    f = check_filters(inst, candles, cfg, now, atr_floor)
    if not f.ok:
        return [], f.reason
    s = cfg["signals"]
    out: list[Signal] = []
    if s["trend_pullback"] and "trend_pullback" not in disabled:
        sig = trend_pullback(inst, candles, cfg, f.atr)
        if sig:
            out.append(sig)
    if s["session_breakout"] and "session_breakout" not in disabled:
        sig = session_breakout(inst, candles, cfg, f.atr, now)
        if sig:
            out.append(sig)
    if s["news_momentum"] and cfg["news"]["enabled"] and "news_momentum" not in disabled:
        sig = news_momentum(inst, candles, cfg, f.atr, news_bias)
        if sig:
            out.append(sig)
    # if two signals disagree on direction, take neither
    if len({x.side for x in out}) > 1:
        return [], "conflicting signals"
    return out[:1], ""
