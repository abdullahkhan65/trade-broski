"""Learning loop, one per mode: daily review (lessons) and weekly playbook rewrite.

Each mode (standard / moderate / aggressive) has its OWN playbook. The AI gate
reads that mode's playbook before every decision, and the daily/weekly jobs
learn only from that mode's own trades.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..db import DB, iso
from ..settings_store import MODE_BLURBS, MODE_LABELS
from .client import AIClient

SEED_PLAYBOOKS = {
    "standard": """1. Only trade in the direction of the 50 EMA trend; never counter-trend.
2. Only A-grade setups: clean pullback to the 20 EMA or a clear first breakout of the Asian range.
3. Skip anything within 30 minutes of high-impact news for that currency.
4. Never enter right after a large candle - wait for the pullback.
5. Let winners run to at least 2R; never move the target closer.
6. Fewer, cleaner trades beat many average ones. When in doubt, SKIP.
7. Protect capital first: the goal is steady growth, not big days.""",
    "moderate": """1. Trade with the 50 EMA trend; counter-trend only on strong news bias with price confirmation.
2. Take B+ setups too, as long as reward:risk is at least 1.5.
3. Use the AI news bias: favour trades in the direction of a fresh, strong catalyst.
4. Avoid entries 15 minutes either side of high-impact news.
5. Gold and GBPUSD move fast - respect the wider stop.
6. After 2 losses in a row on one symbol, skip that symbol for the rest of the session.""",
    "aggressive": """1. Scalp M5 in the direction of the M15/M5 trend during London and New York only.
2. Best windows: London open (07:00-10:00 UTC) and New York open (12:30-15:30 UTC).
3. Quick targets (1.2-1.8R); the stop moves to breakeven at +1R - don't give winners back.
4. Momentum after news is fine once the first spike candle has closed; never trade the spike itself.
5. Skip choppy, flat markets: if the last 12 candles overlap heavily, SKIP.
6. Daily goal +3%: once hit, the account stops for the day. Don't force trades early in the day.
7. Two losses in a row on a symbol = stop trading that symbol until the next session.""",
}

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

DAILY_SYSTEM = """You review one day of ONE paper-trading account's closed trades and skipped signals.
The account's trading style is given; judge trades against that style. Write 3-5 short, specific,
actionable lessons (one sentence each) - which setups worked, which symbols/sessions/news conditions
to avoid. Don't invent patterns from 1 trade; say 'not enough data' when that's true.
Also write a 1-2 sentence summary of the day. JSON only."""

WEEKLY_SYSTEM = """You maintain the trading playbook for ONE paper-trading account (gold, forex majors, BTC
on an XM-style CFD account). Rewrite the playbook from the current version, this week's lessons and
the scorecard, keeping it true to the account's trading style. Max 12 numbered rules, one sentence each.
Keep rules still supported by data, drop ones contradicted by it, add only rules backed by several trades.
Never add rules that raise risk %, leverage or lot size - those are fixed in code.
'changes' = one or two sentences on what you changed and why. JSON only."""


def current_playbook(db: DB, mode: str) -> str:
    row = db.one("SELECT content FROM playbook_versions WHERE mode=? ORDER BY id DESC LIMIT 1", (mode,))
    if row:
        return row["content"]
    seed = SEED_PLAYBOOKS[mode]
    db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
               (mode, iso(), seed, "seed"))
    return seed


def _style(mode: str) -> str:
    return f"ACCOUNT: {MODE_LABELS[mode]}\nSTYLE: {MODE_BLURBS[mode]}"


async def daily_review(ai: AIClient, db: DB, cfg: dict, mode: str, day: datetime | None = None) -> str:
    day = (day or datetime.now(timezone.utc) - timedelta(days=1)).astimezone(timezone.utc)
    d0 = day.strftime("%Y-%m-%d")
    d1 = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    trades = db.all("SELECT symbol, side, lots, signal, pnl_usd, pnl_r, exit_reason, ai_reason, opened_at, closed_at "
                    "FROM trades WHERE account=? AND closed_at >= ? AND closed_at < ?", (mode, d0, d1))
    skips = db.all("SELECT symbol, side, signal, reject_reason FROM decisions "
                   "WHERE account=? AND at >= ? AND at < ? AND accepted=0 LIMIT 25", (mode, d0, d1))
    label = MODE_LABELS[mode]
    if not trades and not skips:
        return f"[{label}] {d0}: nothing happened."
    user = (f"{_style(mode)}\n\nDAY {d0}\nCLOSED TRADES:\n{json.dumps(trades, default=str)}\n\n"
            f"SKIPPED SIGNALS:\n{json.dumps(skips, default=str)}\n\nCURRENT PLAYBOOK:\n{current_playbook(db, mode)}")
    res = await ai.call_json(cfg, f"daily_review:{mode}", DAILY_SYSTEM, user, LESSONS_SCHEMA, max_tokens=400)
    if not res.ok or not res.data:
        return f"[{label}] review {d0} skipped: {res.error}"
    for lesson in res.data["lessons"][:5]:
        db.execute("INSERT INTO lessons(mode, at, day, lesson) VALUES (?,?,?,?)", (mode, iso(), d0, lesson[:400]))
    return f"<b>[{label}] review {d0}</b>\n{res.data['summary']}\n" + "\n".join(f"• {l}" for l in res.data["lessons"][:5])


async def weekly_playbook(ai: AIClient, db: DB, cfg: dict, mode: str, scorecard_text: str) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    lessons = [r["lesson"] for r in db.all("SELECT lesson FROM lessons WHERE mode=? AND day >= ? ORDER BY id",
                                           (mode, since))]
    label = MODE_LABELS[mode]
    if not lessons:
        return f"[{label}] playbook: no lessons this week, unchanged."
    user = (f"{_style(mode)}\n\nCURRENT PLAYBOOK:\n{current_playbook(db, mode)}\n\nTHIS WEEK'S LESSONS:\n" +
            "\n".join(f"- {l}" for l in lessons[:30]) + f"\n\nSCORECARD:\n{scorecard_text}")
    # Sonnet writes the playbook (3 calls a week, the rules every gate call reads);
    # Haiku is the fallback when Sonnet is off or over its budget.
    source = "weekly_sonnet"
    res = await ai.call_json(cfg, f"weekly_playbook:{mode}", WEEKLY_SYSTEM, user, PLAYBOOK_SCHEMA,
                             max_tokens=6000, tier="strong", effort="medium")
    if res.unavailable:
        source = "weekly_haiku"
        res = await ai.call_json(cfg, f"weekly_playbook:{mode}", WEEKLY_SYSTEM, user, PLAYBOOK_SCHEMA, max_tokens=800)
    if not res.ok or not res.data:
        return f"[{label}] playbook skipped: {res.error}"
    db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
               (mode, iso(), res.data["playbook"][:4000], source))
    return f"<b>[{label}] playbook updated</b> ({source.split('_')[1].title()})\n{res.data['changes']}"
