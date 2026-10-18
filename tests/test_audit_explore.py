"""Audit engine (funnel, streak statistics, drift, report) and the exploration budget."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import audit
from app.ai import postmortem
from app.db import DB, iso
from app.engine import Engine
from app.settings_store import DEFAULTS, MODES, cfg_for, validate


@pytest.fixture
def db():
    d = DB(":memory:")
    d.ensure_accounts(MODES, 250)
    return d


def add_trade(db, account, r, days_ago=1, trial=0, symbol="XAU_USD", signal="session_breakout", regime="trend",
              reason=None):
    t = iso(datetime.now(timezone.utc) - timedelta(days=days_ago))
    db.execute("INSERT INTO trades(account, symbol, side, lots, units, leverage, entry, exit, stop, target, pnl_usd, "
               "pnl_r, signal, regime, exit_reason, opened_at, closed_at, trial) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (account, symbol, "LONG", 0.01, 1, 1, 100, 99, 98, 104, r * 4, r, signal, regime,
                reason or ("take profit" if r > 0 else "stop loss"), t, t, trial))


# ------------------------------------------------------------ audit
def test_filter_keys():
    assert audit.filter_key("") == "no setup"
    assert audit.filter_key("dead market (ATR 1.2 < p20 1.5)") == "dead market (low volatility)"
    assert audit.filter_key("strategy not on this account's allowlist for this symbol") == "setup not on allowlist"


def test_short_streak_is_normal_long_streak_is_flagged_with_cause(db):
    for r in (1, -1, -1, -1):                     # newest last -> streak of 3
        add_trade(db, "standard", r)
    s = audit.streak_check(db, "standard")
    assert s["streak"] == 3 and s["level"] == "green" and "normal variance" in s["verdict"]
    for _ in range(6):
        add_trade(db, "standard", -1, days_ago=0)
    s = audit.streak_check(db, "standard")
    assert s["streak"] == 9 and s["level"] == "red" and "signal = session_breakout" in s["verdict"]


def test_trials_do_not_count_in_streaks_or_drift(db):
    for _ in range(9):
        add_trade(db, "standard", -1, trial=1)
    assert audit.streak_check(db, "standard")["streak"] == 0
    assert audit.drift_check(db, "standard")["n"] == 0


def test_drift_flags_results_below_backtest_range(db):
    for _ in range(30):
        add_trade(db, "moderate", -0.6)
    d = audit.drift_check(db, "moderate")
    assert d["level"] == "red" and "edge may be fading" in d["verdict"]


def test_daily_report_explains_no_trades(db):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.execute("INSERT INTO audit_counters(day, account, key, n) VALUES (?,?,?,?)", (day, "standard", "checked", 96))
    db.execute("INSERT INTO audit_counters(day, account, key, n) VALUES (?,?,?,?)", (day, "standard", "filter:no setup", 90))
    db.execute("INSERT INTO audit_counters(day, account, key, n) VALUES (?,?,?,?)",
               (day, "standard", "filter:against the H1 trend", 6))
    rep = audit.daily_report(db, validate(DEFAULTS), "standard", datetime.now(timezone.utc))
    act = [i for i in rep["items"] if i["area"] == "activity"][0]
    assert "quiet market" in act["message"] and "96 candles" in act["message"]
    assert rep["funnel"]["filtered"][0]["reason"] == "against the H1 trend"
    assert "Daily audit" in audit.telegram_text([rep])


def test_postmortem_prompt_builds_without_candles(db):
    add_trade(db, "standard", -1)
    t = db.one("SELECT * FROM trades")
    text = postmortem.build_prompt(db, cfg_for(validate(DEFAULTS), "standard"), t)
    assert "TRADE: GOLD LONG" in text and "PLAYBOOK" in text


# ------------------------------------------------------------ exploration
def test_explore_cfg_adds_trials_to_allowlist_and_enables_them():
    cfg = cfg_for(validate(DEFAULTS), "moderate")
    out = Engine._explore_cfg(None, cfg)
    assert "XAU_USD:session_breakout" in out["signals"]["allowlist"]
    assert "*:zone_retest" in out["signals"]["allowlist"] and out["signals"]["zone_retest"] is True
    assert "*:zone_retest" not in cfg["signals"]["allowlist"]        # original untouched
    cfg["explore"]["enabled"] = False
    assert Engine._explore_cfg(None, cfg) is cfg


def test_trial_budget_and_daily_cap(db):
    cfg = cfg_for(validate(DEFAULTS), "moderate")                      # 2% of $250 = $5 a week, 2 a day
    eng = SimpleNamespace(db=db)
    assert Engine._trial_block(eng, "moderate", cfg, 250.0) is None
    add_trade(db, "moderate", -1, days_ago=0, trial=1)                  # -$4
    assert Engine._trial_block(eng, "moderate", cfg, 250.0) is None or "trial trades today" in Engine._trial_block(
        eng, "moderate", cfg, 250.0)
    add_trade(db, "moderate", -0.5, days_ago=0, trial=1)                # -$6 total
    assert "budget used" in Engine._trial_block(eng, "moderate", cfg, 250.0)
