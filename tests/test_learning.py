import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import adaptive, risk
from app import indicators as ind
from app.db import DB, iso
from app.instruments import INSTRUMENTS
from app.settings_store import DEFAULTS, MODES, cfg_for, validate
from app.signals import detect_regime, momentum_breakout, range_reversion
from app.tuner import grid

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def cfg(mode="standard"):
    return cfg_for(validate(DEFAULTS), mode)


def add_trades(db, account, signal, regime, rs, start=NOW - timedelta(days=5)):
    for i, r in enumerate(rs):
        t = iso(start + timedelta(minutes=i))
        db.execute("INSERT INTO trades(account, symbol, side, lots, units, leverage, entry, exit, pnl_usd, pnl_r, "
                   "signal, regime, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (account, "EUR_USD", "LONG", 0.01, 1000, 1, 1.1, 1.1, r * 2.5, r, signal, regime, t, t))


@pytest.fixture
def db():
    d = DB(":memory:")
    d.ensure_accounts(MODES, 250)
    return d


# ---------------------------------------------------------------- regimes
def test_adx_trend_vs_range():
    up = [100 + i * 0.5 for i in range(80)]
    flat = [100 + (0.3 if i % 2 else -0.3) for i in range(80)]
    assert ind.adx([x + 0.4 for x in up], [x - 0.4 for x in up], up)[-1] > 40
    assert ind.adx([x + 0.4 for x in flat], [x - 0.4 for x in flat], flat)[-1] < 20


def test_detect_regime_labels():
    c = cfg()
    trend = [{"t": i * 900, "o": 100 + i * 0.5, "h": 100.4 + i * 0.5, "l": 99.6 + i * 0.5, "c": 100 + i * 0.5} for i in range(200)]
    assert detect_regime(trend, c).name in ("trend", "volatile")
    rng = [{"t": i * 900, "o": 100, "h": 100.4, "l": 99.6, "c": 100 + (0.2 if i % 2 else -0.2)} for i in range(200)]
    assert detect_regime(rng, c).name == "range"


# ------------------------------------------------------------- new signals
def test_range_reversion_long():
    c = cfg()
    candles = [{"t": i * 900, "o": 1.1, "h": 1.1005, "l": 1.0995, "c": 1.1 + (0.0002 if i % 2 else -0.0002)} for i in range(60)]
    # a sharp drop under the lower band, then a bullish close back inside
    for k, px in enumerate([1.0990, 1.0985, 1.0975]):
        candles.append({"t": (60 + k) * 900, "o": px + 0.0004, "h": px + 0.0005, "l": px - 0.0003, "c": px})
    candles.append({"t": 63 * 900, "o": 1.0976, "h": 1.0992, "l": 1.0975, "c": 1.0991})
    reg = type("R", (), {"name": "range", "__str__": lambda s: "range"})()
    sig = range_reversion(INSTRUMENTS["EUR_USD"], candles, c, 0.0005, reg)
    assert sig and sig.side == "LONG" and sig.type == "range_reversion"
    trend = type("R", (), {"name": "trend", "__str__": lambda s: "trend"})()
    assert range_reversion(INSTRUMENTS["EUR_USD"], candles, c, 0.0005, trend) is None


def test_momentum_breakout_long():
    c = cfg()
    candles = [{"t": i * 900, "o": 100 + i * 0.1, "h": 100.3 + i * 0.1, "l": 99.8 + i * 0.1, "c": 100.1 + i * 0.1} for i in range(80)]
    last = candles[-1]["c"]
    candles.append({"t": 80 * 900, "o": last, "h": last + 1.25, "l": last - 0.05, "c": last + 1.2})
    reg = type("R", (), {"name": "trend", "__str__": lambda s: "trend"})()
    sig = momentum_breakout(INSTRUMENTS["XAU_USD"], candles, c, 1.0, reg)
    assert sig and sig.side == "LONG"


# --------------------------------------------------------------- adaptive
def test_strategy_switched_off_then_probation_then_back(db):
    c = cfg()
    add_trades(db, "standard", "trend_pullback", "range", [-1.0] * 20)
    changes = adaptive.update(db, "standard", c, NOW)
    assert any("switched off" in x for x in changes)
    assert adaptive.lookup(db, "standard", c, "trend_pullback", "range")[0] == "off"
    assert adaptive.lookup(db, "standard", c, "trend_pullback", "trend")[0] == "active"   # other regime unaffected
    # 15 days later: probation at half risk
    later = NOW + timedelta(days=15)
    adaptive.update(db, "standard", c, later)
    status, mult, _ = adaptive.lookup(db, "standard", c, "trend_pullback", "range")
    assert status == "probation" and mult == 0.5
    # 8 winning probation trades -> active again
    add_trades(db, "standard", "trend_pullback", "range", [1.5] * 8, start=later + timedelta(hours=1))
    adaptive.update(db, "standard", c, later + timedelta(days=2))
    assert adaptive.lookup(db, "standard", c, "trend_pullback", "range")[:2] == ("active", 1.0)


def test_weak_strategy_gets_half_risk_and_few_trades_not_judged(db):
    c = cfg()
    add_trades(db, "moderate", "session_breakout", "trend", [1, -1] * 7 + [-1.5])
    adaptive.update(db, "moderate", c, NOW)
    assert adaptive.lookup(db, "moderate", c, "session_breakout", "trend")[:2] == ("cautious", 0.5)
    add_trades(db, "moderate", "range_reversion", "range", [-1, -1, -1])
    adaptive.update(db, "moderate", c, NOW)
    assert adaptive.lookup(db, "moderate", c, "range_reversion", "range")[0] == "active"   # too few trades to judge


def test_switching_can_be_disabled(db):
    c = cfg()
    add_trades(db, "standard", "trend_pullback", "range", [-1.0] * 20)
    adaptive.update(db, "standard", c, NOW)
    c["adaptive"]["enabled"] = False
    assert adaptive.lookup(db, "standard", c, "trend_pullback", "range")[0] == "active"


def test_risk_mult_only_lowers_risk():
    c = cfg("standard")
    full = risk.size_order(INSTRUMENTS["EUR_USD"], c, "LONG", 1.1, 0.0005, 250.0, 0.0)
    half = risk.size_order(INSTRUMENTS["EUR_USD"], c, "LONG", 1.1, 0.0005, 250.0, 0.0, risk_mult=0.5)
    more = risk.size_order(INSTRUMENTS["EUR_USD"], c, "LONG", 1.1, 0.0005, 250.0, 0.0, risk_mult=3.0)
    assert half.lots < full.lots and more.lots == full.lots and "adaptive" in half.note


# ------------------------------------------------------------------ tuner
def test_tuner_grid_respects_mode_limits():
    e = cfg("aggressive")["exits"]
    g = grid(e)
    assert {"default_stop_atr": e["default_stop_atr"], "default_target_atr": e["default_target_atr"],
            "breakeven_at_r": e["breakeven_at_r"]} in g
    for p in g:
        assert e["min_stop_atr"] <= p["default_stop_atr"] <= e["max_stop_atr"]
        assert p["default_target_atr"] >= p["default_stop_atr"] * e["min_reward_risk"]


# --------------------------------------------------------------- database
def test_v2_database_upgraded_in_place(tmp_path):
    path = tmp_path / "trader.db"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY, account TEXT, ticket INTEGER, symbol TEXT,
        side TEXT, lots REAL, units REAL, leverage REAL, entry REAL, exit REAL, stop REAL, target REAL,
        pnl_usd REAL, pnl_r REAL, fees REAL, swap REAL, funding REAL, signal TEXT, ai_reason TEXT,
        exit_reason TEXT, opened_at TEXT, closed_at TEXT);
        INSERT INTO trades(account, symbol, pnl_usd) VALUES ('standard', 'EUR_USD', 1.5);
        PRAGMA user_version=2;""")
    conn.close()
    db = DB(path)
    assert db.migrated_from is None                                    # kept, not wiped
    assert db.one("SELECT pnl_usd FROM trades")["pnl_usd"] == 1.5
    assert "regime" in {r["name"] for r in db.all("PRAGMA table_info(trades)")}
