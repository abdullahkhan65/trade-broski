"""Stall exit ("human instinct"), opening-range and PDH breakouts, allowlist, Lab account."""
from datetime import datetime, timezone

from app import smc
from app.exits import stall_exit
from app.instruments import INSTRUMENTS
from app.settings_store import DEFAULTS, MODE_DEFAULTS, MODES, cfg_for, validate
from app.signals import allowed, opening_range_breakout, pdh_breakout

M15 = 900


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0}


def cfg(mode="moderate", **exits):
    c = cfg_for(validate(DEFAULTS), mode)
    c["exits"].update(exits)
    return c


STALL = dict(stall_exit=True, stall_min_progress=0.5, stall_candles=4, stall_range_atr=1.0)


def test_stall_exit_short_gold_example():
    # short 4070 -> TP 4050; drops to ~4060 (50%) then just chops 4059-4062 for 4 candles
    bars = [bar(0, 4070, 4071, 4064, 4065), bar(900, 4065, 4066, 4059.5, 4060)]
    bars += [bar(900 * (2 + k), 4060, 4062, 4059.8, 4060) for k in range(4)]
    why = stall_exit("SHORT", 4070, 4050, bars, 8.0, cfg(**STALL))
    assert why and "momentum stalled" in why


def test_no_stall_while_still_making_progress_or_too_early():
    trending = [bar(900 * k, 4070 - 2 * k, 4071 - 2 * k, 4068 - 2 * k, 4069 - 2 * k) for k in range(8)]
    assert stall_exit("SHORT", 4070, 4050, trending, 8.0, cfg(**STALL)) is None        # new lows each candle
    early = [bar(0, 4070, 4071, 4066, 4067)] + [bar(900 * (1 + k), 4067, 4068, 4066.5, 4067) for k in range(4)]
    assert stall_exit("SHORT", 4070, 4050, early, 8.0, cfg(**STALL)) is None           # only 15% of the way
    bars = [bar(0, 4070, 4071, 4064, 4065), bar(900, 4065, 4066, 4059.5, 4060)]
    bars += [bar(900 * (2 + k), 4060, 4062, 4059.8, 4060) for k in range(4)]
    assert stall_exit("SHORT", 4070, 4050, bars, 8.0, cfg(stall_exit=False)) is None   # switched off


def test_no_stall_on_sharp_pullback():
    bars = [bar(0, 4070, 4071, 4064, 4065), bar(900, 4065, 4066, 4059.5, 4060)]
    bars += [bar(900 * (2 + k), 4060, 4069, 4059.8, 4061) for k in range(4)]         # 9-point range > 1 ATR (8)
    assert stall_exit("SHORT", 4070, 4050, bars, 8.0, cfg(**STALL)) is None


def test_london_orb_long():
    day = datetime(2026, 10, 20, tzinfo=timezone.utc)          # 08:00 London = 07:00 UTC (BST)
    t0 = int(day.timestamp())
    candles = [bar(t0 + k * M15, 100, 100.2, 99.8, 100) for k in range(28)]          # to 07:00 UTC
    candles += [bar(t0 + (28 + k) * M15, 100, 101, 99.5, 100.5) for k in range(4)]   # range 99.5-101
    candles.append(bar(t0 + 32 * M15, 100.6, 101.4, 100.5, 101.3))                    # 08:00 UTC breakout
    now = datetime.fromtimestamp(candles[-1]["t"] + M15, tz=timezone.utc)
    sig = opening_range_breakout(INSTRUMENTS["XAU_USD"], candles, cfg(), 1.0, now, "london_orb")
    assert sig and sig.side == "LONG" and sig.stop_atr == round(101.3 - 100.25, 2)    # stop at range middle


def test_pdh_breakout_first_close_only():
    d0 = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
    prev = [bar(d0 - 86400 + k * M15, 100, 100.5, 99.5, 100) for k in range(96)]
    today = [bar(d0 + k * M15, 100, 100.3, 99.9, 100.1) for k in range(40)]
    today.append(bar(d0 + 40 * M15, 100.1, 101.0, 100.0, 100.9))                      # strong close above 100.5
    candles = prev + today
    lv = smc.key_levels(candles, 0.6)
    sig = pdh_breakout(INSTRUMENTS["XAU_USD"], candles, cfg(), 0.6, lv)
    assert sig and sig.side == "LONG" and "PDH" in sig.note
    later = candles + [bar(d0 + 41 * M15, 100.9, 101.5, 100.8, 101.4)]                # second close above: no
    assert pdh_breakout(INSTRUMENTS["XAU_USD"], later, cfg(), 0.6, smc.key_levels(later, 0.6)) is None


def test_allowlist_patterns():
    allow = ["XAU_USD:session_breakout", "BTC_USDT:*", "*:ny_orb"]
    assert allowed(allow, "XAU_USD", "session_breakout")
    assert not allowed(allow, "XAU_USD", "trend_pullback")
    assert allowed(allow, "BTC_USDT", "trend_pullback")
    assert allowed(allow, "EUR_USD", "ny_orb")


def test_lab_account_takes_everything_without_ai():
    assert "lab" in MODES
    lab = MODE_DEFAULTS["lab"]
    assert lab["ai"]["use_ai"] is False and lab["signals"]["allowlist"] == []
    assert not lab["signals"]["smc_filter"] and not lab["signals"]["htf_trend_filter"]
    assert lab["adaptive"]["enabled"] is False and lab["risk"]["daily_profit_target_pct"] == 0
