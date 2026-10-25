"""Weekly outlook (every Sunday before the markets open): a plan for every instrument from global events.

Two Sonnet calls (~$0.15-0.40 a week):
1. research - Sonnet with Anthropic's web search tool scans the week ahead: central banks, US data,
   big-tech earnings, geopolitics/tariffs, crypto news, and what drove last week. Plain-text notes.
2. plan - Sonnet (Haiku fallback) turns the notes + our own calendar, headlines and price structure into a
   per-instrument plan: bias, drivers, key events with times, key levels, which of the account's proven
   setups suit the week, which days to stand aside, risk notes.

Shown on the Floor page and Telegram; a one-line summary per instrument goes into the AI gate's prompt as
CONTEXT (it never overrides the coded rules or the proven-setup default).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import smc
from ..db import DB, iso
from ..feeds.prices import load_candles
from ..instruments import INSTRUMENTS
from ..news.digest import latest_digest
from ..news.sources import events_between, recent_headlines
from ..signals import htf_trend
from .client import AIClient, model_for

RESEARCH_SYSTEM = """You are a macro and markets research analyst preparing a trading desk for the week ahead.
Use web search to find what matters THIS WEEK for: gold (XAU/USD), the Dow (US30), the Nasdaq 100 (US100),
EUR/USD, GBP/USD and Bitcoin. Cover: central-bank meetings and speakers (Fed, ECB, BoE), major US/EU/UK data
(CPI, PCE, NFP, GDP, PMIs, retail sales) with dates and UTC times, big earnings that move the Nasdaq/Dow,
geopolitics, tariffs/trade, fiscal news, crypto-specific events, and what drove each market last week.
Be factual and dated; say when something is uncertain. Notes only (no trading advice), max ~700 words."""

PLAN_SYSTEM = """You are the head trader writing the weekly plan for a small paper-trading desk (XM-style CFDs,
$250 accounts, M15 timeframe). The desk only takes these coded PROVEN setups, each filtered by the H1 trend
and SMC levels: GOLD Asian-range breakout and range reversion; US30 London/New York opening-range
breakouts, order-block/FVG retests and H1 trend pullbacks; US100 London opening-range breakout and order-block/FVG retests.
EURUSD, GBPUSD and BTCUSD only see small trial trades.
From the research notes, the economic calendar, headlines and price structure, write for EACH instrument:
- bias (bullish / bearish / neutral / two-way) and the 1-3 drivers behind it,
- key_events: the scheduled events that can move it, with day and UTC time,
- levels: the important support/resistance from the price structure given,
- plan: 2-3 sentences - which of its setups suit this week's likely conditions (trend vs range) and in
  which direction, and when to be careful,
