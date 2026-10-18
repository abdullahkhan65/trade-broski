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
* london_orb / ny_orb - opening range breakout: the first close beyond the high/low of the
                    first hour of London (08:00-09:00 London) or of the New York equity
                    open (09:30-10:30 NY), within 3 hours. Stop at the middle of the range.
* pdh_breakout    - first close of the day beyond the previous day's high/low with a strong
                    body (>= 0.5 ATR). Stop back inside the level.
* liquidity_sweep - (SMC) a previous-day / session high or low, or equal highs/lows, is
                    swept for the first time and the candle closes back inside with a
                    rejection wick. Stop beyond the sweep wick, target the opposite liquidity.

SMC location filter (smc.py): drop a signal whose path is blocked by unswept liquidity
or an opposite order block closer than 1x the stop distance.

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
from . import smc
from .instruments import Instrument
from .sessions import LONDON, NY, asian_range_bounds, in_trading_session, is_forex_like

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
    stop_atr: float | None = None      # structure-based stop / target (x ATR), if the strategy has one
    target_atr: float | None = None


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


ORB_SESSIONS = {"london_orb": (LONDON, 8, 0), "ny_orb": (NY, 9, 30)}
ORB_MINUTES = 60
ORB_WINDOW_H = 3                # the breakout must come within 3 hours of the range closing
ORB_MIN_ATR, ORB_MAX_ATR = 0.8, 4.0


def opening_range_breakout(inst: Instrument, candles: list[dict], cfg: dict, atr: float, now: datetime,
                           kind: str) -> Signal | None:
    tz, hh, mm = ORB_SESSIONS[kind]
    local = now.astimezone(tz)
    start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    st, et = int(start.timestamp()), int(start.timestamp()) + ORB_MINUTES * 60
    last = candles[-1]
    if not (et <= last["t"] < et + ORB_WINDOW_H * 3600):
        return None
    rng = [c for c in candles if st <= c["t"] < et]
    if len(rng) < 0.8 * ORB_MINUTES * 60 / _tf(cfg):
        return None
    hi, lo = max(c["h"] for c in rng), min(c["l"] for c in rng)
    if not (ORB_MIN_ATR * atr <= hi - lo <= ORB_MAX_ATR * atr):
        return None                       # too tight (noise) or too wide (move already done)
    after = [c for c in candles if et <= c["t"] <= last["t"]]
    mid = (hi + lo) / 2
    d = inst.decimals
    name = "London" if kind == "london_orb" else "New York"
    e = cfg["exits"]
    if last["c"] > hi and all(c["c"] <= hi for c in after[:-1]):
        side, stop_atr = "LONG", (last["c"] - mid) / atr
    elif last["c"] < lo and all(c["c"] >= lo for c in after[:-1]):
        side, stop_atr = "SHORT", (mid - last["c"]) / atr
    else:
        return None
    if stop_atr > e["max_stop_atr"]:
        stop_atr = e["max_stop_atr"]
    return Signal(inst.symbol, side, kind, last["c"], atr, last["t"],
                  f"First close {'above' if side == 'LONG' else 'below'} the {name} opening range "
                  f"{lo:.{d}f}-{hi:.{d}f}", {"orb_high": hi, "orb_low": lo},
                  stop_atr=round(max(stop_atr, e["min_stop_atr"]), 2))


PDH_BODY_ATR = 0.5
PDH_STOP_PAD_ATR = 0.3


def pdh_breakout(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                 levels: list[smc.Level]) -> Signal | None:
    last = candles[-1]
    body = last["c"] - last["o"]
    if abs(body) < PDH_BODY_ATR * atr:
        return None
    today0 = last["t"] - last["t"] % 86400
    today = [c for c in candles if c["t"] >= today0]
    e = cfg["exits"]
    d = inst.decimals
    for lv in levels:
        if lv.name == "PDH" and body > 0 and last["c"] > lv.price and all(c["c"] <= lv.price for c in today[:-1]):
            stop_atr = (last["c"] - (lv.price - PDH_STOP_PAD_ATR * atr)) / atr
            side = "LONG"
        elif lv.name == "PDL" and body < 0 and last["c"] < lv.price and all(c["c"] >= lv.price for c in today[:-1]):
            stop_atr = ((lv.price + PDH_STOP_PAD_ATR * atr) - last["c"]) / atr
            side = "SHORT"
        else:
            continue
        stop_atr = min(max(stop_atr, e["min_stop_atr"]), e["max_stop_atr"])
        return Signal(inst.symbol, side, "pdh_breakout", last["c"], atr, last["t"],
                      f"First close of the day {'above PDH' if side == 'LONG' else 'below PDL'} "
                      f"{lv.price:.{d}f} with a strong candle", {"level": lv.name}, stop_atr=round(stop_atr, 2))
    return None


SWEEP_LEVELS = ("PDH", "PDL", "Asian", "London", "New York", "equal")
SWEEP_MAX_DEPTH_ATR = 1.0      # deeper than this is a breakout, not a sweep
SWEEP_CLOSE_SHARE = 0.6        # close in the top 40% (long) / bottom 40% (short) of the candle
SWEEP_STOP_PAD_ATR = 0.1


