"""Daily discipline rules, cost filter, H1 trend filter, Sonnet escalation and AI budgets."""
import json

import pytest

from app import risk
from app.ai import gate
from app.ai.client import AIClient, model_for
from app.db import DB, iso
from app.settings_store import DEFAULTS, DEFAULTS_VERSION, MODE_DEFAULTS, MODES, SettingsStore, cfg_for, validate
from app.signals import htf_trend


def cfg(mode="standard", **risk_overrides):
    c = cfg_for(validate(DEFAULTS), mode)
    c["risk"] = {**c["risk"], **risk_overrides}
    return c


# ------------------------------------------------------------ day rules
def test_max_trades_per_day():
    c = cfg("standard", max_trades_per_day=3)
    assert risk.day_rules(c, 0.0, 2, 0)[0] is None
    assert "max 3 trades" in risk.day_rules(c, 0.0, 3, 0)[0]


def test_loss_streak_stop_day_vs_half_risk():
    block, mult, _ = risk.day_rules(cfg("standard", loss_streak=2, loss_streak_action="stop_day"), -1.0, 1, 2)
    assert "done for the day" in block
    block, mult, note = risk.day_rules(cfg("moderate", loss_streak=2, loss_streak_action="half_risk"), -1.0, 2, 2)
    assert block is None and mult == 0.5 and "losses in a row" in note


def test_aggressive_halves_risk_after_a_good_day():
    c = cfg("aggressive", profit_half_risk_at_pct=3.0, loss_streak=2)
    assert risk.day_rules(c, 2.9, 1, 0)[1] == 1.0
    assert risk.day_rules(c, 3.1, 1, 0)[1] == 0.5
    assert risk.day_rules(c, 3.1, 1, 2)[1] == 0.25        # good day AND a losing streak


def test_losses_in_row_ignores_breakeven():
    t = [{"pnl_usd": -2, "exit_reason": "stop loss"}, {"pnl_usd": -0.2, "exit_reason": "breakeven stop"},
         {"pnl_usd": -2, "exit_reason": "stop loss"}, {"pnl_usd": 3, "exit_reason": "take profit"}]
    assert risk.losses_in_row(t) == 2
    assert risk.losses_in_row([{"pnl_usd": 3, "exit_reason": "take profit"}] + t) == 0


def test_cost_filter():
    c = cfg("aggressive")                          # 15% max, default stop 1.5 ATR
    # BTC: ATR $100 -> stop $150; $40 spread = 27% of the stop -> skip
    assert "spread is 27%" in risk.spread_check(c, 40.0, 100.0)
    assert risk.spread_check(c, 40.0, 300.0) is None          # 9%
    assert risk.spread_check(cfg("standard"), 40.0, 150.0) is None   # filter off in standard


def test_modes_match_backtested_table():
    s, m, a = (MODE_DEFAULTS[x]["risk"] for x in ("standard", "moderate", "aggressive"))
    assert (s["daily_profit_target_pct"], m["daily_profit_target_pct"], a["daily_profit_target_pct"]) == (4.0, 5.0, 6.0)
    assert (s["daily_loss_limit_pct"], m["daily_loss_limit_pct"], a["daily_loss_limit_pct"]) == (5.0, 6.0, 7.0)
    assert (s["risk_per_trade_pct"], m["risk_per_trade_pct"], a["risk_per_trade_pct"]) == (1.0, 1.5, 2.5)
    assert (s["max_trades_per_day"], m["max_trades_per_day"], a["max_trades_per_day"]) == (2, 3, 6)
    assert (s["kill_switch_drawdown_pct"], m["kill_switch_drawdown_pct"], a["kill_switch_drawdown_pct"]) == (25, 30, 35)
    # every real account trades the backtested gold edge, so all must accept gold's minimum lot
    for x in (s, m, a):
        assert x["min_lot_rule"] == "allow" and x["min_lot_max_risk_pct"] >= 5.0
    assert "XAU_USD:session_breakout" in MODE_DEFAULTS["standard"]["signals"]["allowlist"]
    assert MODE_DEFAULTS["standard"]["signals"]["htf_trend_filter"] is True


# ------------------------------------------------------------ H1 trend
def _m15(closes_h1):
    out, t = [], 1_700_000_400 // 3600 * 3600
    for c in closes_h1:
        for k in range(4):
            out.append({"t": t + k * 900, "o": c, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 0})
        t += 3600
    return out


def test_htf_trend():
    c = cfg("standard")
    assert htf_trend(_m15([100 + i for i in range(80)]), c) == "up"
    assert htf_trend(_m15([200 - i for i in range(80)]), c) == "down"
    assert htf_trend(_m15([100 + i for i in range(30)]), c) is None          # not enough H1 history
    candles = _m15([100 + i for i in range(80)])[:-2]                        # last hour incomplete
    assert htf_trend(candles, c) == "up"


