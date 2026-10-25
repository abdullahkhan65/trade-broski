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
    "standard": """1. Proven setups (2-year backtest, both years positive): GOLD Asian-range breakout and range reversion;
   US100 London opening-range breakout and order block / FVG retest; US30 London and New York opening-range
   breakouts, order block / FVG retest and H1 trend pullback. Each fires ~once a week or less; together ~4-5 trades a week.
2. All only WITH the H1 trend and not straight into unswept liquidity or an opposite order block.
3. Breakouts: take a clean close beyond the range, not a wick that closes back inside; skip ranges that are already huge.
4. Zone retests: the first touch of a fresh zone that closes back out - never a zone price already sliced through.
5. Skip entries within 30 minutes of high-impact USD news (NFP, CPI, FOMC move gold and indices hardest).
6. Index setups target the next liquidity level and trail 1R behind; let the trail work.
7. Trials are small learning trades - judge them on the same standards, but don't force them.""",
    "moderate": """1. Proven setups: GOLD Asian-range breakout and range reversion; US100 London ORB and OB/FVG retest;
   US30 London/New York ORBs and OB/FVG retest.
2. Prefer breakouts and retests that start after liquidity was taken on the other side and have room to the next pool.
3. Skip anything straight into PDH/PDL, a session high/low or an opposite order block.
4. Avoid entries 15 minutes either side of high-impact news; US data at 13:30 UTC moves US30/US100 sharply.
5. US30 and US100 move together - two positions on both is one bet, be selective.
6. After 2 losses in a row the bot halves risk - keep standards high, don't chase.""",
    "aggressive": """1. Proven setups: GOLD Asian-range breakout and range reversion; US100 London ORB and OB/FVG retest;
   US30 London/NY ORBs and OB/FVG retest.
2. Hunt the clean entry: a strong close beyond the range or out of the zone, not a wick.
3. Never trade the news spike candle itself; momentum after it is fine.
4. Quick management: breakeven at +1R; index trades trail 1R behind and target the next liquidity.
5. US30 and US100 are correlated - don't stack both in the same direction unless both setups are A-grade.
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
You also get recent SKIPPED signals with what they WOULD have done (shadow_result target/stop/expired,
shadow_r in R). Skips by the AI that would have hit the target are mistakes to learn from; skips that
would have hit the stop were good calls - say which patterns the AI should take or keep avoiding.
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
    since3 = (day - timedelta(days=3)).strftime("%Y-%m-%d")
    shadows = db.all("SELECT at, symbol, side, signal, reject_reason, shadow_result, shadow_r FROM decisions "
                     "WHERE account=? AND accepted=0 AND at >= ? AND shadow_result IS NOT NULL "
                     "ORDER BY ABS(shadow_r) DESC LIMIT 20", (mode, since3))
    label = MODE_LABELS[mode]
    if not trades and not skips and not shadows:
        return f"[{label}] {d0}: nothing happened."
    user = (f"{_style(mode)}\n\nDAY {d0}\nCLOSED TRADES:\n{json.dumps(trades, default=str)}\n\n"
            f"SKIPPED SIGNALS:\n{json.dumps(skips, default=str)}\n\n"
            f"RECENT SKIPS WITH WHAT THEY WOULD HAVE DONE (last 3 days):\n{json.dumps(shadows, default=str)}\n\n"
            f"CURRENT PLAYBOOK:\n{current_playbook(db, mode)}")
    res = await ai.call_json(cfg, f"daily_review:{mode}", DAILY_SYSTEM, user, LESSONS_SCHEMA, max_tokens=400)
    if not res.ok or not res.data:
        return f"[{label}] review {d0} skipped: {res.error}"
    for lesson in res.data["lessons"][:5]:
        db.execute("INSERT INTO lessons(mode, at, day, lesson) VALUES (?,?,?,?)", (mode, iso(), d0, lesson[:400]))
    return f"<b>[{label}] review {d0}</b>\n{res.data['summary']}\n" + "\n".join(f"• {l}" for l in res.data["lessons"][:5])


async def weekly_playbook(ai: AIClient, db: DB, cfg: dict, mode: str, scorecard_text: str,
                          active: list[str] | None = None) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    lessons = [r["lesson"] for r in db.all("SELECT lesson FROM lessons WHERE mode=? AND day >= ? ORDER BY id",
                                           (mode, since))]
    label = MODE_LABELS[mode]
    if not lessons:
        return f"[{label}] playbook: no lessons this week, unchanged."
    # the curated active list (coach.curate_lessons) first - it is the cleaned-up version of the raw lessons
    user = (f"{_style(mode)}\n\nCURRENT PLAYBOOK:\n{current_playbook(db, mode)}\n\n" +
            ("CURATED ACTIVE LESSONS:\n" + "\n".join(f"- {l}" for l in active) + "\n\n" if active else "") +
            "THIS WEEK'S RAW LESSONS:\n" + "\n".join(f"- {l}" for l in lessons[:30]) + f"\n\nSCORECARD:\n{scorecard_text}")
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