def liquidity_sweep(inst: Instrument, candles: list[dict], cfg: dict, atr: float,
                    levels: list[smc.Level]) -> Signal | None:
    last = candles[-1]
    rng = last["h"] - last["l"]
    if rng <= 0:
        return None
    e = cfg["exits"]
    d = inst.decimals
    for lv in levels:
        if not lv.name.startswith(SWEEP_LEVELS) or lv.swept_t != last["t"]:
            continue                       # only the FIRST sweep of a strong level, on this candle
        if lv.kind == "low":
            side, ok = "LONG", last["c"] > lv.price and (last["c"] - last["l"]) / rng >= SWEEP_CLOSE_SHARE
            depth, stop = lv.price - last["l"], last["l"] - SWEEP_STOP_PAD_ATR * atr
        else:
            side, ok = "SHORT", last["c"] < lv.price and (last["h"] - last["c"]) / rng >= SWEEP_CLOSE_SHARE
            depth, stop = last["h"] - lv.price, last["h"] + SWEEP_STOP_PAD_ATR * atr
        if not ok or depth > SWEEP_MAX_DEPTH_ATR * atr:
            continue
        stop_atr = abs(last["c"] - stop) / atr
        if stop_atr > e["max_stop_atr"]:
            continue                       # structure stop too wide for this account
        stop_atr = max(stop_atr, e["min_stop_atr"])
        tgt = smc.liquidity_target(side, last["c"], stop_atr * e["min_reward_risk"] * atr,
                                   [x for x in levels if x is not lv])
        target_atr = abs(tgt.price - last["c"]) / atr if tgt else None
        note = (f"Swept {lv.name} {lv.price:.{d}f} and closed back {'above' if side == 'LONG' else 'below'}"
                + (f"; target {tgt.name} {tgt.price:.{d}f}" if tgt else ""))
        return Signal(inst.symbol, side, "liquidity_sweep", last["c"], atr, last["t"], note,
                      {"level": lv.name, "level_price": lv.price}, stop_atr=round(stop_atr, 2),
                      target_atr=round(target_atr, 2) if target_atr else None)
    return None


H1_MIN_BARS = 55


def htf_trend(candles: list[dict], cfg: dict) -> str | None:
    """H1 trend built from the M5/M15 candles: 'up' (close > EMA50 and EMA20 > EMA50),
    'down' (the opposite) or 'flat'. None if there isn't enough history to tell."""
    per_hour = 3600 // _tf(cfg)
    hours: dict[int, list[dict]] = {}
    for c in candles:
        hours.setdefault(c["t"] // 3600, []).append(c)
    closes = [rows[-1]["c"] for _, rows in sorted(hours.items()) if len(rows) >= per_hour]  # complete hours only
    if len(closes) < H1_MIN_BARS:
        return None
    fast = ind.ema(closes, cfg["signals"]["ema_fast"])[-1]
    slow = ind.ema(closes, cfg["signals"]["ema_slow"])[-1]
    if fast is None or slow is None:
        return None
    if closes[-1] > slow and fast > slow:
        return "up"
    if closes[-1] < slow and fast < slow:
        return "down"
    return "flat"


def allowed(allowlist: list[str], symbol: str, strategy: str) -> bool:
    """'XAU_USD:session_breakout', 'XAU_USD:*', '*:ny_orb' or '*:*'."""
    for item in allowlist:
        sym, _, strat = item.partition(":")
        if sym.strip() in ("*", symbol) and strat.strip() in ("*", strategy):
            return True
    return False


def evaluate(inst: Instrument, candles: list[dict], cfg: dict, now: datetime | None = None,
             news_bias: dict | None = None, disabled: set[str] | None = None,
             atr_floor: float | None = None, rejected: list | None = None) -> tuple[list[Signal], str]:
    """Return (signals, filter_reason). filter_reason is '' when filters passed.
    Several signals can come back (same direction); the engine picks the one the
    adaptive layer rates best. Signals dropped by the H1-trend or SMC filter are appended to
    `rejected` as (signal, reason) so the engine can log them and track their shadow outcome."""
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
    levels = (smc.key_levels(candles, f.atr)
              if (s.get("liquidity_sweep") or s.get("smc_filter") or s.get("pdh_breakout")) else [])
    for kind in ("london_orb", "ny_orb"):
        if s.get(kind) and kind not in disabled:
            sig = opening_range_breakout(inst, candles, cfg, f.atr, now, kind)
            if sig:
                out.append(sig)
    if s.get("pdh_breakout") and "pdh_breakout" not in disabled:
        sig = pdh_breakout(inst, candles, cfg, f.atr, levels)
        if sig:
            out.append(sig)
    if s.get("liquidity_sweep") and "liquidity_sweep" not in disabled:
        sig = liquidity_sweep(inst, candles, cfg, f.atr, levels)
        if sig:
            out.append(sig)
    for sig in out:
        sig.regime = regime.name if regime else "unknown"
    allow = s.get("allowlist") or []
    if allow and out:
        out = [x for x in out if allowed(allow, inst.symbol, x.type)]
        if not out:
            return [], "strategy not on this account's allowlist for this symbol"
    # if two signals disagree on direction, take neither
    if len({x.side for x in out}) > 1:
        return [], "conflicting signals"
    if out and s.get("htf_trend_filter"):
        trend = htf_trend(candles, cfg)
        if trend is not None:
            want = {"up": "LONG", "down": "SHORT"}.get(trend)
            if out[0].side != want:
                why = f"{out[0].side.lower()} signal against the H1 trend ({trend})"
                if rejected is not None:
                    rejected.extend((x, why) for x in out)
                return [], why
            for sig in out:
                sig.note += f"; with the H1 trend ({trend})"
    if out and s.get("smc_filter"):
        zs = smc.zones(candles, f.atr)
        e = cfg["exits"]
        kept, why = [], ""
        for sig in out:
            stop_dist = (sig.stop_atr or min(max(e["default_stop_atr"], e["min_stop_atr"]), e["max_stop_atr"])) * f.atr
            why = smc.location_check(sig.side, sig.price, stop_dist, levels, zs, inst.decimals) or ""
            if not why:
                kept.append(sig)
            elif rejected is not None:
                rejected.append((sig, f"SMC: {why}"))
        if not kept:
            return [], f"SMC: {why}"
        out = kept
    return out, ""
