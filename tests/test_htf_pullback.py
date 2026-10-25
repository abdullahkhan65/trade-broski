"""H1 trend pullback: long only into a fresh bullish H1 zone in an H1 up-trend, closing back out."""
from app import smc
from app.instruments import INSTRUMENTS
from app.settings_store import DEFAULTS, cfg_for, validate
from app.signals import htf_pullback, htf_trend

M15 = 900


def uptrend_with_gap():
    rows, p, t = [], 100.0, 1_000_000 * 3600
    for k in range(360):                      # 90 hours of a steady rise
        step = 3.0 if k == 200 else 0.1       # one jump candle -> an H1 fair value gap
        o, c = p, p + step
        rows.append({"t": t + k * M15, "o": o, "h": c + 0.05, "l": o - 0.05, "c": c, "v": 0})
        p = c
    return rows


def test_long_on_pullback_into_fresh_h1_gap():
    cfg = cfg_for(validate(DEFAULTS), "moderate")
    rows = uptrend_with_gap()
    assert htf_trend(rows, cfg) == "up"
    h1 = smc.hourly(rows)
    gaps = [z for z in smc.zones(h1, 0.5) if z.kind == "fvg" and z.side == "bull" and not z.touched]
    assert gaps
    z = gaps[-1]
    t = rows[-1]["t"] + M15
    t += 3600 - t % 3600                       # first candle of a new hour
    dip = {"t": t, "o": z.top + 0.3, "h": z.top + 0.6, "l": (z.top + z.bottom) / 2, "c": z.top + 0.4, "v": 0}
    sig = htf_pullback(INSTRUMENTS["XAU_USD"], rows + [dip], cfg, 0.4)
    assert sig and sig.side == "LONG" and sig.type == "htf_pullback" and "H1" in sig.note
    closes_inside = dict(dip, c=(z.top + z.bottom) / 2, o=z.top + 0.1)
    assert htf_pullback(INSTRUMENTS["XAU_USD"], rows + [closes_inside], cfg, 0.4) is None


def test_no_signal_without_a_trend():
    cfg = cfg_for(validate(DEFAULTS), "moderate")
    flat = [{"t": 3600 * 1000 + k * M15, "o": 100, "h": 100.2, "l": 99.8, "c": 100 + (0.1 if k % 2 else -0.1), "v": 0}
            for k in range(400)]
    assert htf_pullback(INSTRUMENTS["XAU_USD"], flat, cfg, 0.4) is None
