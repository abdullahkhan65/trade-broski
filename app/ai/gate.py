"""AI gate: Haiku says TAKE or SKIP when a coded signal fires.

The reply is a suggestion only. Side must match the signal, leverage is
clipped to the caps, stop/target multiples are clamped, and the risk engine
recalculates size from the stop distance.
"""
from __future__ import annotations

import json

from ..instruments import Instrument
from ..signals import Signal
from .client import AIClient, AIResult

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["TAKE", "SKIP"]},
        "symbol": {"type": "string"},
        "side": {"type": "string", "enum": ["LONG", "SHORT"]},
        "leverage": {"type": "number"},
        "stop_atr": {"type": "number"},
        "target_atr": {"type": "number"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "symbol", "side", "leverage", "stop_atr", "target_atr", "confidence", "reason"],
    "additionalProperties": False,
}

SYSTEM = """You are the trade gate for a paper-trading bot (virtual money) on gold, forex majors and BTC.
A coded signal has fired. Decide TAKE or SKIP for THIS signal only. You cannot change its side.

Judge:
- Does the recent price action support the signal, or is it chasing/late/into resistance?
- Do upcoming high-impact events or the news digest argue against it?
- Does the playbook (lessons learned from past trades) say to avoid this setup?
- Recent results on this signal type and instrument.

Rules:
- Prefer SKIP when evidence is mixed. Skipping costs nothing; bad trades cost money.
- stop_atr between 1.0 and 3.0; target_atr at least 1.5x stop_atr.
- leverage is only a suggestion; risk is fixed by code.
- confidence is your probability (0-1) that the trade reaches its target before its stop.
- reason: one short sentence a human can read on a dashboard.
Reply with JSON only."""


def _candles_summary(candles: list[dict], decimals: int, n: int = 48) -> str:
    rows = candles[-n:]
    if not rows:
        return "none"
    first, last = rows[0], rows[-1]
    hi, lo = max(c["h"] for c in rows), min(c["l"] for c in rows)
    lines = [f"last {len(rows)} x 15m: open {first['o']:.{decimals}f} -> close {last['c']:.{decimals}f}, "
             f"high {hi:.{decimals}f}, low {lo:.{decimals}f}",
             "closes (oldest->newest, every 2nd candle): " +
             " ".join(f"{c['c']:.{decimals}f}" for c in rows[::2])]
    lines.append("last 6 candles o/h/l/c: " + "; ".join(
        f"{c['o']:.{decimals}f}/{c['h']:.{decimals}f}/{c['l']:.{decimals}f}/{c['c']:.{decimals}f}"
        for c in rows[-6:]))
    return "\n".join(lines)


def build_prompt(inst: Instrument, sig: Signal, candles: list[dict], account: dict,
                 positions: list[dict], recent_trades: list[dict], scorecard_line: str,
                 playbook: str, news: dict, session: str, spread: float | None) -> str:
    d = inst.decimals
    pos_lines = [f"- {p['symbol']} {p['side']} lev {p['leverage']} entry {p['entry']}" for p in positions] or ["- none"]
    trade_lines = [
        f"- {t['symbol']} {t['side']} {t['signal']} {t['pnl_r']:+.2f}R ({t['exit_reason']})"
        if t.get("pnl_r") is not None else f"- {t['symbol']} {t['side']} {t['signal']} ${t['pnl_usd']:+.2f}"
        for t in recent_trades] or ["- none yet"]
    events = news.get("events") or []
    ev_lines = [f"- {e['at'][11:16]} UTC {e['currency']} {e['impact']}: {e['title']}" for e in events] or ["- none in next 4h"]
    heads = news.get("headlines") or []
    head_lines = [f"- {h['title']}" for h in heads[:6]] or ["- none"]
    digest = news.get("digest")
    digest_line = (f"bias {digest.get('bias', 0):+.2f}, risk {digest.get('risk', '?')}: {digest.get('summary', '')}"
                   if digest else "no digest")
    return f"""SIGNAL
instrument: {inst.symbol} ({inst.display})
side: {sig.side}
type: {sig.type}
why: {sig.note}
price: {sig.price:.{d}f}  ATR(14,15m): {sig.atr:.{d}f}  spread: {spread if spread is not None else 'n/a'}
session now: {session}

PRICE ACTION
{_candles_summary(candles, d)}

ACCOUNT
equity ${account['equity']:.2f}, margin used {account['margin_used_pct']:.0f}%, status {account['status']}
open positions:
{chr(10).join(pos_lines)}

LAST 10 TRADES
{chr(10).join(trade_lines)}
scorecard for this signal on this instrument: {scorecard_line}

NEWS & EVENTS
AI news digest for {inst.symbol}: {digest_line}
upcoming events:
{chr(10).join(ev_lines)}
recent headlines:
{chr(10).join(head_lines)}

PLAYBOOK
{playbook or '(empty - no lessons yet)'}
"""


def validate_reply(data: dict, sig: Signal, min_conf: float) -> tuple[bool, str]:
    """Return (take, reason_if_not)."""
    try:
        decision = str(data["decision"]).upper()
        side = str(data["side"]).upper()
        conf = float(data["confidence"])
        float(data["leverage"]), float(data["stop_atr"]), float(data["target_atr"])
    except (KeyError, TypeError, ValueError):
        return False, "AI reply missing fields"
    if decision != "TAKE":
        return False, f"AI SKIP: {data.get('reason', '')}"[:300]
    if side != sig.side:
        return False, "AI tried to flip the side"
    if conf < min_conf:
        return False, f"AI confidence {conf:.2f} < min {min_conf:.2f}"
    return True, ""


async def ask(ai: AIClient, cfg: dict, prompt: str) -> AIResult:
    return await ai.call_json(cfg, "gate", SYSTEM, prompt, SCHEMA, max_tokens=400)


def dumps(data: dict | None) -> str | None:
    return json.dumps(data) if data is not None else None
