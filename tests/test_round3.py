"""Partial take-profit + trailing stop, zone retest, liquidity targets, Asian-range filter,
AI calibration proposals, research candidate instruments."""
from datetime import datetime, timedelta, timezone

import pytest

from app import promoter, smc
from app.db import DB, iso
from app.exits import manage
from app.instruments import EXTRA_INSTRUMENTS, INSTRUMENTS
from app.sessions import is_forex_like, market_open
from app.settings_store import DEFAULTS, MODES, cfg_for, validate
from app.signals import Signal, set_liquidity_targets, zone_retest

M15 = 900


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0}


def cfg(mode="moderate", **exits):
    c = cfg_for(validate(DEFAULTS), mode)
    c["exits"] = {**c["exits"], "breakeven_at_r": 0.0, **exits}
    return c


# ------------------------------------------------------------ exits
def test_trailing_stop_follows_best_price():
    c = cfg(trail_r=1.0)
    # long 100, stop 98 (risk 2): at +1R (102) stop -> entry; at +2.5R (105) stop -> 103
    assert manage("LONG", 100, 98, 98, 1.9, 0.01, 0.01, False, c) == (98, 0.0)
    assert manage("LONG", 100, 98, 98, 2.0, 0.01, 0.01, False, c)[0] == pytest.approx(100)
    assert manage("LONG", 100, 98, 100, 5.0, 0.01, 0.01, False, c)[0] == pytest.approx(103)
    assert manage("SHORT", 100, 102, 102, 5.0, 0.01, 0.01, False, c)[0] == pytest.approx(97)
    # never loosens
    assert manage("LONG", 100, 98, 103, 2.0, 0.01, 0.01, False, c)[0] == 103


def test_partial_needs_two_min_lots():
    c = cfg(partial_at_r=1.0, partial_fraction=0.5)
    assert manage("LONG", 100, 98, 98, 2.0, 0.01, 0.01, False, c) == (98, 0.0)      # 0.01 lot: can't split
    stop, frac = manage("LONG", 100, 98, 98, 2.0, 0.04, 0.01, False, c)
    assert frac == 0.5 and stop == 100                                              # half off, stop to entry
    assert manage("LONG", 100, 98, 100, 3.0, 0.02, 0.01, True, c)[1] == 0.0         # only once


# ------------------------------------------------------------ zone retest
def test_zone_retest_long_on_fresh_bullish_fvg():
    rows = [bar(k * M15, 100, 100.2, 99.8, 100) for k in range(30)]
    t = 30 * M15
    rows += [bar(t, 100, 100.2, 99.9, 100.1), bar(t + M15, 100.1, 101.5, 100.1, 101.4),
             bar(t + 2 * M15, 101.4, 101.8, 100.8, 101.6)]                       # bullish FVG 100.2-100.8
    rows += [bar(t + 3 * M15, 101.6, 101.9, 101.3, 101.5), bar(t + 4 * M15, 101.5, 101.7, 101.0, 101.2)]
    rows.append(bar(t + 5 * M15, 101.0, 101.3, 100.5, 101.1))                    # dips into the gap, closes out
    c = cfg()
    sig = zone_retest(INSTRUMENTS["XAU_USD"], rows, c, 0.4)
    assert sig and sig.side == "LONG" and "fair value gap" in sig.note
    assert sig.stop_atr == pytest.approx(max((101.1 - (100.2 - 0.08)) / 0.4, c["exits"]["min_stop_atr"]), abs=0.01)
    rows[-1] = bar(t + 5 * M15, 101.0, 101.3, 99.9, 100.0)                       # blew through: no trade
    assert zone_retest(INSTRUMENTS["XAU_USD"], rows, c, 0.4) is None


# ------------------------------------------------------------ liquidity targets
def test_liquidity_target_picks_nearest_far_enough_level():
    c = cfg()        # moderate: stop 1.5 ATR, min RR 1.5 -> target must be >= 2.25 ATR away
    levels = [smc.Level("Asian high", 101.0, "high", 0), smc.Level("PDH", 103.0, "high", 0),
              smc.Level("equal highs", 105.0, "high", 0)]
    sig = Signal("XAU_USD", "LONG", "session_breakout", 100.0, 1.0, 0, "x")
    set_liquidity_targets([sig], c, 1.0, levels)
    assert sig.target_atr == 3.0 and "PDH" in sig.note                            # 101 too close, 103 ok
    far = Signal("XAU_USD", "LONG", "session_breakout", 100.0, 1.0, 0, "x")
    set_liquidity_targets([far], c, 1.0, [smc.Level("PDH", 120.0, "high", 0)])
    assert far.target_atr is None                                                 # > 8 ATR: keep default


# ------------------------------------------------------------ calibration
def test_calibration_lowers_confidence_when_skips_would_have_won():
    db = DB(":memory:")
    db.ensure_accounts(MODES, 250)
    settings = validate(DEFAULTS)
    for _ in range(30):
        db.execute("INSERT INTO decisions(at, account, symbol, signal, side, accepted, reject_reason, shadow_result, shadow_r) "
                   "VALUES (?,?,?,?,?,0,?,?,?)", (iso(), "moderate", "XAU_USD", "session_breakout", "LONG",
                                                  "AI SKIP: unsure", "target", 1.5))
    cal = promoter.calibrations(db, settings)
    assert [(c["account"], c["item"]) for c in cal] == [("moderate", "ai.min_confidence=0.45")]
    new = promoter.apply_proposal(settings, {"account": "moderate", "action": "set", "item": cal[0]["item"]})
    assert new["modes"]["moderate"]["ai"]["min_confidence"] == 0.45


# ------------------------------------------------------------ research candidates
def test_candidate_instruments_follow_market_hours():
    assert not set(EXTRA_INSTRUMENTS) & set(INSTRUMENTS)            # research-only, not live
    us30 = INSTRUMENTS["US30"]                                      # promoted to live after round 3
    assert is_forex_like(us30)
    sat = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    tue = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
    brk = datetime(2026, 10, 20, 21, 30, tzinfo=timezone.utc)      # 17:30 New York: daily break
    assert not market_open(us30, sat) and market_open(us30, tue) and not market_open(us30, brk)
