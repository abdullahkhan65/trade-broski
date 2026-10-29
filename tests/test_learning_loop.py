"""Learning loop: per-setup rules, decision features, loss causes, Lab seasons, rule proposals."""
import asyncio
import json

from app import lab, promoter, signals
from app.instruments import INSTRUMENTS
from app.settings_store import DEFAULTS, cfg_for, validate
from app.signals import Signal, parse_rules, rule_block

from tests.test_engine_path import TUE, engine, gold_signal

F = {"hour": 13, "day": "Wed", "regime": "volatile", "adx": 31.0, "atr_pct": 92, "side": "LONG"}


def test_rules_parse_and_block():
    rules = parse_rules(["XAU_USD:session_breakout skip_hours=13,14", "XAU_USD:session_breakout@trail1.5 max_atr_pct=90",
                         "US30:ny_orb regimes=trend,range", "junk line", "US30:ny_orb bogus=1"])
    gold = rules["XAU_USD:session_breakout"]
    assert gold == {"skip_hours": [13, 14], "max_atr_pct": 90.0}
    assert "13:00" in rule_block(gold, F)
    assert "percentile" in rule_block({"max_atr_pct": 90.0}, F)
    assert rule_block({"min_atr_pct": 50.0}, F) is None
    assert "volatile" in rule_block(rules["US30:ny_orb"], F)
    assert rule_block({"sides": ["SHORT"]}, F) and rule_block({"skip_days": ["Mon"]}, F) is None
    assert rule_block(None, F) is None


def test_evaluate_applies_setup_rules(monkeypatch):
    sig = gold_signal()
    sig.context["f"] = dict(F)
    monkeypatch.setattr(signals, "raw_signals", lambda *a, **k: ([sig], "", 4.0, []))
    monkeypatch.setattr(signals, "htf_trend", lambda *a, **k: "up")
    monkeypatch.setattr(signals.smc, "key_levels", lambda *a, **k: [])
    monkeypatch.setattr(signals.smc, "zones", lambda *a, **k: [])
    cfg = cfg_for(validate(DEFAULTS), "standard")
    out, why = signals.evaluate(INSTRUMENTS["XAU_USD"], [], cfg, TUE)
    assert out == [sig] and sig.context["f"]["h1"] == "up" and sig.context["f"]["smc"] == ""
    cfg["signals"]["setup_rules"] = ["XAU_USD:session_breakout skip_hours=13"]
    rejected = []
    out, why = signals.evaluate(INSTRUMENTS["XAU_USD"], [], cfg, TUE, rejected=rejected)
    assert out == [] and why.startswith("setup rule:") and rejected[0][0] is sig


def test_loss_causes():
    t = {"side": "LONG", "opened_at": "2026-10-20T14:00:00+00:00", "closed_at": "2026-10-20T14:15:00+00:00",
         "mfe_r": 0.1}
    c = lab.loss_causes({**F, "h1": "down", "smc": "PDH 4150 is right above"}, t, news_near=True)
    assert c[:5] == ["against H1 trend", "into level/OB", "volatile market", "news nearby", "wrong from the start"]
    late = lab.loss_causes({"h1": "up", "hour": 20, "atr_pct": 50, "side": "LONG"},
                           {**t, "closed_at": "2026-10-20T18:00:00+00:00", "mfe_r": 1.4})
    assert late == ["gave back +1R", "late session"]
    assert lab.loss_causes(None, {**t, "closed_at": "2026-10-20T18:00:00+00:00"}) == ["normal loss (no flag)"]


def test_losing_trade_gets_features_and_causes():
    eng = engine()
    sig = gold_signal()
    sig.context["f"] = {**F, "h1": "down", "smc": ""}
    asyncio.run(eng._choose_and_handle("lab", INSTRUMENTS["XAU_USD"], [sig], [], TUE))
    assert json.loads(eng.db.scalar("SELECT features FROM decisions WHERE account='lab'"))["h1"] == "down"
    p = eng.broker.positions("lab")[0]
    t = eng.broker.close(eng.cfg("lab"), p, p["entry"] - 5, "stop")
    assert t["decision_id"] and "against H1 trend" in t["causes"]
    assert eng.db.scalar("SELECT causes FROM trades") == t["causes"]
    s = lab.summary(eng.db)
    assert s["causes"][0]["n"] == 1 and s["causes_by_account"]["lab"]


