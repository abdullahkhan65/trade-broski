"""Learning loop: daily review (lessons) and weekly playbook rewrite."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..db import DB, iso
from .client import AIClient

SEED_PLAYBOOK = """1. Only take setups in the direction of the 50 EMA trend.
2. Skip signals within 15 minutes of high-impact news for that currency.
3. Avoid entering right after a large candle - wait for the pullback.
4. Gold and GBP/USD are fast: respect the wider stop, don't tighten it.
5. On BTC, avoid new longs when funding is very positive (crowded longs).
6. When in doubt, skip. Fewer, cleaner trades beat many average ones."""

LESSONS_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["lessons", "summary"],
    "additionalProperties": False,
}

PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {"playbook": {"type": "string"}, "changes": {"type": "string"}},
    "required": ["playbook", "changes"],
    "additionalProperties": False,
}

DAILY_SYSTEM = """You review one day of a paper-trading bot's closed trades and skipped signals.
Write 3-5 short, specific, actionable lessons (one sentence each) - e.g. which setups worked,
which instruments/sessions/news conditions to avoid. Don't invent patterns from 1 trade; say
'not enough data' when that's the truth. Also write a 1-2 sentence summary of the day. JSON only."""

WEEKLY_SYSTEM = """You maintain a trading playbook for a paper-trading bot (gold, forex majors, BTC).
Rewrite the playbook using the current version, this week's lessons and the scorecard.
Keep it under one page: max 12 numbered rules, each one sentence. Keep rules that are still
supported, drop ones contradicted by data, add only rules backed by several trades.
Never add rules that raise risk, leverage or position size - those are fixed in code.
'changes' = one or two sentences on what you changed and why. JSON only."""


def current_playbook(db: DB) -> str:
    row = db.one("SELECT content FROM playbook_versions ORDER BY id DESC LIMIT 1")
    if row:
        return row["content"]
    db.execute("INSERT INTO playbook_versions(at, content, source) VALUES (?,?,?)",
               (iso(), SEED_PLAYBOOK, "seed"))
    return SEED_PLAYBOOK


async def daily_review(ai: AIClient, db: DB, cfg: dict, day: datetime | None = None) -> str:
    day = (day or datetime.now(timezone.utc) - timedelta(days=1)).astimezone(timezone.utc)
    d0 = day.strftime("%Y-%m-%d")
    d1 = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    trades = db.all("SELECT symbol, side, signal, pnl_usd, pnl_r, exit_reason, ai_reason, opened_at, closed_at "
                    "FROM trades WHERE closed_at >= ? AND closed_at < ?", (d0, d1))
    skips = db.all("SELECT symbol, side, signal, reject_reason FROM decisions "
                   "WHERE at >= ? AND at < ? AND accepted=0 LIMIT 25", (d0, d1))
    if not trades and not skips:
        return f"Daily review {d0}: nothing happened."
    user = (f"DAY {d0}\nCLOSED TRADES:\n{json.dumps(trades, default=str)}\n\n"
            f"SKIPPED SIGNALS:\n{json.dumps(skips, default=str)}\n\nCURRENT PLAYBOOK:\n{current_playbook(db)}")
    res = await ai.call_json(cfg, "daily_review", DAILY_SYSTEM, user, LESSONS_SCHEMA, max_tokens=400)
    if not res.ok or not res.data:
        return f"Daily review {d0} skipped: {res.error}"
    for lesson in res.data["lessons"][:5]:
        db.execute("INSERT INTO lessons(at, day, lesson) VALUES (?,?,?)", (iso(), d0, lesson[:400]))
    return f"<b>Daily review {d0}</b>\n{res.data['summary']}\n" + "\n".join(f"• {l}" for l in res.data["lessons"][:5])


async def weekly_playbook(ai: AIClient, db: DB, cfg: dict, scorecard_text: str) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    lessons = [r["lesson"] for r in db.all("SELECT lesson FROM lessons WHERE day >= ? ORDER BY id", (since,))]
    if not lessons:
        return "Weekly playbook: no lessons this week, playbook unchanged."
    user = (f"CURRENT PLAYBOOK:\n{current_playbook(db)}\n\nTHIS WEEK'S LESSONS:\n" +
            "\n".join(f"- {l}" for l in lessons[:30]) + f"\n\nSCORECARD:\n{scorecard_text}")
    res = await ai.call_json(cfg, "weekly_playbook", WEEKLY_SYSTEM, user, PLAYBOOK_SCHEMA, max_tokens=800)
    if not res.ok or not res.data:
        return f"Weekly playbook skipped: {res.error}"
    db.execute("INSERT INTO playbook_versions(at, content, source) VALUES (?,?,?)",
               (iso(), res.data["playbook"][:4000], "weekly_ai"))
    return f"<b>Playbook updated</b>\n{res.data['changes']}"
