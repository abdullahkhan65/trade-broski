"""Free, coded signal engine. Runs on CLOSED candles (M15, or M5 for the scalper).

Signals:
* trend_pullback  - price on the trend side of the slow EMA pulls back to the fast EMA
                    and RSI turns back in the trend direction.
* session_breakout - (gold/forex) a close beyond the Asian-session high/low during
                    London or New York hours.
* news_momentum   - the AI news digest has a strong bias for the instrument and
                    price confirms it (close beyond fast EMA, fast EMA beyond slow EMA).
* range_reversion - (ranging market) price poked outside a Bollinger band with stretched
                    RSI and closed back inside: fade it back towards the middle.
* momentum_breakout - (trending/volatile market) a strong candle closes beyond the
                    highest high / lowest low of the last 20 candles, with the trend.

Market regime (detect_regime) labels every candle as trend / range / volatile /
mixed from ADX and ATR. Each signal is tagged with it, and the adaptive layer
(adaptive.py) learns which strategy works in which regime for each account.

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
    regime: str = ""


@dataclass
class Regime:
    name: str                 # trend | range | volatile | mixed
    adx: float
    atr_pct: float            # where current ATR sits in its ~20-day range (0-100)

    def __str__(self) -> str:
        return f"{self.name} (ADX {self.adx:.0f}, volatility p{self.atr_pct:.0f})"


def detect_regime(candles: list[dict], cfg: dict) -> Regime | None:
    s = cfg["signals"]
    if len(candles) < 60:
        return None
    h = [c["h"] for c in candles]
    l = [c["l"] for c in candles]
    c = [c["c"] for c in candles]
    a = ind.adx(h, l, c, 14)[-1]
    atr_series = ind.atr(h, l, c, s["atr_period"])
    window = sorted(x for x in atr_series[-(20 * 86400 // _tf(cfg)):] if x)
    cur = atr_series[-1]
    if a is None or not cur or not window:
        return None
    below = sum(1 for x in window if x < cur)
    equal = sum(1 for x in window if x == cur)
    pct = 100 * (below + equal / 2) / len(window)      # mid-rank, so a flat ATR sits at p50
    if pct >= 90:
        name = "volatile"
    elif a >= 25:
        name = "trend"
    elif a <= 20:
        name = "range"
    else:
        name = "mixed"
    return Regime(name, a, pct)


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


def range_reversion(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                    regime: Regime | None) -> Signal | None:
    if not regime or regime.name not in ("range", "mixed"):
        return None
    s = cfg["signals"]
    closes = [c["c"] for c in candles]
    mid, up, lo = ind.bollinger(closes, 20, 2.0)
    rsi = ind.rsi(closes, s["rsi_period"])
    if None in (mid[-1], up[-2], lo[-2], rsi[-2]):
        return None
    last, prev = candles[-1], candles[-2]
    if (prev["l"] < lo[-2] or prev["c"] < lo[-2]) and rsi[-2] < 35 and last["c"] > lo[-1] and last["c"] > last["o"]:
        return Signal(inst.symbol, "LONG", "range_reversion", last["c"], atr, last["t"],
                      f"Ranging market ({regime}); dipped below lower Bollinger band with RSI {rsi[-2]:.0f} "
                      f"and closed back inside - fade towards the middle {mid[-1]:.{inst.decimals}f}",
                      {"bb_mid": mid[-1], "rsi": rsi[-2]})
    if (prev["h"] > up[-2] or prev["c"] > up[-2]) and rsi[-2] > 65 and last["c"] < up[-1] and last["c"] < last["o"]:
        return Signal(inst.symbol, "SHORT", "range_reversion", last["c"], atr, last["t"],
                      f"Ranging market ({regime}); spiked above upper Bollinger band with RSI {rsi[-2]:.0f} "
                      f"and closed back inside - fade towards the middle {mid[-1]:.{inst.decimals}f}",
                      {"bb_mid": mid[-1], "rsi": rsi[-2]})
    return None


def momentum_breakout(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                      regime: Regime | None) -> Signal | None:
    if not regime or regime.name not in ("trend", "volatile"):
        return None
    s = cfg["signals"]
    if len(candles) < 22:
        return None
    closes = [c["c"] for c in candles]
    slow = ind.ema(closes, s["ema_slow"])
    if slow[-1] is None:
        return None
    last = candles[-1]
    prior = candles[-21:-1]
    hi, lo = max(c["h"] for c in prior), min(c["l"] for c in prior)
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"] or 1e-12
    strong = body >= 0.6 * atr
    if last["c"] > hi and strong and (last["c"] - last["l"]) / rng >= 0.7 and last["c"] > slow[-1]:
        return Signal(inst.symbol, "LONG", "momentum_breakout", last["c"], atr, last["t"],
                      f"{regime.name.capitalize()} market ({regime}); strong candle closed above the "
                      f"20-candle high {hi:.{inst.decimals}f}", {"high20": hi})
    if last["c"] < lo and strong and (last["h"] - last["c"]) / rng >= 0.7 and last["c"] < slow[-1]:
        return Signal(inst.symbol, "SHORT", "momentum_breakout", last["c"], atr, last["t"],
                      f"{regime.name.capitalize()} market ({regime}); strong candle closed below the "
                      f"20-candle low {lo:.{inst.decimals}f}", {"low20": lo})
    return None


def evaluate(inst: Instrument, candles: list[dict], cfg: dict, now: datetime | None = None,
             news_bias: dict | None = None, disabled: set[str] | None = None,
             atr_floor: float | None = None) -> tuple[list[Signal], str]:
    """Return (signals, filter_reason). filter_reason is '' when filters passed.
    Several signals can come back (same direction); the engine picks the one the
    adaptive layer rates best."""
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
    regime = detect_regime(candles, cfg)
    if s.get("range_reversion") and "range_reversion" not in disabled:
        sig = range_reversion(inst, candles, cfg, f.atr, regime)
        if sig:
            out.append(sig)
    if s.get("momentum_breakout") and "momentum_breakout" not in disabled:
        sig = momentum_breakout(inst, candles, cfg, f.atr, regime)
        if sig:
            out.append(sig)
    for sig in out:
        sig.regime = regime.name if regime else "unknown"
    # if two signals disagree on direction, take neither
    if len({x.side for x in out}) > 1:
        return [], "conflicting signals"
    return out, ""
