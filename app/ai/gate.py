"""AI gate: Haiku says TAKE or SKIP when a coded signal fires.

The reply is a suggestion only. Side must match the signal, leverage is
clipped to the caps, stop/target multiples are clamped, and the risk engine
recalculates size from the stop distance.
"""
from __future__ import annotations

import json

from ..instruments import Instrument
from ..settings_store import MODE_BLURBS, MODE_LABELS
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

SYSTEM = """You are the trade gate for ONE paper-trading account (virtual money) on an XM-style MT5
account trading GOLD, US30/US100, forex majors and BTCUSD. A coded signal has fired. Decide TAKE or SKIP for THIS
signal only. You cannot change its side. Judge it against THIS account's trading style and playbook.

Judge:
- Does the recent price action support the signal, or is it chasing/late/into resistance?
- Smart-money view: is it buying into unswept liquidity above / an opposite order block, or
  entering after liquidity was taken (a sweep) with room to the next pool? Prefer trades that
  start near a fresh FVG/OB in their favour and have clear liquidity to target.
- Do upcoming high-impact events or the news digest argue against it?
- Does this account's playbook (its rules and lessons) say to avoid this setup?
- Does it fit the account's style (e.g. a scalper wants quick momentum; a standard trader wants A-grade setups)?
- Recent results on this signal type and instrument.

Setup status (given below):
- PROVEN = this setup made money in BOTH years of a 2-year backtest, after XM spreads, losers included.
  Default to TAKE. SKIP only for a concrete flaw you can name: entry late/extended (well beyond the
  trigger), straight into an unswept level or opposite order block inside the target, high-impact
  news within the blackout, or a playbook rule it breaks. "Mixed" or "unclear" is NOT a reason to skip
  a proven setup - its losing trades are already in the backtest.
- TRIAL = not yet proven: prefer SKIP when evidence is mixed.

Rules:
- Position size, risk %, the 0.01-lot minimum, leverage and the spread are set and already checked by
  code (cost filter, risk limits). They are NEVER a reason to skip.
- stop_atr and target_atr within the account's limits given below. If a structure stop/target is
  given, use it unless the levels clearly argue otherwise.
- leverage is only a suggestion; risk is fixed by code.
- confidence is your probability (0-1) that the trade reaches its target before its stop.
- reason: one short sentence a human can read on a dashboard.
Reply with JSON only."""


def _candles_summary(candles: list[dict], decimals: int, tf: str = "15m", n: int = 48) -> str:
    rows = candles[-n:]
    if not rows:
        return "none"
    first, last = rows[0], rows[-1]
    hi, lo = max(c["h"] for c in rows), min(c["l"] for c in rows)
    lines = [f"last {len(rows)} x {tf}: open {first['o']:.{decimals}f} -> close {last['c']:.{decimals}f}, "
             f"high {hi:.{decimals}f}, low {lo:.{decimals}f}",
             "closes (oldest->newest, every 2nd candle): " +
             " ".join(f"{c['c']:.{decimals}f}" for c in rows[::2])]
    lines.append("last 6 candles o/h/l/c: " + "; ".join(
        f"{c['o']:.{decimals}f}/{c['h']:.{decimals}f}/{c['l']:.{decimals}f}/{c['c']:.{decimals}f}"
        for c in rows[-6:]))
    return "\n".join(lines)


