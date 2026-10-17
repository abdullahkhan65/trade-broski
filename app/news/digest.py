"""AI news digest: one cheap Haiku call turns the latest headlines + calendar
into a bias (-1 bearish .. +1 bullish) and a risk level per instrument.

Runs every `news.digest_interval_min` minutes, and only if there are new
headlines since the last digest (so a quiet weekend costs nothing).
The digest is used three ways:
1. shown in the AI gate prompt for every signal,
2. powers the optional `news_momentum` signal,
3. risk == "extreme" blocks new trades on that instrument (setting).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..ai.client import AIClient
from ..db import DB, iso, parse_iso
from ..instruments import INSTRUMENTS
from .sources import events_between, fear_greed, recent_headlines

_INST_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "number"},
        "risk": {"type": "string", "enum": ["low", "medium", "high", "extreme"]},
        "summary": {"type": "string"},
    },
    "required": ["bias", "risk", "summary"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "instruments": {
            "type": "object",
            "properties": {s: _INST_SCHEMA for s in INSTRUMENTS},
            "required": list(INSTRUMENTS),
            "additionalProperties": False,
        },
        "market_summary": {"type": "string"},
    },
    "required": ["instruments", "market_summary"],
    "additionalProperties": False,
}

SYSTEM = """You are a macro news analyst for a short-term (hours) trading bot on
XAU_USD (gold), EUR_USD, GBP_USD, USD_JPY and BTC_USDT.
From the headlines, economic calendar and sentiment given, give each instrument:
- bias: -1.0 (strongly bearish for the instrument's price) .. +1.0 (strongly bullish). 0 = no clear read.
  Remember direction: a stronger USD is bearish EUR_USD, GBP_USD and gold, bullish USD_JPY.
- risk: low | medium | high | extreme (extreme = imminent event or shock that makes price unpredictable).
- summary: max 20 words naming the driver.
Be conservative: most of the time bias should be between -0.4 and 0.4. Only go beyond 0.6 for a
clear, fresh, market-moving catalyst. Ignore old or irrelevant headlines. JSON only."""


def latest_digest(db: DB, max_age_hours: float = 3) -> dict | None:
    row = db.one("SELECT at, data FROM news_digests ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    if datetime.now(timezone.utc) - parse_iso(row["at"]) > timedelta(hours=max_age_hours):
        return None
    data = json.loads(row["data"])
    data["at"] = row["at"]
    return data


def bias_for(db: DB, symbol: str) -> dict | None:
    d = latest_digest(db)
    if not d:
        return None
    return d.get("instruments", {}).get(symbol)


async def run_digest(ai: AIClient, db: DB, cfg: dict, force: bool = False) -> str:
    if not (cfg["news"]["enabled"] and cfg["news"]["digest_enabled"]):
        return "news digest disabled"
    last = db.one("SELECT at FROM news_digests ORDER BY id DESC LIMIT 1")
    since = last["at"] if last else iso(datetime.now(timezone.utc) - timedelta(hours=12))
    n_new = db.scalar("SELECT COUNT(*) FROM news_items WHERE at > ?", (since,)) or 0
    if not force and n_new == 0:
        return "no new headlines"

    now = datetime.now(timezone.utc)
    heads = recent_headlines(db, None, hours=8, limit=45)
    events = events_between(db, now - timedelta(hours=6), now + timedelta(hours=24),
                            ("USD", "EUR", "GBP", "JPY", "CNY"), ("High", "Medium"))
    fng = await fear_greed()
    lines = [f"NOW: {iso(now)}", "", "HEADLINES (newest first):"]
    lines += [f"- [{h['at'][5:16]}] {h['title']}" for h in heads]
    lines += ["", "ECONOMIC CALENDAR (-6h .. +24h, UTC):"]
    lines += [f"- {e['at'][5:16]} {e['currency']} {e['impact']}: {e['title']} "
              f"(fcst {e['forecast'] or '-'}, prev {e['previous'] or '-'})" for e in events] or ["- none"]
    if fng:
        lines += ["", f"CRYPTO FEAR & GREED: {fng['value']} ({fng['label']})"]

    res = await ai.call_json(cfg, "news_digest", SYSTEM, "\n".join(lines), SCHEMA, max_tokens=600)
    if not res.ok or not res.data:
        return f"digest failed: {res.error}"
    for v in res.data["instruments"].values():
        v["bias"] = max(-1.0, min(1.0, float(v["bias"])))
    db.execute("INSERT INTO news_digests(at, data) VALUES (?, ?)", (iso(), json.dumps(res.data)))
    db.execute("DELETE FROM news_digests WHERE at < ?", (iso(now - timedelta(days=30)),))
    return res.data["market_summary"]
