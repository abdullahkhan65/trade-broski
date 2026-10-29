"""Weekly hypothesis step: Sonnet (Haiku fallback) proposes rules, the walk-forward replay tests them.

Reads: the loss causes of the last 4 weeks (lab.summary - every account, the Lab most of all), what the
skipped setups would have done, and YEAR 1 of each setup's 2-year replay broken down by hour / day /
regime / side / volatility / ADX (app/hypothesis.py profile). Writes up to MAX_HYPOTHESES rules from a
fixed menu (signals.RULE_KNOBS). Each one is tested on YEAR 2, which the model never saw; only passes
become proposals (Learning tab). ~$0.03-0.06 a week.
"""
from __future__ import annotations

import json

from .. import lab, shadow
from ..db import DB, iso
from ..signals import RULE_KNOBS
from .client import AIClient

MAX_HYPOTHESES = 3

SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "setup": {"type": "string"},
                    "knob": {"type": "string", "enum": list(RULE_KNOBS)},
                    "value": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["setup", "knob", "value", "why"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["hypotheses", "notes"],
    "additionalProperties": False,
}

SYSTEM = f"""You are the quant researcher of a small paper-trading desk. Each week you propose at most
{MAX_HYPOTHESES} simple rules that could stop a coded setup's repeated mistakes. A separate backtest then
tests each rule on a year of data you have NOT seen; most ideas fail, and that's fine.

Rule menu (one knob per hypothesis; value format in brackets):
- skip_hours [comma-separated UTC hours, e.g. 13,14]   - skip_days [Mon,Tue,Wed,Thu,Fri]
- regimes [allowed: trend,range,volatile,mixed]          - sides [LONG or SHORT]
- min_atr_pct / max_atr_pct [volatility percentile 0-100] - min_adx / max_adx [number]

Good hypotheses:
- have a market reason (e.g. "the US open at 13-14 UTC whipsaws gold breakouts", "range fades fail when ADX > 30"),
- target a bucket with enough trades (10+) that clearly loses in year 1 while the rest is fine,
- or follow a live loss cause that repeats (e.g. many "volatile market" losses on one setup),
- keep most of the trades. Never stack several rules on one setup in one week.
Bad: cherry-picking a 3-trade bucket, or a rule that removes half the trades.
"setup" must be copied exactly from the list given. Fewer, better hypotheses beat three weak ones; an empty
list is fine when nothing stands out. notes: 2-3 sentences on what you saw. JSON only."""


def build_input(db: DB, settings: dict, setups: list[str], profiles: dict) -> str:
    s = lab.summary(db, days=28)
    parts = ["## Loss causes, last 4 weeks (all accounts; a loss can have several)"]
    parts += [f"- {c['cause']}: {c['n']} losses, {c['r']:+.1f}R; most on "
              + ", ".join(f"{k} x{v}" for k, v in c["setups"]) for c in s["causes"]] or ["- none tagged yet"]
    for m, v in settings["modes"].items():
        if m == "lab" or not v["enabled"]:
            continue
        sk = shadow.summary(db, m, days=28)
        if sk:
            parts.append(f"## Skipped setups in {m} (28d, what they would have done)")
            parts += [f"- {c['category']}: {c['n']} skipped, {c['wins']} would have won, {c['total_r']:+.1f}R"
                      for c in sk]
        rules = v["signals"].get("setup_rules") or []
        if rules:
            parts.append(f"## Rules already live in {m}: " + "; ".join(rules))
    parts.append("## Setups - YEAR 1 of the 2-year replay (Aggressive account rules), R by bucket")
    for setup in setups:
        p = profiles.get(setup) or {}
        if "error" in p or not p:
            continue
        parts.append(f"### {setup}: year 1 {p['year1']['n']} trades, {p['year1']['r']:+.1f}R")
        for dim, rows in p["breakdown"].items():
            parts.append(f"- {dim}: " + ", ".join(f"{b} {v['n']}tr {v['r']:+.1f}R" for b, v in rows.items()))
    return "\n".join(parts)


async def propose(ai: AIClient, db: DB, settings: dict, cfg: dict, setups: list[str], profiles: dict) -> dict:
    """Ask for hypotheses. Returns {"hypotheses": [...], "notes": str} (empty on failure)."""
    user = build_input(db, settings, setups, profiles)
    res = await ai.call_json(cfg, "hypotheses", SYSTEM, user, SCHEMA, max_tokens=6000, tier="strong", effort="medium")
    if res.unavailable:
        res = await ai.call_json(cfg, "hypotheses", SYSTEM, user, SCHEMA, max_tokens=1200)
    if not res.ok or not res.data:
        return {"hypotheses": [], "notes": f"hypotheses skipped: {res.error}"}
    hyps = [h for h in res.data["hypotheses"] if h["setup"] in setups][:MAX_HYPOTHESES]
    return {"hypotheses": hyps, "notes": res.data["notes"]}


def record(db: DB, setup: str, rule: str, why: str, result: str, detail: dict, proposal_id: int | None) -> int:
    cur = db.execute("INSERT INTO hypotheses(at, setup, rule, why, result, detail, proposal_id) VALUES (?,?,?,?,?,?,?)",
                     (iso(), setup, rule, why, result, json.dumps(detail), proposal_id))
    return int(cur.lastrowid)


def recent(db: DB, limit: int = 20) -> list[dict]:
    rows = db.all("SELECT id, at, setup, rule, why, result, detail, proposal_id FROM hypotheses ORDER BY id DESC LIMIT ?",
                  (limit,))
    for r in rows:
        d = json.loads(r.pop("detail") or "{}")
        r["reason"] = d.get("reason", "")
    return rows
