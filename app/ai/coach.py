"""The floor manager's coaching loop (on top of losing-trade post-mortems in postmortem.py).

1. Missed-trade reviews (Haiku): a signal the trader's RULES allowed but the AI skipped, which then hit
   its target. Judged only on what was visible at the time: "fair skip" (the reason was sound - fine,
   even though it would have won) or "too cautious" (the stated reason didn't hold up). Only
   too-cautious lessons are kept. Filter skips (H1, SMC, cost...) are not reviewed here - those are
   rules, judged in bulk by shadow outcomes and the weekly calibration proposals.
2. Winner reviews (Haiku): "good process" (repeat it) or "lucky" (don't learn from it).
3. Coaching note (Haiku, daily): what the trader did well, what to fix, one focus for tomorrow - from the
   audit report, the day's post-mortems and reviews. Shown on the Floor page and sent to Telegram.
4. Lesson curation (Sonnet weekly, Haiku fallback): all raw lessons -> at most 10 active lessons per
   trader; duplicates merged, lessons the results contradict dropped. The active list is what the AI gate
   reads before every trade (next to the playbook), and what the weekly playbook rewrite starts from.

Cost: ~$0.0005 per Haiku review (capped per day), ~$0.02 per Sonnet curation: ~$0.30-0.50 a month.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import shadow, smc
from ..db import DB, iso, parse_iso
from ..feeds.prices import load_candles
from ..instruments import INSTRUMENTS
from ..news.sources import events_between
from ..settings_store import MODE_BLURBS, MODE_LABELS
from .client import AIClient, model_for
from .postmortem import _bars_text
from .review import current_playbook

MAX_MISSED_PER_DAY = 3
MAX_WINNERS_PER_DAY = 2
MAX_ACTIVE_LESSONS = 10
JUDGEMENT_SKIPS = ("Haiku skip", "Haiku low confidence", "Sonnet veto")   # rules allowed it, the AI said no

MISSED_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["fair skip", "too cautious", "unclear"]},
        "what_happened": {"type": "string"},
        "assessment": {"type": "string"},
        "lesson": {"type": "string"},
    },
    "required": ["verdict", "what_happened", "assessment", "lesson"],
    "additionalProperties": False,
}

MISSED_SYSTEM = """You are the floor manager reviewing ONE trade the trader SKIPPED that would have hit its target.
The setup passed all of the account's rules and filters; the AI trader chose not to take it, for the reason given.
Judge the DECISION with only what was visible AT THE TIME - not the outcome. Hindsight is not evidence.
- "fair skip": the stated reason was real and sound (genuine conflict with a key level, news, trend, playbook);
  skipping winners like this is a normal cost of being selective.
- "too cautious": the stated reason does not hold up on the chart and data shown (e.g. the "obstacle" was
  far away or already swept, the trend agreed, the playbook favours this setup) - a clean A-setup was passed up.