def build_prompt(inst: Instrument, sig: Signal, candles: list[dict], account: dict,
                 positions: list[dict], recent_trades: list[dict], scorecard_line: str,
                 playbook: str, news: dict, session: str, spread: float | None, cfg: dict,
                 smc_text: str = "") -> str:
    d = inst.decimals
    mode = cfg["mode"]
    e = cfg["exits"]
    pos_lines = [f"- {p['symbol']} {p['side']} {p['lots']:.2f} lots entry {p['entry']}" for p in positions] or ["- none"]
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
    return f"""ACCOUNT
{MODE_LABELS[mode]} - {MODE_BLURBS[mode]}
timeframe: {cfg['timeframe']}; risk per trade {cfg['risk']['risk_per_trade_pct']}%;
stop_atr allowed {e['min_stop_atr']}-{e['max_stop_atr']}; min reward:risk {e['min_reward_risk']}
tested defaults (weekly walk-forward tuning): stop {e['default_stop_atr']} ATR, target {e['default_target_atr']} ATR,
breakeven at {e['breakeven_at_r'] or 'off'}R - prefer these unless the chart clearly argues otherwise

SIGNAL
symbol: {inst.xm}
side: {sig.side}
type: {sig.type}
market regime: {sig.regime}
strategy track record in this regime: {sig.context.get('adaptive', 'no history')}
setup status: {sig.context.get('setup_status', 'TRIAL')}
why: {sig.note}
{f"structure stop {sig.stop_atr} ATR" + (f", target {sig.target_atr} ATR (next liquidity)" if sig.target_atr else "") if sig.stop_atr else ""}
price: {sig.price:.{d}f}  ATR(14,{cfg['timeframe']}): {sig.atr:.{d}f}  spread: {spread if spread is not None else 'n/a'}
session now: {session}

PRICE ACTION
{_candles_summary(candles, d, cfg['timeframe'])}

KEY LEVELS (SMC: liquidity, sweeps, FVG / order blocks; distance from price in ATR)
{smc_text or 'n/a'}

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

PLAYBOOK FOR THIS ACCOUNT (always follow it)
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
    return await ai.call_json(cfg, f"gate:{cfg['mode']}", SYSTEM, prompt, SCHEMA, max_tokens=400)


SECOND_SYSTEM = SYSTEM.replace(
    "A coded signal has fired. Decide TAKE or SKIP",
    "A coded signal has fired and a first reviewer approved it. You are the senior reviewer giving a "
    "SECOND OPINION because this trade is high-stakes (large risk, the 0.01-lot minimum forcing extra risk, "
    "or a borderline first approval). Decide TAKE or SKIP") + """
As the senior reviewer: don't just agree with the first reviewer. Check for a concrete reason this trade
fails - late/extended entry, an unswept level or opposite order block inside the target, news, a playbook
rule it breaks. If you can name one, SKIP and name it. If you can't, TAKE (a PROVEN setup's normal
losers are already in its backtest). The risk size is fixed by the account's rules and is not your call."""


def needs_second_opinion(cfg: dict, risk_pct: float, min_lot_forced: bool, confidence: float) -> str | None:
    """Why this trade deserves Sonnet's look, or None. Only Haiku TAKEs reach here -
    a SKIP costs nothing, so it never needs a second opinion."""
    a = cfg["ai"]
    if not a.get("second_opinion", True):
        return None
    if min_lot_forced:
        return "0.01 lot forces risk above the normal %"
    if risk_pct >= a["second_opinion_risk_pct"]:
        return f"risk {risk_pct:.1f}% >= {a['second_opinion_risk_pct']:g}%"
    if confidence < a["min_confidence"] + 0.1:
        return f"borderline approval ({confidence:.2f})"
    return None


async def second_opinion(ai: AIClient, cfg: dict, prompt: str, first: dict, why: str,
                         risk_pct: float, risk_usd: float) -> AIResult:
    extra = (f"\nFIRST REVIEWER (Haiku) said TAKE, confidence {float(first.get('confidence', 0)):.2f}: "
             f"{first.get('reason', '')}\n"
             f"Risk {risk_pct:.1f}% of equity (${risk_usd:.2f}), set by the account's rules - not a reason to skip. "
             f"Why you are asked: {why}.\n")
    return await ai.call_json(cfg, f"second_opinion:{cfg['mode']}", SECOND_SYSTEM, prompt + extra, SCHEMA,
                              max_tokens=3000, tier="strong", effort="low")


def dumps(data: dict | None) -> str | None:
    return json.dumps(data) if data is not None else None
