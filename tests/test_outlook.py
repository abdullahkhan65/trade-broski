"""Weekly outlook: week dating, the two-step run (research -> plan), storage, gate line, Telegram text."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.ai import outlook
from app.ai.client import AIResult
from app.db import DB
from app.settings_store import DEFAULTS, cfg_for, validate

PLAN = {"week_summary": "Fed week.", "global_risks": ["tariffs"], "instruments": [
    {"symbol": "XAU_USD", "bias": "bearish", "drivers": ["strong dollar", "yields up"], "key_events": ["Wed 18:00 UTC FOMC"],
     "levels": ["4000 support"], "plan": "Favour Asian-range breakout shorts with the H1 trend.",
     "caution_days": ["Wed 18:00 UTC"], "risk": "a dovish Fed"}]}


class FakeAI:
    def __init__(self, research_ok=True):
        self.calls, self.research_ok = [], research_ok

    async def call_research(self, cfg, purpose, system, user, max_searches=8, **k):
        self.calls.append(("research", user))
        return AIResult(True, data={"text": "FOMC Wednesday; CPI Thursday.", "searches": 5}) if self.research_ok \
            else AIResult(False, error="off", unavailable=True)

    async def call_json(self, cfg, purpose, system, user, schema, max_tokens=400, tier="fast", effort="low"):
        self.calls.append(("plan", user))
        return AIResult(True, data=PLAN)


def test_week_of_is_the_coming_monday_on_sunday():
    assert outlook.week_of(datetime(2026, 10, 25, 18, tzinfo=timezone.utc)) == "2026-10-26"
    assert outlook.week_of(datetime(2026, 10, 28, 9, tzinfo=timezone.utc)) == "2026-10-26"


def test_run_stores_plan_and_feeds_the_gate():
    db = DB(":memory:")
    ai = FakeAI()
    msg = asyncio.run(outlook.run(ai, db, cfg_for(validate(DEFAULTS), "standard")))
    assert [c[0] for c in ai.calls] == ["research", "plan"]
    assert "FOMC Wednesday" in ai.calls[1][1] and "PRICE STRUCTURE" in ai.calls[1][1]
    o = outlook.latest(db)
    assert o["searches"] == 5 and o["instruments"][0]["bias"] == "bearish"
    assert "Weekly outlook" in msg and "GOLD" in msg
    line = outlook.gate_line(db, "XAU_USD")
    assert line.startswith("bias bearish") and "FOMC" not in line or "Wed 18:00" in line
    assert outlook.gate_line(db, "US30") == "none"


def test_runs_without_web_research():
    db = DB(":memory:")
    ai = FakeAI(research_ok=False)
    asyncio.run(outlook.run(ai, db, cfg_for(validate(DEFAULTS), "standard")))
    assert "(no web research this week)" in ai.calls[-1][1]


def test_old_outlook_is_not_given_to_the_gate():
    db = DB(":memory:")
    old = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    db.execute("INSERT INTO weekly_outlooks(week, at, model, data) VALUES (?,?,?,?)",
               (old, old, "claude-sonnet-5-5", json.dumps({**PLAN, "searches": 0, "research": ""})))
    assert outlook.gate_line(db, "XAU_USD") == "none"