- "unclear": not enough information.
what_happened: 1 sentence. assessment: 1-2 sentences on whether the skip reason held up.
lesson: if too cautious, ONE specific sentence naming the condition when this setup SHOULD be taken
(symbol, setup, context) - never a general "take more trades"; else "". JSON only."""

WINNER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good process", "lucky", "unclear"]},
        "what_worked": {"type": "string"},
        "lesson": {"type": "string"},
    },
    "required": ["verdict", "what_worked", "lesson"],
    "additionalProperties": False,
}

WINNER_SYSTEM = """You are the floor manager reviewing ONE winning paper trade. Judge the DECISION, not the result.
- "good process": a clean setup that followed the account's rules and playbook, sensible entry, stop and target.
- "lucky": it won despite a flaw (into a key level, against the trend, around news, chased entry...).
- "unclear": not enough information.
what_worked: 1-2 sentences. lesson: if good process, ONE specific sentence on what to repeat (setup + context);
if lucky, the flaw to avoid next time; else "". JSON only."""

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "well": {"type": "string"},
        "fix": {"type": "string"},
        "focus": {"type": "string"},
    },
    "required": ["well", "fix", "focus"],
    "additionalProperties": False,
}

COACH_SYSTEM = """You are the floor manager writing a short daily coaching note to ONE trader (an AI paper trader
with a defined style). You get yesterday's audit (activity, signal funnel, losing streak, edge vs backtest,
health), the post-mortems of its losing trades, reviews of skipped winners and of winning trades.
Judge process, not single results. Be specific (symbols, setups, sessions, skip reasons) and brief.
If nothing happened because the market was quiet, say so - no trades on a quiet day is correct.
Never push for more risk or more trades for their own sake.
well: 1 sentence. fix: 1 sentence ("" if nothing). focus: one concrete thing for today. JSON only."""

CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": {"type": "string"}},
        "dropped": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lessons", "dropped"],
    "additionalProperties": False,
}

CURATE_SYSTEM = f"""You curate the active lesson list for ONE paper-trading account. The trader's AI reads this
list before every trade, so it must be short, specific, consistent and backed by evidence.
Input: the account style, the current active lessons, all new raw lessons (from daily reviews, losing-trade
post-mortems, skipped-winner reviews and winner reviews) and the evidence: results by setup and what
skipped signals would have done by skip reason.
- Merge duplicates; keep the most specific wording (symbol, setup, session/context).
- Drop lessons contradicted by the evidence, lessons from a single trade with no support, and vague ones.
- If AI skips are SAVING money overall, don't keep lessons that loosen the AI's standards; if they are
  costing winners, keep the specific "take this when..." lessons.
- Never add lessons about raising risk %, leverage or lot size.
lessons: at most {MAX_ACTIVE_LESSONS}, one sentence each, most important first.
dropped: short notes "lesson - why dropped" (max 8). JSON only."""


# ------------------------------------------------------------------ helpers
def _atr(rows: list[dict]) -> float | None:
    if len(rows) < 16:
        return None
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(rows[-15:], rows[-14:])]
    return sum(trs) / len(trs)


def _context(db: DB, symbol: str, tf: str, t_from: datetime, t_to: datetime) -> tuple[str, str, str, str]:
    """(smc map at t_from, last 24 bars before, bars from t_from to t_to, news) - what was visible / what followed."""
    inst = INSTRUMENTS[symbol]
    d = inst.decimals
    t0, t1 = int(t_from.timestamp()), int(t_to.timestamp())
    candles = load_candles(db, symbol, 2000, tf)
    before = [c for c in candles if c["t"] < t0][-600:]
    after = [c for c in candles if t0 <= c["t"] <= t1][:40]
    atr = _atr(before)
    smc_text = smc.context_text(before, atr, d) if atr else "n/a"
    events = events_between(db, t_from - timedelta(hours=2), t_to, inst.currencies, ("High", "Medium"))
    ev = "; ".join(f"{e['at'][11:16]} {e['currency']} {e['impact']} {e['title']}" for e in events) or "none"
    return smc_text, _bars_text(before[-24:], d), _bars_text(after, d), ev


def _save(db: DB, account: str, kind: str, ref_id: int, verdict: str, data: dict) -> None:
    db.execute("INSERT OR IGNORE INTO coach_reviews(account, kind, ref_id, at, verdict, data) VALUES (?,?,?,?,?,?)",
               (account, kind, ref_id, iso(), verdict, json.dumps(data)))


def _lesson(db: DB, account: str, day: str, tag: str, text: str) -> None:
    if text.strip():
        db.execute("INSERT INTO lessons(mode, at, day, lesson) VALUES (?,?,?,?)",
                   (account, iso(), day, f"[{tag}] {text.strip()}"[:400]))


# ------------------------------------------------------------------ 1. skipped winners
def missed_candidates(db: DB, account: str, now: datetime, limit: int = MAX_MISSED_PER_DAY) -> list[dict]:
    """Resolved AI-judgement skips that would have hit the target, not yet reviewed (last 4 days)."""
    rows = db.all("SELECT d.* FROM decisions d LEFT JOIN coach_reviews c ON c.kind='missed' AND c.ref_id=d.id "
                  "WHERE d.account=? AND d.accepted=0 AND d.shadow_result='target' AND d.at >= ? AND c.id IS NULL "
                  "ORDER BY d.shadow_r DESC", (account, iso(now - timedelta(days=4))))
    return [r for r in rows if shadow.category(r["reject_reason"]) in JUDGEMENT_SKIPS][:limit]


def missed_prompt(db: DB, d: dict) -> str:
    inst = INSTRUMENTS[d["symbol"]]
    p = inst.decimals
    at = parse_iso(d["at"])
    end = parse_iso(d["shadow_at"]) if d.get("shadow_at") else at + timedelta(hours=12)
    smc_text, before, after, ev = _context(db, d["symbol"], d.get("plan_tf") or "15m", at, end)
    reply = d.get("ai_reply") or ""
    return f"""ACCOUNT: {MODE_LABELS[d['account']]} - {MODE_BLURBS[d['account']]}
