"""Smart Money Concepts, coded as objective rules (no hand-drawn judgement).

From the account's own candles (M5/M15) this finds:
* key levels - previous day high/low (PDH/PDL), today's completed session highs/lows
  (Asian 00-07, London 07-12, New York 12-17 UTC), and liquidity pools: unswept swing
  highs/lows and equal highs/lows (two swings within 0.1 ATR - stops cluster there);
* whether each level has been SWEPT (price traded through it after it formed);
* fair value gaps (FVG) - 3-candle imbalance: candle 1's high below candle 3's low
  (bullish) or the mirror; dropped once fully filled;
* order blocks (OB) - the last opposite candle before a displacement candle (body >= 1 ATR)
  that breaks the last 10 candles' high/low; dropped once price closes through them.

Used three ways:
1. `liquidity_sweep` strategy (signals.py): a session/day level or liquidity pool is swept
   and the candle closes back inside with a rejection wick -> trade the reversal, stop
   beyond the sweep wick, target the opposite liquidity.
2. Location filter: don't buy straight into an unswept high/bearish OB (or sell into a
   low/bullish OB) that sits closer than 1x the stop distance.
3. Context for Haiku/Sonnet and lines/zones on the dashboard chart.

All times are UTC. Day = UTC day; for "previous day" we skip days with little data
(weekends), so on Monday PDH/PDL are Friday's.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

DAY = 86400
SESSIONS = (("Asian", 0, 7), ("London", 7, 12), ("New York", 12, 17))
SWING_N = 3                 # fractal: higher than 3 candles each side
SWING_LOOKBACK_H = 72       # liquidity pools from the last 3 days
EQUAL_TOL_ATR = 0.1
ZONE_LOOKBACK = 150         # candles scanned for FVG / OB
MIN_FVG_ATR = 0.15
OB_BODY_ATR = 1.0


@dataclass
class Level:
    name: str               # "PDH", "Asian low", "equal highs", "swing high" ...
    price: float
    kind: str               # high | low
    formed_t: int           # sweeps are counted from this candle time on
    swept: bool = False
    swept_t: int | None = None

    def short(self, decimals: int) -> str:
        return f"{self.name} {self.price:.{decimals}f}{' (swept)' if self.swept else ''}"


@dataclass
class Zone:
    kind: str               # fvg | ob
    side: str               # bull | bear
    top: float
    bottom: float
    t: int                  # candle time where the zone starts
    touched: bool = False   # price has come back into it
    tf: str = ""            # "H1" for zones found on hourly candles (chart only)


def _tf_seconds(candles: list[dict]) -> int:
    if len(candles) < 2:
        return 900
    return min(candles[i + 1]["t"] - candles[i]["t"] for i in range(min(10, len(candles) - 1))) or 900


def _mark_swept(level: Level, candles: list[dict]) -> Level:
    for c in candles:
        if c["t"] < level.formed_t:
            continue
        if (level.kind == "high" and c["h"] > level.price) or (level.kind == "low" and c["l"] < level.price):
            level.swept, level.swept_t = True, c["t"]
            break
    return level


def _prev_day(candles: list[dict], today0: int, tf: int) -> list[dict]:
    """The latest UTC day before today with at least 25% of its candles (skips weekends)."""
    for back in range(1, 5):
        d0 = today0 - back * DAY
        rows = [c for c in candles if d0 <= c["t"] < d0 + DAY]
        if len(rows) >= 0.25 * DAY / tf:
            return rows
    return []


def _swings(candles: list[dict]) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs, lows = [], []
    n = SWING_N
    for i in range(n, len(candles) - n):
        h, l = candles[i]["h"], candles[i]["l"]
        if all(h > candles[j]["h"] for j in range(i - n, i + n + 1) if j != i):
            highs.append((candles[i]["t"], h))
        if all(l < candles[j]["l"] for j in range(i - n, i + n + 1) if j != i):
            lows.append((candles[i]["t"], l))
    return highs, lows


def key_levels(candles: list[dict], atr: float) -> list[Level]:
    """Day, session and liquidity-pool levels, each marked swept / unswept."""
    if not candles:
        return []
    tf = _tf_seconds(candles)
    last_t = candles[-1]["t"]
    today0 = last_t - last_t % DAY
    out: list[Level] = []

    prev = _prev_day(candles, today0, tf)
    if prev:
        out.append(Level("PDH", max(c["h"] for c in prev), "high", today0))
        out.append(Level("PDL", min(c["l"] for c in prev), "low", today0))

    for name, h0, h1 in SESSIONS:
        s0, s1 = today0 + h0 * 3600, today0 + h1 * 3600
        if last_t + tf < s1:            # session not finished yet -> not a level
            continue
        rows = [c for c in candles if s0 <= c["t"] < s1]
        if len(rows) < 0.5 * (s1 - s0) / tf:
            continue
        out.append(Level(f"{name} high", max(c["h"] for c in rows), "high", s1))
        out.append(Level(f"{name} low", min(c["l"] for c in rows), "low", s1))

    # liquidity pools: swing points from the last 3 days that nothing has traded through yet
    # (judged on the candles BEFORE the last one, so a sweep on the last candle still shows as a sweep)
    recent = [c for c in candles if c["t"] >= last_t - SWING_LOOKBACK_H * 3600]
    highs, lows = _swings(recent)
    for kind, pts in (("high", highs), ("low", lows)):
        pools: list[Level] = []
        for t, p in pts:
            lv = _mark_swept(Level(f"swing {kind}", p, kind, t + tf), recent[:-1])
            if not lv.swept:
                pools.append(lv)
        # equal highs/lows: unswept swings within 0.1 ATR of each other form one stronger pool
        pools.sort(key=lambda x: x.price)
        clusters: list[list[Level]] = []
        for lv in pools:
            if clusters and lv.price - clusters[-1][-1].price <= EQUAL_TOL_ATR * atr:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        singles = []
        for cl in clusters:
            if len(cl) >= 2:
                p = max(x.price for x in cl) if kind == "high" else min(x.price for x in cl)
                out.append(Level(f"equal {kind}s", p, kind, max(x.formed_t for x in cl)))
            else:
                singles.append(cl[0])
        out.extend(sorted(singles, key=lambda x: x.formed_t)[-3:])

    return [_mark_swept(lv, candles) for lv in out]


def zones(candles: list[dict], atr: float) -> list[Zone]:
    """Unfilled FVGs and unbroken order blocks from the last ~150 candles (newest last)."""
    rows = candles[-ZONE_LOOKBACK:]
    out: list[Zone] = []
    for i in range(2, len(rows)):
        a, b, c = rows[i - 2], rows[i - 1], rows[i]
        if c["l"] - a["h"] >= MIN_FVG_ATR * atr:
            out.append(Zone("fvg", "bull", c["l"], a["h"], b["t"]))
        elif a["l"] - c["h"] >= MIN_FVG_ATR * atr:
            out.append(Zone("fvg", "bear", a["l"], c["h"], b["t"]))
        body = c["c"] - c["o"]
        prior = rows[max(0, i - 10):i]
        if prior and abs(body) >= OB_BODY_ATR * atr:
            if body > 0 and c["c"] > max(x["h"] for x in prior):
                ob = next((x for x in reversed(rows[max(0, i - 3):i]) if x["c"] < x["o"]), None)
                if ob:
                    out.append(Zone("ob", "bull", ob["h"], ob["l"], ob["t"]))
            elif body < 0 and c["c"] < min(x["l"] for x in prior):
                ob = next((x for x in reversed(rows[max(0, i - 3):i]) if x["c"] > x["o"]), None)
                if ob:
                    out.append(Zone("ob", "bear", ob["h"], ob["l"], ob["t"]))

    live: list[Zone] = []
    for z in out:
        # for an FVG, z.t is the middle candle; skip candle 3 too (it formed the gap)
        later = [x for x in rows if x["t"] > z.t + (_tf_seconds(rows) if z.kind == "fvg" else 0)]
        if z.kind == "fvg":
            if z.side == "bull":
                if any(x["l"] <= z.bottom for x in later):
                    continue                       # fully filled
                z.touched = any(x["l"] <= z.top for x in later)
            else:
                if any(x["h"] >= z.top for x in later):
                    continue
                z.touched = any(x["h"] >= z.bottom for x in later)
        else:
            if z.side == "bull":
                if any(x["c"] < z.bottom for x in later):
                    continue                       # broken
                z.touched = any(x["l"] <= z.top for x in later[3:])
            else:
                if any(x["c"] > z.top for x in later):
                    continue
                z.touched = any(x["h"] >= z.bottom for x in later[3:])
        live.append(z)
    # keep the 3 most recent of each kind/side
    keep: list[Zone] = []
    for kind in ("fvg", "ob"):
        for side in ("bull", "bear"):
            keep.extend([z for z in live if z.kind == kind and z.side == side][-3:])
    return keep


# ------------------------------------------------------------------ uses
def opposing_obstacle(side: str, price: float, levels: list[Level], zs: list[Zone]) -> tuple[str, float] | None:
    """Nearest unswept liquidity / untouched opposite OB in the trade's way: (name, price)."""
    if side == "LONG":
        cands = [(lv.name, lv.price) for lv in levels if lv.kind == "high" and not lv.swept and lv.price > price]
        cands += [("bearish OB", z.bottom) for z in zs if z.kind == "ob" and z.side == "bear" and z.bottom > price]
        return min(cands, key=lambda x: x[1]) if cands else None
    cands = [(lv.name, lv.price) for lv in levels if lv.kind == "low" and not lv.swept and lv.price < price]
    cands += [("bullish OB", z.top) for z in zs if z.kind == "ob" and z.side == "bull" and z.top < price]
    return max(cands, key=lambda x: x[1]) if cands else None


