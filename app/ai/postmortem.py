"""Losing-trade post-mortems (Haiku, ~$0.0005 each, capped per day).

For every losing trade: the plan, the reasons it was taken, the price action before and
during it, the SMC map at entry and the news around it. Haiku answers one question:
was this a GOOD trade that lost (bad luck) or a BAD DECISION (a rule broken, into a level,
against news, late entry...)? Only bad-decision lessons go into the playbook's lessons, so
the bot learns from mistakes instead of from noise.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import smc
from ..db import DB, iso, parse_iso
from ..feeds.prices import load_candles, tf_seconds
from ..instruments import INSTRUMENTS
from ..news.sources import events_between
from ..settings_store import MODE_BLURBS, MODE_LABELS
from .client import AIClient
from .review import current_playbook

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["bad luck", "bad decision", "unclear"]},
        "what_happened": {"type": "string"},
        "mistake": {"type": "string"},
        "lesson": {"type": "string"},
    },
    "required": ["verdict", "what_happened", "mistake", "lesson"],
    "additionalProperties": False,
}

SYSTEM = """You are the floor manager auditing ONE losing paper trade. Judge the DECISION, not the outcome.
- "bad luck": the setup followed the account's rules and playbook, the entry was reasonable, and the market
  simply went the other way (normal: even good strategies lose 50-65% of trades).
- "bad decision": something identifiable was wrong at entry - against the higher-timeframe trend, straight
  into a key level / opposite order block, right before high-impact news, a late or chased entry, a stop
  inside obvious noise, or a playbook rule broken.
- "unclear": not enough information.
what_happened: 1-2 sentences. mistake: the specific error, or "" for bad luck. lesson: one actionable
sentence for the playbook if bad decision, else "". JSON only."""

MAX_PER_DAY = 4


def _bars_text(rows: list[dict], d: int) -> str:
    return "; ".join(f"{datetime.fromtimestamp(r['t'], timezone.utc):%H:%M} "
                     f"{r['o']:.{d}f}/{r['h']:.{d}f}/{r['l']:.{d}f}/{r['c']:.{d}f}" for r in rows)


def build_prompt(db: DB, cfg: dict, trade: dict) -> str:
    inst = INSTRUMENTS[trade["symbol"]]
    d = inst.decimals
    tf = cfg["timeframe"]
    sec = tf_seconds(tf)
    t_open = int(parse_iso(trade["opened_at"]).timestamp())
    t_close = int(parse_iso(trade["closed_at"]).timestamp())
    candles = load_candles(db, trade["symbol"], 1500, tf)
    before = [c for c in candles if c["t"] < t_open][-600:]
    during = [c for c in candles if t_open - sec <= c["t"] <= t_close][:40]
    atr = None
    if len(before) > 20:
        trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(before[-15:], before[-14:])]
        atr = sum(trs) / len(trs)
    smc_text = smc.context_text(before, atr, d) if atr else "n/a"
    dec = db.one("SELECT signal_note, ai_reply, reject_reason FROM decisions WHERE account=? AND symbol=? AND signal=? "
                 "AND accepted=1 AND at <= ? ORDER BY id DESC LIMIT 1",
                 (trade["account"], trade["symbol"], trade["signal"], trade["opened_at"]))
    events = events_between(db, parse_iso(trade["opened_at"]) - timedelta(hours=2), parse_iso(trade["closed_at"]),
                            inst.currencies, ("High", "Medium"))
    ev = "; ".join(f"{e['at'][11:16]} {e['currency']} {e['impact']} {e['title']}" for e in events) or "none"
    return f"""ACCOUNT: {MODE_LABELS[trade['account']]} - {MODE_BLURBS[trade['account']]}
TRADE: {inst.xm} {trade['side']} {trade['lots']} lots, strategy {trade['signal']}, regime {trade.get('regime')}
entry {trade['entry']:.{d}f} stop {trade['stop']:.{d}f} target {trade['target']:.{d}f} -> exit {trade['exit']:.{d}f}
({trade['exit_reason']}), result {trade['pnl_r']:+.2f}R, opened {trade['opened_at']} closed {trade['closed_at']}
WHY IT WAS TAKEN: {dec['signal_note'] if dec else trade.get('ai_reason')}
AI REASON: {trade.get('ai_reason')}
SMC MAP AT ENTRY:
{smc_text}
NEWS / EVENTS AROUND IT: {ev}
LAST 24 x {tf} BEFORE ENTRY (o/h/l/c): {_bars_text(before[-24:], d)}
DURING THE TRADE: {_bars_text(during, d)}
PLAYBOOK:
{current_playbook(db, trade['account'])}
"""


async def run(ai: AIClient, db: DB, cfg: dict, account: str, day: datetime) -> list[dict]:
    d0 = day.strftime("%Y-%m-%d")
    d1 = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    trades = db.all("SELECT t.* FROM trades t LEFT JOIN post_mortems p ON p.trade_id = t.id WHERE t.account=? "
                    "AND t.closed_at >= ? AND t.closed_at < ? AND t.pnl_usd < 0 AND p.id IS NULL "
                    "AND t.exit_reason NOT LIKE 'breakeven%' AND t.exit_reason NOT LIKE 'partial%' "
                    "ORDER BY t.pnl_r LIMIT ?", (account, d0, d1, MAX_PER_DAY))
    out = []
    for t in trades:
        if t["pnl_r"] is None:
            continue
        res = await ai.call_json(cfg, f"post_mortem:{account}", SYSTEM, build_prompt(db, cfg, t), SCHEMA, max_tokens=400)
        if not res.ok or not res.data:
            continue
        db.execute("INSERT OR IGNORE INTO post_mortems(trade_id, account, at, verdict, data) VALUES (?,?,?,?,?)",
                   (t["id"], account, iso(), res.data["verdict"], json.dumps({**res.data, "symbol": t["symbol"],
                                                                              "signal": t["signal"], "r": t["pnl_r"]})))
        if res.data["verdict"] == "bad decision" and res.data["lesson"]:
            db.execute("INSERT INTO lessons(mode, at, day, lesson) VALUES (?,?,?,?)",
                       (account, iso(), d0, f"[post-mortem] {res.data['lesson']}"[:400]))
        out.append({"trade_id": t["id"], **res.data})
    return out