SKIPPED SETUP: {inst.xm} {d['side']} {d['signal']} (regime {d.get('regime')}) at {d['at']}
plan: entry {d['plan_entry']:.{p}f} stop {d['plan_stop']:.{p}f} target {d['plan_target']:.{p}f}
WHY THE SIGNAL FIRED: {d.get('signal_note')}
WHY IT WAS SKIPPED: {d['reject_reason']}
AI REPLY AT THE TIME: {reply[:800]}
SMC MAP AT THE TIME:
{smc_text}
NEWS / EVENTS AROUND IT: {ev}
LAST 24 BARS BEFORE (o/h/l/c): {before}
WHAT FOLLOWED (hit target, {d.get('shadow_r') or 0:+.2f}R): {after}
PLAYBOOK:
{current_playbook(db, d['account'])}
"""


async def review_missed(ai: AIClient, db: DB, cfg: dict, account: str, day: datetime) -> list[dict]:
    out = []
    for d in missed_candidates(db, account, day + timedelta(days=1)):
        res = await ai.call_json(cfg, f"missed_review:{account}", MISSED_SYSTEM, missed_prompt(db, d), MISSED_SCHEMA,
                                 max_tokens=400)
        if not res.ok or not res.data:
            continue
        r = res.data
        _save(db, account, "missed", d["id"], r["verdict"], {**r, "symbol": d["symbol"], "signal": d["signal"],
                                                             "side": d["side"], "r": d["shadow_r"],
                                                             "skip_reason": d["reject_reason"], "at_signal": d["at"]})
        if r["verdict"] == "too cautious":
            _lesson(db, account, day.strftime("%Y-%m-%d"), "missed trade", r["lesson"])
        out.append({"decision_id": d["id"], **r})
    return out


# ------------------------------------------------------------------ 2. winners
def winner_candidates(db: DB, account: str, day: datetime, limit: int = MAX_WINNERS_PER_DAY) -> list[dict]:
    d0, d1 = day.strftime("%Y-%m-%d"), (day + timedelta(days=1)).strftime("%Y-%m-%d")
    return db.all("SELECT t.* FROM trades t LEFT JOIN coach_reviews c ON c.kind='winner' AND c.ref_id=t.id "
                  "WHERE t.account=? AND t.closed_at >= ? AND t.closed_at < ? AND t.pnl_r >= 0.5 AND c.id IS NULL "
                  "ORDER BY t.pnl_r DESC LIMIT ?", (account, d0, d1, limit))


def winner_prompt(db: DB, cfg: dict, t: dict) -> str:
    inst = INSTRUMENTS[t["symbol"]]
    p = inst.decimals
    smc_text, before, during, ev = _context(db, t["symbol"], cfg["timeframe"], parse_iso(t["opened_at"]),
                                            parse_iso(t["closed_at"]))
    return f"""ACCOUNT: {MODE_LABELS[t['account']]} - {MODE_BLURBS[t['account']]}