def liquidity_target(side: str, price: float, min_dist: float, levels: list[Level]) -> Level | None:
    """Nearest unswept opposite liquidity at least `min_dist` away (the sweep trade's target)."""
    if side == "LONG":
        c = [lv for lv in levels if lv.kind == "high" and not lv.swept and lv.price - price >= min_dist]
        return min(c, key=lambda x: x.price) if c else None
    c = [lv for lv in levels if lv.kind == "low" and not lv.swept and price - lv.price >= min_dist]
    return max(c, key=lambda x: x.price) if c else None


def location_check(side: str, price: float, stop_dist: float, levels: list[Level], zs: list[Zone],
                   decimals: int) -> str | None:
    """Block a trade whose path is blocked by liquidity/OB closer than 1x the stop distance."""
    ob = opposing_obstacle(side, price, levels, zs)
    if ob and abs(ob[1] - price) < stop_dist:
        where = "above" if side == "LONG" else "below"
        return f"{ob[0]} {ob[1]:.{decimals}f} is right {where} (closer than the stop distance)"
    return None


def context_text(candles: list[dict], atr: float, decimals: int) -> str:
    """Compact SMC map for the AI prompt (~150 tokens)."""
    if not candles or not atr:
        return "n/a"
    price = candles[-1]["c"]
    levels = key_levels(candles, atr)
    zs = zones(candles, atr)

    def dist(p: float) -> str:
        return f"{(p - price) / atr:+.1f} ATR"

    lines = []
    for lv in sorted(levels, key=lambda x: abs(x.price - price))[:8]:
        lines.append(f"- {lv.short(decimals)} ({dist(lv.price)})")
    for z in sorted(zs, key=lambda z: abs((z.top + z.bottom) / 2 - price))[:4]:
        lines.append(f"- {z.side}ish {z.kind.upper()} {z.bottom:.{decimals}f}-{z.top:.{decimals}f} "
                     f"({dist((z.top + z.bottom) / 2)}{', touched' if z.touched else ', fresh'})")
    return "\n".join(lines) if lines else "no key levels found"


def hourly(candles: list[dict]) -> list[dict]:
    """Complete H1 candles built from M5/M15 candles."""
    if len(candles) < 2:
        return []
    per_hour = 3600 // max(60, candles[-1]["t"] - candles[-2]["t"])
    hours: dict[int, list[dict]] = {}
    for c in candles:
        hours.setdefault(c["t"] // 3600, []).append(c)
    return [{"t": h * 3600, "o": rows[0]["o"], "h": max(x["h"] for x in rows), "l": min(x["l"] for x in rows),
             "c": rows[-1]["c"], "v": 0} for h, rows in sorted(hours.items()) if len(rows) >= per_hour]


def to_json(candles: list[dict], atr: float) -> dict:
    """For the dashboard chart: levels, chart-timeframe zones and H1 zones (bigger, more important)."""
    out = [asdict(z) for z in zones(candles, atr)]
    h1 = hourly(candles)
    if len(h1) > 20:
        trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(h1[-15:], h1[-14:])]
        h1_atr = sum(trs) / len(trs)
        for z in zones(h1, h1_atr):
            z.tf = "H1"
            out.append(asdict(z))
    return {"levels": [asdict(lv) for lv in key_levels(candles, atr)], "zones": out}