def test_lab_refills_and_starts_a_new_season():
    eng = engine()
    assert lab.needs_refill(200, 250, "active", 0) is None
    assert lab.needs_refill(140, 250, "active", 1) is None              # wait for open trades to close
    eng.db.execute("UPDATE accounts SET cash=140, equity=140 WHERE id='lab'")
    asyncio.run(eng._lab_refill())
    acc = eng.db.one("SELECT cash, status FROM accounts WHERE id='lab'")
    assert acc["cash"] == 250 and acc["status"] == "active"
    s = lab.summary(eng.db)
    assert s["season"] == 2 and s["refilled"] == 110 and s["all_time_usd"] == -110
    eng.db.execute("UPDATE accounts SET status='paused_weekly' WHERE id='lab'")
    asyncio.run(eng._lab_refill())
    assert lab.summary(eng.db)["season"] == 3


def test_rule_proposal_is_applied():
    s = validate(DEFAULTS)
    out = promoter.apply_proposal(s, {"account": "aggressive", "action": "rule",
                                      "item": "XAU_USD:session_breakout skip_hours=13"})
    assert out["modes"]["aggressive"]["signals"]["setup_rules"] == ["XAU_USD:session_breakout skip_hours=13"]
    assert validate(out)["modes"]["aggressive"]["signals"]["setup_rules"] == ["XAU_USD:session_breakout skip_hours=13"]
    add = promoter.apply_proposal(s, {"account": "standard", "action": "add", "item": "US30:pdh_breakout@liq+trail1"})
    assert add["modes"]["standard"]["signals"]["pdh_breakout"] is True     # strategy switched on despite the @profile


class SkipAI:
    """Haiku always says SKIP; records calls (no Sonnet expected in advisory mode)."""
    def __init__(self):
        self.calls = []

    def budget_block(self, cfg, tier="fast"):
        return None

    async def call_json(self, cfg, purpose, system, user, schema, max_tokens=400, tier="fast", effort="low"):
        from app.ai.client import AIResult
        self.calls.append((purpose, tier))
        return AIResult(True, data={"decision": "SKIP", "side": "LONG", "confidence": 0.3, "leverage": 1,
                                    "stop_atr": 1.5, "target_atr": 3, "reason": "into resistance"})


def test_advisory_ai_takes_proven_setups_and_labels_them():
    eng = engine()
    eng.ai = SkipAI()
    data = eng.store.current
    data["modes"]["aggressive"]["ai"]["veto_proven"] = False
    eng.store.save(data)
    sig = gold_signal()
    sig.type = "pdh_breakout"                                          # on Aggressive's allowlist
    asyncio.run(eng._choose_and_handle("aggressive", INSTRUMENTS["XAU_USD"], [sig], [], TUE))
    pos = eng.broker.positions("aggressive")
    assert len(pos) == 1 and pos[0]["ai_reason"].startswith("[AI would skip: AI SKIP: into resistance]")
    assert [c[1] for c in eng.ai.calls] == ["fast"]                        # no Sonnet second opinion
    # with the veto on (Standard), the same reply skips the trade
    asyncio.run(eng._choose_and_handle("standard", INSTRUMENTS["XAU_USD"], [gold_signal()], [], TUE))
    assert eng.broker.positions("standard") == []


def test_hypothesis_passes_only_on_unseen_year_gain(monkeypatch):
    from app import hypothesis
    monkeypatch.setattr(hypothesis, "load_scan", lambda *a, **k: ([], {}))
    monkeypatch.setattr(hypothesis, "setup_signals", lambda sc, strat, prof, rule=None: {"rule": bool(rule)})
    monkeypatch.setattr(hypothesis, "load_settings", lambda: validate(DEFAULTS))

    def fake(gain2):
        def replay(symbol, candles, sigs, settings, mode):
            r = {"n": 40, "r": 4.0, "y1_n": 20, "y1_r": 3.0, "y2_n": 20, "y2_r": 1.0, "pf": 1.2, "equity": 260,
                 "max_dd": 10, "weeks": 104}
            if sigs["rule"]:
                r = {**r, "n": 32, "y1_r": 4.0, "y2_r": 1.0 + gain2, "r": 5.0 + gain2}
            return r
        return replay

    monkeypatch.setattr(hypothesis, "replay", fake(2.0))
    ok = hypothesis.test("XAU_USD:session_breakout", "skip_hours=13", {"standard": ["XAU_USD:session_breakout"]})
    assert ok["passed"] and ok["styles"] == ["aggressive", "standard"]
    monkeypatch.setattr(hypothesis, "replay", fake(0.3))           # only the in-sample year improved
    bad = hypothesis.test("XAU_USD:session_breakout", "skip_hours=13")
    assert not bad["passed"] and "year 2" in bad["reason"]
    assert hypothesis.test("XAU_USD:session_breakout", "nonsense=1")["reason"] == "rule not understood"
