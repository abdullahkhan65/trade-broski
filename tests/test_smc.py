"""SMC levels, zones, the liquidity-sweep strategy and the location filter."""
from app import smc
from app.instruments import INSTRUMENTS
from app.settings_store import DEFAULTS, cfg_for, validate
from app.signals import liquidity_sweep

DAY0 = 1_792_540_800 - 1_792_540_800 % 86400      # a UTC midnight (Tuesday 2026-10-20)
M15 = 900


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0}


def flat_day(d0, lo=100.0, hi=101.0, n=96):
    """A quiet day oscillating between lo and hi (highs/lows only at the two ends)."""
    out = []
    for i in range(n):
        mid = lo + (hi - lo) * (0.5 + 0.3 * ((i % 8) / 8 - 0.5))
        out.append(bar(d0 + i * M15, mid, mid + 0.1, mid - 0.1, mid))
    out[10] = bar(d0 + 10 * M15, 100.5, hi, 100.4, 100.6)     # day high
    out[50] = bar(d0 + 50 * M15, 100.5, 100.6, lo, 100.4)     # day low
    return out


def cfg(mode="moderate"):
    return cfg_for(validate(DEFAULTS), mode)


def test_pdh_pdl_and_sessions():
    prev = flat_day(DAY0 - 86400)
    today = [bar(DAY0 + i * M15, 100.5, 100.7, 100.3, 100.5) for i in range(36)]   # to 09:00 UTC
    lv = {x.name: x for x in smc.key_levels(prev + today, 0.3)}
    assert lv["PDH"].price == 101.0 and lv["PDL"].price == 100.0
    assert not lv["PDH"].swept and not lv["PDL"].swept
    assert lv["Asian high"].price == 100.7 and "London high" not in lv     # London not finished


def test_previous_day_skips_weekend():
    friday = flat_day(DAY0 - 4 * 86400)          # nothing on Sat/Sun
    monday = [bar(DAY0 - 86400 + i * M15, 100.5, 100.7, 100.3, 100.5) for i in range(8)]
    lv = {x.name: x for x in smc.key_levels(friday + monday, 0.3)}
    assert lv["PDH"].price == 101.0


def test_liquidity_sweep_long_with_structure_stop_and_target():
    prev = flat_day(DAY0 - 86400, hi=102.0)
    today = [bar(DAY0 + i * M15, 100.5, 100.7, 100.3, 100.5) for i in range(40)]
    # 10:00 UTC: wick through PDL (100.0) to 99.8, close back at 100.45 near the high
    today.append(bar(DAY0 + 40 * M15, 100.3, 100.5, 99.8, 100.45))
    candles = prev + today
    c = cfg()
    levels = smc.key_levels(candles, 0.4)
    sig = liquidity_sweep(INSTRUMENTS["XAU_USD"], candles, c, 0.4, levels)
    assert sig and sig.side == "LONG" and sig.type == "liquidity_sweep" and "PDL" in sig.note
    assert sig.stop_atr == round((100.45 - (99.8 - 0.04)) / 0.4, 2)          # beyond the wick
    # nearest opposite liquidity at >= 1.5R: the Asian high (100.7) is too close, so PDH (102)
    assert "target PDH" in sig.note and sig.target_atr == round((102.0 - 100.45) / 0.4, 2)


def test_no_sweep_when_close_is_weak_or_too_deep():
    prev = flat_day(DAY0 - 86400)
    today = [bar(DAY0 + i * M15, 100.5, 100.7, 100.3, 100.5) for i in range(40)]
    weak = today + [bar(DAY0 + 40 * M15, 100.3, 100.5, 99.8, 100.05)]           # closes near the low
    deep = today + [bar(DAY0 + 40 * M15, 100.3, 100.5, 99.0, 100.45)]           # 1 ATR+ through: breakout
    for candles in (prev + weak, prev + deep):
        lv = smc.key_levels(candles, 0.4)
        assert liquidity_sweep(INSTRUMENTS["XAU_USD"], candles, cfg(), 0.4, lv) is None


def test_fvg_found_then_filled():
    rows = [bar(i * M15, 100, 100.2, 99.8, 100) for i in range(20)]
    t = 20 * M15
    rows += [bar(t, 100, 100.2, 99.9, 100.1), bar(t + M15, 100.1, 101.5, 100.1, 101.4),
             bar(t + 2 * M15, 101.4, 101.8, 100.8, 101.6)]                       # gap 100.2 -> 100.8
    zs = [z for z in smc.zones(rows, 0.4) if z.kind == "fvg"]
    assert zs and zs[-1].side == "bull" and (zs[-1].bottom, zs[-1].top) == (100.2, 100.8)
    rows.append(bar(t + 3 * M15, 101.6, 101.6, 100.1, 100.3))                   # fills it fully
    assert not [z for z in smc.zones(rows, 0.4) if z.kind == "fvg" and z.bottom == 100.2]


def test_order_block_detected():
    rows = [bar(i * M15, 100, 100.2, 99.8, 100) for i in range(20)]
    t = 20 * M15
    rows += [bar(t, 100.0, 100.1, 99.6, 99.7),                                  # last bearish candle
             bar(t + M15, 99.7, 101.2, 99.7, 101.1)]                            # displacement, breaks highs
    obs = [z for z in smc.zones(rows, 0.5) if z.kind == "ob"]
    assert obs and obs[-1].side == "bull" and obs[-1].bottom == 99.6


def test_location_filter_blocks_buying_into_liquidity():
    levels = [smc.Level("PDH", 101.0, "high", 0)]
    assert "PDH" in smc.location_check("LONG", 100.8, 0.5, levels, [], 2)      # 0.2 away < 0.5 stop
    assert smc.location_check("LONG", 100.0, 0.5, levels, [], 2) is None        # 1.0 away: room
    assert smc.location_check("SHORT", 100.8, 0.5, levels, [], 2) is None       # a high doesn't block shorts


def test_context_text_lists_levels():
    prev = flat_day(DAY0 - 86400)
    today = [bar(DAY0 + i * M15, 100.5, 100.7, 100.3, 100.5) for i in range(36)]
    txt = smc.context_text(prev + today, 0.3, 2)
    assert "PDH 101.00" in txt and "ATR" in txt


def test_hourly_candles_and_h1_zones_in_chart_json():
    rows = [{"t": 3600 * 10 + k * 900, "o": 100 + k * 0.1, "h": 100.3 + k * 0.1, "l": 99.9 + k * 0.1,
             "c": 100.2 + k * 0.1, "v": 0} for k in range(400)]
    h1 = smc.hourly(rows)
    assert len(h1) == 100 and h1[0]["o"] == rows[0]["o"] and h1[0]["c"] == rows[3]["c"]
    assert h1[0]["h"] == max(r["h"] for r in rows[:4])
    assert smc.hourly(rows[:-1])[-1]["t"] == h1[-2]["t"]          # incomplete last hour dropped
    out = smc.to_json(rows, 0.4)
    assert {"levels", "zones"} <= set(out) and all(z["tf"] in ("", "H1") for z in out["zones"])