# ------------------------------------------------------------ Sonnet escalation
def test_second_opinion_triggers():
    a, s = cfg("aggressive"), cfg("standard")
    assert "risk" in gate.needs_second_opinion(a, 2.0, False, 0.8)
    assert "0.01 lot" in gate.needs_second_opinion(s, 0.7, True, 0.9)
    assert "borderline" in gate.needs_second_opinion(s, 0.7, False, s["ai"]["min_confidence"] + 0.05)
    assert gate.needs_second_opinion(s, 0.7, False, 0.9) is None
    a["ai"]["second_opinion"] = False
    assert gate.needs_second_opinion(a, 3.0, True, 0.5) is None


def test_sonnet_has_own_budget_and_cap():
    db = DB(":memory:")
    ai = AIClient(db)
    ai.client = object()                            # pretend a key is set
    c = cfg("aggressive")
    assert ai.budget_block(c, "strong") is None
    db.execute("INSERT INTO cost_log(at, purpose, model, input_tokens, output_tokens, cost_usd, ok) "
               "VALUES (?,?,?,?,?,?,1)", (iso(), "second_opinion:aggressive", model_for("strong"), 1, 1, 3.0))
    assert "Sonnet budget" in ai.budget_block(c, "strong")
    assert ai.budget_block(c, "fast") is None       # Haiku keeps working: total $3 < $6
    db.execute("INSERT INTO cost_log(at, purpose, model, input_tokens, output_tokens, cost_usd, ok) "
               "VALUES (?,?,?,?,?,?,1)", (iso(), "gate:aggressive", model_for("fast"), 1, 1, 3.0))
    assert "monthly AI budget" in ai.budget_block(c, "fast")


def test_total_budget_hard_max_is_8():
    d = validate(DEFAULTS)
    d["ai"]["monthly_budget_usd"] = 50
    d["ai"]["strong_budget_usd"] = 50
    out = validate(d)
    assert out["ai"]["monthly_budget_usd"] == 8 and out["ai"]["strong_budget_usd"] == 5


# ------------------------------------------------------------ settings upgrade
def test_old_saved_settings_get_new_defaults_once():
    db = DB(":memory:")
    old = validate(DEFAULTS)
    old["modes"]["standard"]["risk"]["risk_per_trade_pct"] = 1.0
    old["modes"]["standard"]["risk"]["daily_profit_target_pct"] = 0.0
    old["broker"]["spread_XAU_USD"] = 35
    old["broker"]["spread_BTC_USDT"] = 4100            # user-changed value: kept
    old["ai"]["monthly_budget_usd"] = 2.0
    db.execute("INSERT INTO settings_versions(at, source, data) VALUES (?,?,?)", (iso(), "ui", json.dumps(old)))
    store = SettingsStore(db)
    assert store.cfg("standard")["risk"]["risk_per_trade_pct"] == MODE_DEFAULTS["standard"]["risk"]["risk_per_trade_pct"]
    assert store.cfg("standard")["risk"]["daily_profit_target_pct"] == MODE_DEFAULTS["standard"]["risk"]["daily_profit_target_pct"]
    assert "lab" in store.current["modes"]
    assert store.current["broker"]["spread_XAU_USD"] == 50
    assert store.current["broker"]["spread_BTC_USDT"] == 4100
    assert store.current["ai"]["monthly_budget_usd"] == 6.0
    assert db.kv_get("settings_defaults_version") == DEFAULTS_VERSION
    # a later change by the user is not overwritten on the next start
    data = store.current
    data["modes"]["standard"]["risk"]["risk_per_trade_pct"] = 0.5
    store.save(data)
    assert SettingsStore(db).cfg("standard")["risk"]["risk_per_trade_pct"] == 0.5


def test_upgrade_from_v2_server_adds_lab_allowlists_and_new_playbooks():
    db = DB(":memory:")
    old = validate(DEFAULTS)
    old["modes"]["standard"]["signals"]["allowlist"] = []
    db.execute("INSERT INTO settings_versions(at, source, data) VALUES (?,?,?)", (iso(), "ui", json.dumps(old)))
    db.kv_set("settings_defaults_version", 2)
    store = SettingsStore(db)
    assert store.cfg("standard")["signals"]["allowlist"][0] == "XAU_USD:session_breakout"
    assert "US30:zone_retest@liq+trail1" in store.cfg("standard")["signals"]["allowlist"]
    assert store.current["instruments"]["US30"] is True
    assert store.cfg("lab")["ai"]["use_ai"] is False
    assert db.scalar("SELECT COUNT(*) FROM playbook_versions WHERE source='seed-v4'") == len(MODES)
    assert db.kv_get("settings_defaults_version") == 4