- caution_days: days/times to stand aside (e.g. "Wed 18:00 UTC FOMC"),
- risk: the main thing that would invalidate the plan.
Be honest about uncertainty - "two-way" is a valid bias. Never suggest raising risk or lot size.
week_summary: 3-4 sentences. global_risks: up to 4. JSON only."""

SYMBOLS = ["XAU_USD", "US30", "US100", "EUR_USD", "GBP_USD", "BTC_USDT"]

SCHEMA = {
    "type": "object",
    "properties": {
        "week_summary": {"type": "string"},
        "global_risks": {"type": "array", "items": {"type": "string"}},
        "instruments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "enum": SYMBOLS},
                    "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral", "two-way"]},
                    "drivers": {"type": "array", "items": {"type": "string"}},
                    "key_events": {"type": "array", "items": {"type": "string"}},
                    "levels": {"type": "array", "items": {"type": "string"}},
                    "plan": {"type": "string"},
                    "caution_days": {"type": "array", "items": {"type": "string"}},
                    "risk": {"type": "string"},
                },
                "required": ["symbol", "bias", "drivers", "key_events", "levels", "plan", "caution_days", "risk"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["week_summary", "global_risks", "instruments"],
    "additionalProperties": False,
}


def week_of(now: datetime) -> str:
    """The Monday the outlook is for (on Sunday: tomorrow)."""
    monday = (now + timedelta(days=1)).date() if now.weekday() == 6 else (now - timedelta(days=now.weekday())).date()
    return monday.isoformat()


def _structure(db: DB, sym: str, cfg: dict) -> str:
    inst = INSTRUMENTS[sym]
    d = inst.decimals
    rows = load_candles(db, sym, 2000, "15m")
    if len(rows) < 100:
        return f"{inst.xm}: not enough price history"
    last = rows[-1]["c"]
    cut = rows[-1]["t"] - 7 * 86400
    week = [r for r in rows if r["t"] >= cut]
    prev = [r for r in rows if cut - 7 * 86400 <= r["t"] < cut]
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(rows[-15:], rows[-14:])]
    atr = sum(trs) / len(trs)
    lv = [x for x in smc.key_levels(rows, atr) if not x.swept and (x.name.startswith(("PD", "equal")) or
                                                                     "Asian" in x.name or "London" in x.name)]
    lv_txt = ", ".join(f"{x.name} {x.price:.{d}f}" for x in sorted(lv, key=lambda x: x.price)[:8]) or "none"
    wk = (f"last week {min(r['l'] for r in week):.{d}f}-{max(r['h'] for r in week):.{d}f}, "
          f"open {week[0]['o']:.{d}f} close {week[-1]['c']:.{d}f} ({100 * (week[-1]['c'] / week[0]['o'] - 1):+.2f}%)"
          if week else "no data last week")
    pw = f"; week before {min(r['l'] for r in prev):.{d}f}-{max(r['h'] for r in prev):.{d}f}" if prev else ""
    return (f"{inst.xm}: last {last:.{d}f}; {wk}{pw}; H1 trend {htf_trend(rows, cfg) or 'n/a'}; "
            f"M15 ATR {atr:.{d}f}; unswept levels: {lv_txt}")


def build_inputs(db: DB, cfg: dict, now: datetime) -> tuple[str, str]:
    """(research request, plan context)."""
    end = now + timedelta(days=7)
    events = events_between(db, now, end, ("USD", "EUR", "GBP", "CNY", "JPY"), ("High", "Medium"))
    ev = "\n".join(f"- {e['at'][:16].replace('T', ' ')} UTC {e['currency']} {e['impact']}: {e['title']}"
                   for e in events[:60]) or "- (calendar empty)"
    heads = recent_headlines(db, None, hours=7 * 24, limit=60)
    hd = "\n".join(f"- {h['at'][:10]} {h['title']}" for h in heads) or "- none"
    dig = latest_digest(db, max_age_hours=72)
    struct = "\n".join(_structure(db, s, cfg) for s in SYMBOLS if s in INSTRUMENTS)
    research_req = (f"Week ahead starting {week_of(now)} (today {now:%A %d %b %Y}). Our economic calendar "
                    f"(may be incomplete):\n{ev}\n\nResearch the week ahead for the six markets.")
    context = (f"WEEK STARTING {week_of(now)}\n\nPRICE STRUCTURE (M15 data)\n{struct}\n\n"
               f"ECONOMIC CALENDAR (next 7 days, high/medium impact)\n{ev}\n\n"
               f"HEADLINES (last 7 days)\n{hd}\n\n"
               f"LATEST NEWS DIGEST\n{json.dumps(dig)[:1500] if dig else 'none'}")
    return research_req, context


async def run(ai: AIClient, db: DB, cfg: dict, web_search: bool = True, max_searches: int = 8) -> str:
    now = datetime.now(timezone.utc)
    research_req, context = build_inputs(db, cfg, now)
    notes, searches = "", 0
    if web_search:
        res = await ai.call_research(cfg, "outlook_research", RESEARCH_SYSTEM, research_req, max_searches=max_searches)
        if res.ok and res.data:
            notes, searches = res.data["text"], res.data["searches"]
    user = f"RESEARCH NOTES (web search, {searches} searches)\n{notes or '(no web research this week)'}\n\n{context}"
    tier = "strong"
    res = await ai.call_json(cfg, "outlook_plan", PLAN_SYSTEM, user, SCHEMA, max_tokens=8000, tier="strong",
                             effort="medium")
    if res.unavailable:
        tier = "fast"
        res = await ai.call_json(cfg, "outlook_plan", PLAN_SYSTEM, user, SCHEMA, max_tokens=3000)
    if not res.ok or not res.data:
        return f"Weekly outlook failed: {res.error}"
    data = {**res.data, "searches": searches, "research": notes[:6000]}
    db.execute("INSERT INTO weekly_outlooks(week, at, model, data) VALUES (?,?,?,?)",
               (week_of(now), iso(), model_for(tier), json.dumps(data)))
    return telegram_text(data, week_of(now))


def latest(db: DB) -> dict | None:
    row = db.one("SELECT week, at, model, data FROM weekly_outlooks ORDER BY id DESC LIMIT 1")
    return {"week": row["week"], "at": row["at"], "model": row["model"], **json.loads(row["data"])} if row else None


def gate_line(db: DB, symbol: str) -> str:
    """One line for the AI gate: this week's outlook for the instrument (context, not a rule)."""
    o = latest(db)
    if not o or datetime.now(timezone.utc).date().isoformat() > (
            datetime.fromisoformat(o["week"]) + timedelta(days=7)).date().isoformat():
        return "none"
    x = next((i for i in o["instruments"] if i["symbol"] == symbol), None)
    if not x:
        return "none"
    return (f"bias {x['bias']} ({'; '.join(x['drivers'][:2])}). Plan: {x['plan']} "
            f"Caution: {', '.join(x['caution_days'][:3]) or 'none'}")[:500]


def telegram_text(o: dict, week: str) -> str:
    icon = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪", "two-way": "🟡"}
    lines = [f"🗓 <b>Weekly outlook - week of {week}</b>", o["week_summary"], ""]
    for x in o["instruments"]:
        name = INSTRUMENTS[x["symbol"]].xm if x["symbol"] in INSTRUMENTS else x["symbol"]
        lines.append(f"{icon.get(x['bias'], '')} <b>{name}</b> {x['bias']}: {x['plan']}")
        if x["caution_days"]:
            lines.append(f"   ⚠ {', '.join(x['caution_days'][:3])}")
    if o["global_risks"]:
        lines += ["", "Risks: " + "; ".join(o["global_risks"][:4])]
    return "\n".join(lines)
