"""Weekly research review by Sonnet (Haiku fallback): reads everything the feedback loops
produced this week and writes a short, plain-English review with recommendations.

Input (~2-4k tokens): each account's last-7/30-day results by strategy, the Lab league table,
what skipped signals would have done (by skip reason), strategy-switching changes, tuning runs
and this week's allowlist proposals. Output: summary, recommendations, risks - stored in
research_reviews, shown in the Learning tab and sent to Telegram. It changes nothing by itself.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import promoter, shadow
from ..db import DB, iso
from ..settings_store import MODE_BLURBS, MODE_LABELS
from .client import AIClient, model_for

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommendations", "risks"],
    "additionalProperties": False,
}

SYSTEM = """You are the head of research for a small paper-trading operation (XM-style CFD accounts: gold,
forex majors, BTC; $250 per account). Several accounts trade the same live prices with different rules;
a "Lab" account takes every signal so there is live data on every strategy.
Each week you read the numbers and write a short review for the owner, who is not a quant.

Be concrete and honest:
- Separate evidence from noise: under ~30 trades, say results are not yet meaningful.
- Use the skipped-signal outcomes: if a skip reason (AI veto, cost filter, SMC filter...) would have
  lost money it is protecting the account; if it would have made money it may be too strict.
- Comment on pending allowlist proposals (approve or not, and why).
- Never recommend raising risk to chase returns; recommend changes as concrete settings where possible.
summary: 3-5 sentences. recommendations: up to 6 short bullet points. risks: up to 3. JSON only."""


def _account_block(db: DB, mode: str, since7: str, since30: str) -> str:
    rows = db.all("SELECT signal, symbol, COUNT(*) n, SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) w, "
                  "COALESCE(SUM(pnl_r),0) r, COALESCE(SUM(pnl_usd),0) usd FROM trades WHERE account=? AND closed_at >= ? "
                  "GROUP BY signal, symbol", (mode, since30))
    acc = db.one("SELECT equity, starting_balance, status FROM accounts WHERE id=?", (mode,))
    week = db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) usd FROM trades WHERE account=? AND closed_at >= ?",
                  (mode, since7))
    lines = [f"## {MODE_LABELS[mode]} - equity ${acc['equity']:.2f} (start ${acc['starting_balance']:.0f}), "
             f"status {acc['status']}; this week {week['n']} trades, ${week['usd']:+.2f}",
             f"style: {MODE_BLURBS[mode]}"]
    lines += [f"- 30d {r['symbol']} {r['signal']}: {r['n']} trades, {r['w']} wins, {r['r']:+.1f}R, ${r['usd']:+.2f}"
              for r in rows] or ["- no closed trades in 30 days"]
    sk = shadow.summary(db, mode, days=30)
    if sk:
        lines.append("skipped signals, what they would have done (30d):")
        lines += [f"- {c['category']}: {c['n']} skipped, {c['wins']} would have won, {c['total_r']:+.1f}R total"
                  for c in sk]
    states = db.all("SELECT signal, regime, status, n, avg_r FROM strategy_state WHERE account=? AND status != 'active'",
                    (mode,))
    lines += [f"- strategy switching: {s['signal']} in {s['regime']} is {s['status']} ({s['n']} trades, "
              f"avg {s['avg_r'] or 0:+.2f}R)" for s in states]
    return "\n".join(lines)


def build_input(db: DB, settings: dict) -> str:
    now = datetime.now(timezone.utc)
    since7, since30 = iso(now - timedelta(days=7)), iso(now - timedelta(days=30))
    parts = []
    for mode in promoter.real_modes(settings) + ["lab"]:
        parts.append(_account_block(db, mode, since7, since30))
        if mode != "lab":
            parts.append(f"allowlist: {settings['modes'][mode]['signals'].get('allowlist') or 'everything'}")
    lab = promoter.lab_table(db)
    parts.append("## Lab league table (60d, by total R)")
    parts += [f"- {r['symbol']} {r['signal']}: {r['n']} trades, {r['wins']} wins, avg {r['avg_r']:+.2f}R, "
              f"{r['total_r']:+.1f}R" for r in lab[:15]] or ["- no Lab trades yet"]
    props = db.all("SELECT account, action, item, reason, status FROM proposals WHERE at >= ? ORDER BY id", (since7,))
    parts.append("## Allowlist proposals this week")
    parts += [f"- {p['action']} {p['item']} for {p['account']} ({p['status']}): {p['reason']}" for p in props] or ["- none"]
    runs = db.all("SELECT account, applied, reason FROM tuning_runs WHERE at >= ?", (since7,))
    parts += [f"- tuner {r['account']}: {'applied' if r['applied'] else 'kept'} - {r['reason']}" for r in runs]
    return "\n".join(parts)


async def weekly_review(ai: AIClient, db: DB, settings: dict, cfg: dict) -> str:
    user = build_input(db, settings)
    tier = "strong"
    res = await ai.call_json(cfg, "research_review", SYSTEM, user, SCHEMA, max_tokens=6000, tier="strong",
                             effort="medium")
    if res.unavailable:
        tier = "fast"
        res = await ai.call_json(cfg, "research_review", SYSTEM, user, SCHEMA, max_tokens=900)
    if not res.ok or not res.data:
        return f"Research review skipped: {res.error}"
    db.execute("INSERT INTO research_reviews(at, model, data) VALUES (?,?,?)",
               (iso(), model_for(tier), json.dumps(res.data)))
    d = res.data
    return ("<b>🔬 Weekly research review</b>\n" + d["summary"] + "\n\n" +
            "\n".join(f"• {x}" for x in d["recommendations"]) +
            ("\n\nRisks:\n" + "\n".join(f"• {x}" for x in d["risks"]) if d["risks"] else ""))


def latest(db: DB) -> dict | None:
    row = db.one("SELECT at, model, data FROM research_reviews ORDER BY id DESC LIMIT 1")
    return {"at": row["at"], "model": row["model"], **json.loads(row["data"])} if row else None
