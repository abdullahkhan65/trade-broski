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
    "standard": """1. Main edge (2-year backtest): GOLD's first close beyond the Asian range during London/NY, WITH the H1 trend.
2. Take it only when the breakout candle closes cleanly beyond the range - not a wick poke that closes back inside.
3. Skip if unswept liquidity (PDH/PDL, session high/low) or an opposite order block sits just ahead of the trade.
4. Skip within 30 minutes of high-impact USD news; the breakout must not be the news spike itself.
5. If the Asian range was unusually wide (> 3 ATR) the move is often done - be stricter.
6. Patience: 1-2 trades a week is normal. Never force a trade to hit a daily number.
7. When price stalls short of the target, the bot takes the profit - that is intended.""",
    "moderate": """1. Main edge: GOLD Asian-range breakout during London/NY with the H1 trend (2-year backtest, both years positive).
2. On trial: BTC London opening-range breakout with the H1 trend - be selective, it is only slightly positive.
3. Prefer breakouts that start after liquidity was taken on the other side (a sweep) and have room to the next pool.
4. Skip breakouts straight into PDH/PDL, a session high/low or an opposite order block.
5. Avoid entries 15 minutes either side of high-impact news.
6. After 2 losses in a row the bot halves risk - keep standards high, don't chase.""",
    "aggressive": """1. Edge (2-year backtest): GOLD's first close beyond the Asian range on M15, with the H1 trend.
2. Hunt the clean entry: a strong close beyond the range, not a wick. Late breakouts (> 3 hours into London) are weaker.
3. Skip breakouts straight into unswept liquidity or an opposite order block.
4. Never trade the news spike candle itself; momentum after it is fine.
5. Quick management: breakeven at +1R, profit taken when momentum stalls. Don't give winners back.
6. Risk halves after the day is +4%; done for the day at +6% or -7%.""",
    "lab": """Research account: no AI, takes every signal. This playbook is not used.""",
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