TRADE: {inst.xm} {t['side']} {t['lots']} lots, strategy {t['signal']}, regime {t.get('regime')}{' (trial)' if t.get('trial') else ''}
entry {t['entry']:.{p}f} stop {t['stop']:.{p}f} target {t['target']:.{p}f} -> exit {t['exit']:.{p}f} ({t['exit_reason']}),
result {t['pnl_r']:+.2f}R
AI REASON: {t.get('ai_reason')}
SMC MAP AT ENTRY:
{smc_text}
NEWS / EVENTS AROUND IT: {ev}
LAST 24 BARS BEFORE ENTRY: {before}
DURING THE TRADE: {during}
PLAYBOOK:
{current_playbook(db, t['account'])}
"""


async def review_winners(ai: AIClient, db: DB, cfg: dict, account: str, day: datetime) -> list[dict]:
    out = []
    for t in winner_candidates(db, account, day):
        res = await ai.call_json(cfg, f"winner_review:{account}", WINNER_SYSTEM, winner_prompt(db, cfg, t), WINNER_SCHEMA,
                                 max_tokens=350)
        if not res.ok or not res.data:
            continue
        r = res.data
        _save(db, account, "winner", t["id"], r["verdict"], {**r, "symbol": t["symbol"], "signal": t["signal"],
                                                             "side": t["side"], "r": t["pnl_r"]})
        if r["verdict"] in ("good process", "lucky"):
            _lesson(db, account, day.strftime("%Y-%m-%d"), "winner" if r["verdict"] == "good process" else "lucky win",
                    r["lesson"])
        out.append({"trade_id": t["id"], **r})
    return out


# ------------------------------------------------------------------ 3. daily coaching note
def coach_prompt(db: DB, account: str, report: dict, day: datetime) -> str:
    d0, d1 = day.strftime("%Y-%m-%d"), (day + timedelta(days=1)).strftime("%Y-%m-%d")
    f = report["funnel"]
    lines = [f"TRADER: {MODE_LABELS[account]} - {MODE_BLURBS[account]}", f"DAY {d0}",
             "AUDIT FINDINGS:"] + [f"- [{i['level']}] {i['area']}: {i['message']}" for i in report["items"]]
    lines.append(f"FUNNEL: {f['checked']} candles checked, {f['signals']} setups, {f['opened']} trades; "
                 f"filtered: {', '.join(x['reason'] + ' x' + str(x['n']) for x in f['filtered'][:5]) or 'none'}; "
                 f"blocked: {', '.join(x['reason'] + ' x' + str(x['n']) for x in f['blocked'][:5]) or 'none'}")
    trades = db.all("SELECT symbol, side, signal, pnl_r, exit_reason, COALESCE(trial,0) trial FROM trades "
                    "WHERE account=? AND closed_at >= ? AND closed_at < ?", (account, d0, d1))
    lines.append("CLOSED TRADES: " + ("; ".join(f"{t['symbol']} {t['side']} {t['signal']} {t['pnl_r'] or 0:+.2f}R "
                                                f"({t['exit_reason']}{', trial' if t['trial'] else ''})" for t in trades)
                                      or "none"))
    for p in db.all("SELECT verdict, data FROM post_mortems WHERE account=? AND at >= ?", (account, d1)):
        x = json.loads(p["data"])
        lines.append(f"LOSS REVIEW ({p['verdict']}): {x.get('symbol')} {x.get('signal')} - {x.get('mistake') or x.get('what_happened')}")
    for c in db.all("SELECT kind, verdict, data FROM coach_reviews WHERE account=? AND at >= ? AND kind IN ('missed','winner')",
                    (account, d1)):
        x = json.loads(c["data"])
        what = x.get("assessment") or x.get("what_worked") or ""
        lines.append(f"{'SKIPPED WINNER' if c['kind'] == 'missed' else 'WIN'} REVIEW ({c['verdict']}): "
                     f"{x.get('symbol')} {x.get('signal')} - {what}")
    sk = shadow.summary(db, account, days=7)
    if sk:
        lines.append("SKIPS LAST 7 DAYS, WHAT THEY WOULD HAVE DONE: " +
                     "; ".join(f"{c['category']} {c['n']} skipped, {c['total_r']:+.1f}R" for c in sk))
    return "\n".join(lines)


async def coaching_note(ai: AIClient, db: DB, cfg: dict, account: str, report: dict, day: datetime) -> dict | None:
    res = await ai.call_json(cfg, f"coach:{account}", COACH_SYSTEM, coach_prompt(db, account, report, day), COACH_SCHEMA,
                             max_tokens=300)
    return res.data if res.ok and res.data else None


# ------------------------------------------------------------------ 4. weekly lesson curation
def active_lessons(db: DB, account: str) -> list[str]:
    row = db.one("SELECT data FROM lesson_sets WHERE account=? ORDER BY id DESC LIMIT 1", (account,))
    return json.loads(row["data"])["lessons"] if row else []


def curate_prompt(db: DB, account: str, scorecard_text: str) -> str:
    last = db.one("SELECT at FROM lesson_sets WHERE account=? ORDER BY id DESC LIMIT 1", (account,))
    since = last["at"] if last else iso(datetime.now(timezone.utc) - timedelta(days=30))
    raw = [r["lesson"] for r in db.all("SELECT lesson FROM lessons WHERE mode=? AND at > ? ORDER BY id DESC LIMIT 60",
                                       (account, since))]
    sk = shadow.summary(db, account, days=30)
    return "\n".join([
        f"ACCOUNT: {MODE_LABELS[account]} - {MODE_BLURBS[account]}",
        "CURRENT ACTIVE LESSONS:", *([f"- {x}" for x in active_lessons(db, account)] or ["- none"]),
        "NEW RAW LESSONS:", *([f"- {x}" for x in raw] or ["- none"]),
        "RESULTS BY SETUP:", scorecard_text or "none",
        "SKIPPED SIGNALS, WHAT THEY WOULD HAVE DONE (30d):",
        *([f"- {c['category']}: {c['n']} skipped, {c['wins']} would have won, {c['total_r']:+.1f}R ({c['verdict']})"
           for c in sk] or ["- none resolved"]),
    ])


async def curate_lessons(ai: AIClient, db: DB, cfg: dict, account: str, scorecard_text: str) -> str:
    label = MODE_LABELS[account]
    last = db.one("SELECT at FROM lesson_sets WHERE account=? ORDER BY id DESC LIMIT 1", (account,))
    if last and not db.scalar("SELECT COUNT(*) FROM lessons WHERE mode=? AND at > ?", (account, last["at"])):
        return f"[{label}] lessons: nothing new, active list unchanged."
    if not last and not db.scalar("SELECT COUNT(*) FROM lessons WHERE mode=?", (account,)):
        return f"[{label}] lessons: none yet."
    user = curate_prompt(db, account, scorecard_text)
    tier = "strong"
    res = await ai.call_json(cfg, f"curate_lessons:{account}", CURATE_SYSTEM, user, CURATE_SCHEMA,
                             max_tokens=4000, tier="strong", effort="low")
    if res.unavailable:
        tier = "fast"
        res = await ai.call_json(cfg, f"curate_lessons:{account}", CURATE_SYSTEM, user, CURATE_SCHEMA, max_tokens=700)
    if not res.ok or not res.data:
        return f"[{label}] lesson curation skipped: {res.error}"
    data = {"lessons": [x[:300] for x in res.data["lessons"][:MAX_ACTIVE_LESSONS]], "dropped": res.data["dropped"][:8],
            "model": model_for(tier)}
    db.execute("INSERT INTO lesson_sets(account, at, data) VALUES (?,?,?)", (account, iso(), json.dumps(data)))
    return (f"<b>[{label}] active lessons</b> ({len(data['lessons'])})\n" + "\n".join(f"• {x}" for x in data["lessons"]) +
            (f"\nDropped {len(data['dropped'])}." if data["dropped"] else ""))


def gate_text(db: DB, account: str) -> str:
    """Playbook + the curated active lessons - what the AI gate reads before every trade."""
    pb = current_playbook(db, account)
    lessons = active_lessons(db, account)
    if not lessons:
        return pb
    return pb + "\n\nACTIVE LESSONS (from reviewing this account's own trades and skips):\n" + \
        "\n".join(f"- {x}" for x in lessons)


# ------------------------------------------------------------------ daily entry point
async def run_daily(ai: AIClient, db: DB, cfg: dict, account: str, report: dict, day: datetime) -> dict:
    missed = await review_missed(ai, db, cfg, account, day)
    winners = await review_winners(ai, db, cfg, account, day)
    note = await coaching_note(ai, db, cfg, account, report, day)
    return {"missed": missed, "winners": winners, "note": note}
