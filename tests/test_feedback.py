"""Feedback engine: shadow outcomes, pooled Lab learning, Lab -> allowlist proposals, research input."""
from datetime import datetime, timedelta, timezone

import pytest

from app import adaptive, promoter, shadow
from app.ai import research
from app.db import DB, iso
from app.settings_store import DEFAULTS, MODES, cfg_for, validate

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    d = DB(":memory:")
    d.ensure_accounts(MODES, 250)
    return d


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0}


def add_trade(db, account, symbol, signal, regime, r, days_ago=5):
    t = iso(datetime.now(timezone.utc) - timedelta(days=days_ago))
    db.execute("INSERT INTO trades(account, symbol, side, lots, units, leverage, entry, exit, pnl_usd, pnl_r, "
               "signal, regime, exit_reason, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (account, symbol, "LONG", 0.01, 1, 1, 1, 1, r * 3, r, signal, regime,
                "take profit" if r > 0 else "stop loss", t, t))


# ------------------------------------------------------------ shadow outcomes
def test_shadow_outcome_target_stop_expired():
    t0 = 1_000_000
    up = [bar(t0 + k * 900, 100 + k, 100.5 + k, 99.8 + k, 100.4 + k) for k in range(10)]
    assert shadow.outcome("LONG", 100, 98, 104, up, t0, t0 + 48 * 3600, 0.0, t0 + 50 * 3600) == ("target", 2.0)
    down = [bar(t0 + k * 900, 100 - k, 100.2 - k, 99.5 - k, 99.6 - k) for k in range(10)]
    assert shadow.outcome("LONG", 100, 98, 104, down, t0, t0 + 48 * 3600, 0.0, t0 + 50 * 3600) == ("stop", -1.0)
    flat = [bar(t0 + k * 900, 100, 100.3, 99.7, 100.2) for k in range(10)]
    assert shadow.outcome("LONG", 100, 98, 104, flat, t0, t0 + 48 * 3600, 0.0, t0 + 3600) is None   # still open
    res = shadow.outcome("LONG", 100, 98, 104, flat, t0, t0 + 48 * 3600, 0.2, t0 + 50 * 3600)
    assert res[0] == "expired" and res[1] == pytest.approx(0.1 - 0.1)          # +0.2/2 R, minus 0.2/2 spread


def test_shadow_categories():
    assert shadow.category("Sonnet veto (risk 2.5%): AI SKIP: ...") == "Sonnet veto"
    assert shadow.category("AI SKIP: chasing") == "Haiku skip"
    assert shadow.category("spread is 40% of the stop (max 15%)") == "cost filter"
    assert shadow.category("SMC: PDH 4183.52 is right above") == "SMC filter"
    assert shadow.category("long signal against the H1 trend (down)") == "H1 trend filter"


def test_shadow_summary_verdicts(db):
    for r in (-1, -1, -1, 2):          # the AI skipped 3 losers and 1 winner -> saving money
        db.execute("INSERT INTO decisions(at, account, symbol, signal, side, accepted, reject_reason, shadow_result, shadow_r) "
                   "VALUES (?,?,?,?,?,0,?,?,?)", (iso(), "standard", "XAU_USD", "session_breakout", "LONG",
                                                  "AI SKIP: no", "target" if r > 0 else "stop", r))
    s = shadow.summary(db, "standard")
    assert s[0]["category"] == "Haiku skip" and s[0]["total_r"] == -1.0 and s[0]["verdict"] == "neutral"
    db.execute("INSERT INTO decisions(at, account, symbol, signal, side, accepted, reject_reason, shadow_result, shadow_r) "
               "VALUES (?,?,?,?,?,0,?,?,?)", (iso(), "standard", "XAU_USD", "session_breakout", "LONG", "AI SKIP: no", "stop", -1))
    assert shadow.summary(db, "standard")[0]["verdict"] == "saving money"


# ------------------------------------------------------------ pooled learning
def test_pooled_lab_trades_switch_off_faster(db):
    cfg = cfg_for(validate(DEFAULTS), "standard")
    for _ in range(4):
        add_trade(db, "standard", "XAU_USD", "session_breakout", "range", -1)
    for _ in range(30):                                # lab: same strategy+regime on gold, losing
        add_trade(db, "lab", "XAU_USD", "session_breakout", "range", -1)
    for _ in range(30):                                # lab on EURUSD: not allowlisted for standard -> ignored
        add_trade(db, "lab", "EUR_USD", "session_breakout", "range", +2)
    adaptive.update(db, "standard", cfg)
    st = {(s["signal"], s["regime"]): s for s in adaptive.states(db, "standard")}
    assert st[("session_breakout", "range")]["status"] == "off"
    assert "Lab" in st[("session_breakout", "range")]["reason"]
    cfg["adaptive"]["pool_lab"] = False
    adaptive.reset(db, "standard", "session_breakout", "range")
    adaptive.update(db, "standard", cfg)
    st = {(s["signal"], s["regime"]): s for s in adaptive.states(db, "standard")}
    assert st[("session_breakout", "range")]["status"] == "active"      # 4 own trades: too few to judge


# ------------------------------------------------------------ promotion pipeline
def test_candidates_and_removals(db):
    settings = validate(DEFAULTS)
    for _ in range(12):
        add_trade(db, "lab", "BTC_USDT", "ny_orb", "trend", +0.5)
    for _ in range(12):
        add_trade(db, "lab", "EUR_USD", "trend_pullback", "trend", -0.4)
    cands = promoter.candidates(db, settings)
    assert {(c["account"], c["item"]) for c in cands} == {(m, "BTC_USDT:ny_orb") for m in ("standard", "moderate", "aggressive")}
    for _ in range(12):
        add_trade(db, "moderate", "XAU_USD", "session_breakout", "range", -0.5)
    rem = promoter.removals(db, settings)
    assert [(r["account"], r["item"]) for r in rem] == [("moderate", "XAU_USD:session_breakout")]
    # a pending proposal is not proposed twice
    promoter.propose(db, "standard", "add", "BTC_USDT:ny_orb", {}, "test")
    assert ("standard", "BTC_USDT:ny_orb") not in {(c["account"], c["item"]) for c in promoter.candidates(db, settings)}


def test_apply_proposal_edits_allowlist_and_enables_strategy():
    settings = validate(DEFAULTS)
    new = promoter.apply_proposal(settings, {"account": "standard", "action": "add", "item": "BTC_USDT:london_orb"})
    sig = new["modes"]["standard"]["signals"]
    assert "BTC_USDT:london_orb" in sig["allowlist"] and sig["london_orb"] is True
    back = promoter.apply_proposal(new, {"account": "standard", "action": "remove", "item": "BTC_USDT:london_orb"})
    assert "BTC_USDT:london_orb" not in back["modes"]["standard"]["signals"]["allowlist"]
    assert settings["modes"]["standard"]["signals"]["allowlist"] == ["XAU_USD:session_breakout"]   # not mutated


def test_research_input_builds(db):
    add_trade(db, "standard", "XAU_USD", "session_breakout", "trend", 1.5)
    add_trade(db, "lab", "BTC_USDT", "ny_orb", "trend", -1)
    text = research.build_input(db, validate(DEFAULTS))
    assert "Standard" in text and "Lab league table" in text and "BTC_USDT ny_orb" in text
