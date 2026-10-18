"""Coaching loop: skipped-winner reviews, winner reviews, daily note, weekly lesson curation, gate text."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import audit
from app.ai import coach
from app.ai.client import AIResult
from app.db import DB, iso
from app.settings_store import DEFAULTS, MODES, cfg_for, validate


class FakeAI:
    """Answers by purpose prefix; records which tier was asked."""
    def __init__(self, answers, strong_unavailable=False):
        self.answers, self.calls, self.strong_unavailable = answers, [], strong_unavailable

    async def call_json(self, cfg, purpose, system, user, schema, max_tokens=400, tier="fast", effort="low"):
        self.calls.append((purpose.split(":")[0], tier, user))
        if tier == "strong" and self.strong_unavailable:
            return AIResult(False, error="sonnet off", unavailable=True)
        return AIResult(True, data=self.answers[purpose.split(":")[0]])


@pytest.fixture
def db():
    d = DB(":memory:")
    d.ensure_accounts(MODES, 250)
    return d


NOW = datetime.now(timezone.utc)
CFG = cfg_for(validate(DEFAULTS), "moderate")


def add_skip(db, reason, result="target", r=1.5, hours_ago=30):
    cur = db.execute("INSERT INTO decisions(at, account, symbol, signal, side, accepted, reject_reason, signal_note, "
                     "plan_entry, plan_stop, plan_target, plan_tf, shadow_result, shadow_r, shadow_at) "
                     "VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)",
                     (iso(NOW - timedelta(hours=hours_ago)), "moderate", "XAU_USD", "session_breakout", "LONG", reason,
                      "Asian range break", 4000, 3990, 4015, "15m", result, r, iso(NOW - timedelta(hours=hours_ago - 3))))
    return cur.lastrowid


def add_trade(db, r, day):
    t = iso(day.replace(hour=12))
    db.execute("INSERT INTO trades(account, symbol, side, lots, units, leverage, entry, exit, stop, target, pnl_usd, "
               "pnl_r, signal, regime, exit_reason, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("moderate", "XAU_USD", "LONG", 0.01, 1, 1, 4000, 4015, 3990, 4015, r * 10, r, "session_breakout",
                "trend", "take profit" if r > 0 else "stop loss", t, t))


def test_only_ai_judgement_skips_that_won_are_reviewed(db):
    a = add_skip(db, "AI SKIP: into resistance")
    b = add_skip(db, "Sonnet veto (risk 2.5%): AI SKIP: late")
    add_skip(db, "long signal against the H1 trend (down)")          # a rule, not a judgement
    add_skip(db, "AI SKIP: chasing", result="stop", r=-1)            # skip was right
    add_skip(db, "AI SKIP: old", hours_ago=24 * 6)                   # too old
    ids = [d["id"] for d in coach.missed_candidates(db, "moderate", NOW)]
    assert set(ids) == {a, b}


def test_daily_run_saves_reviews_lessons_and_note(db):
    yday = NOW - timedelta(days=1)
    add_skip(db, "AI SKIP: into resistance")
    add_trade(db, 1.5, yday)
    add_trade(db, -1.0, yday)                                        # losers are post-mortem territory
    ai = FakeAI({
        "missed_review": {"verdict": "too cautious", "what_happened": "x", "assessment": "resistance was swept",
                          "lesson": "Take gold Asian breakouts when the nearby high is already swept."},
        "winner_review": {"verdict": "good process", "what_worked": "clean break", "lesson": "Repeat clean closes."},
        "coach": {"well": "patient", "fix": "", "focus": "London open on gold"},
    })
    rep = audit.daily_report(db, validate(DEFAULTS), "moderate", yday)
    out = asyncio.run(coach.run_daily(ai, db, CFG, "moderate", rep, yday))
    assert len(out["missed"]) == 1 and len(out["winners"]) == 1 and out["note"]["focus"]
    lessons = [r["lesson"] for r in db.all("SELECT lesson FROM lessons WHERE mode='moderate'")]
    assert any(l.startswith("[missed trade]") for l in lessons) and any(l.startswith("[winner]") for l in lessons)
    # second run: nothing reviewed twice
    out2 = asyncio.run(coach.run_daily(ai, db, CFG, "moderate", rep, yday))
    assert out2["missed"] == [] and out2["winners"] == []
    assert "SKIPPED WINNER REVIEW (too cautious)" in ai.calls[-1][2]


def test_fair_skip_adds_no_lesson(db):
    add_skip(db, "AI SKIP: CPI in 10 minutes")
    ai = FakeAI({"missed_review": {"verdict": "fair skip", "what_happened": "x", "assessment": "news was real",
                                   "lesson": "should be ignored"}})
    asyncio.run(coach.review_missed(ai, db, CFG, "moderate", NOW - timedelta(days=1)))
    assert db.scalar("SELECT COUNT(*) FROM lessons") == 0
    assert db.scalar("SELECT verdict FROM coach_reviews") == "fair skip"


def test_curation_keeps_max_ten_and_feeds_the_gate(db):
    for i in range(14):
        db.execute("INSERT INTO lessons(mode, at, day, lesson) VALUES (?,?,?,?)", ("moderate", iso(), "2026-10-17", f"l{i}"))
    ai = FakeAI({"curate_lessons": {"lessons": [f"keep {i}" for i in range(13)], "dropped": ["l3 - one trade"]}},
                strong_unavailable=True)
    msg = asyncio.run(coach.curate_lessons(ai, db, CFG, "moderate", "scorecard"))
    assert [c[1] for c in ai.calls] == ["strong", "fast"]            # Sonnet first, Haiku fallback
    assert len(coach.active_lessons(db, "moderate")) == 10 and "active lessons" in msg
    text = coach.gate_text(db, "moderate")
    assert "ACTIVE LESSONS" in text and "keep 0" in text
    assert coach.gate_text(db, "standard") == coach.current_playbook(db, "standard")   # none curated yet
    # nothing new since -> no AI call
    n = len(ai.calls)
    assert "unchanged" in asyncio.run(coach.curate_lessons(ai, db, CFG, "moderate", "scorecard")) and len(ai.calls) == n


def test_prompts_build_without_candles(db):
    did = add_skip(db, "AI SKIP: x")
    d = db.one("SELECT * FROM decisions WHERE id=?", (did,))
    assert "WHY IT WAS SKIPPED: AI SKIP: x" in coach.missed_prompt(db, d)
    add_trade(db, 2.0, NOW - timedelta(days=1))
    t = db.one("SELECT * FROM trades")
    assert "TRADE: GOLD LONG" in coach.winner_prompt(db, CFG, t)


def test_telegram_shows_focus():
    rep = {"account": "moderate", "grade": "green", "items": [], "coach": {"focus": "London open"}}
    assert "focus: London open" in audit.telegram_text([rep])
