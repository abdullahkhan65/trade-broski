"""Shadow outcomes: what would a SKIPPED signal have done?

Every decision is stored with the plan it had at that moment (entry, stop, target, timeframe).
For the ones that were NOT taken, this module walks the candles that followed and records
which came first:
  target -> +reward/risk R      stop -> -1R      neither within 48h -> R at the last close
minus the XM spread (in R). Same-candle stop+target counts as a stop (conservative).

Grouped by skip reason, this answers "is the AI veto / cost filter / SMC filter saving us
money or costing us winners?" - shown in the Learning tab, and fed to the daily AI review.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import DB, iso, parse_iso
from .feeds.prices import load_candles, tf_seconds
from .instruments import INSTRUMENTS

EXPIRE_HOURS = 48
LOOKBACK_DAYS = 7

CATEGORIES = [                       # (prefix/substring in reject_reason, category) - first match wins
    ("Sonnet veto", "Sonnet veto"),
    ("Sonnet error", "AI error"),
    ("AI SKIP", "Haiku skip"),
    ("AI confidence", "Haiku low confidence"),
    ("AI error", "AI error"),
    ("AI unavailable", "AI unavailable"),
    ("AI tried", "Haiku skip"),
    ("spread is", "cost filter"),
    ("SMC:", "SMC filter"),
    ("against the H1 trend", "H1 trend filter"),
    ("news", "news filter"),
    ("cooldown", "cooldown after AI skip"),
    ("trades today", "day rules"),
    ("losses in a row", "day rules"),
    ("0.01 lot", "lot size / risk limit"),
    ("leverage", "lot size / risk limit"),
    ("margin", "lot size / risk limit"),
    ("already have", "portfolio limits"),
    ("open positions", "portfolio limits"),
    ("USD forex", "portfolio limits"),
    ("account ", "account paused"),
    ("weekly close", "weekend"),
    ("adaptive:", "strategy switched off"),
    ("signals_only", "signals-only mode"),
]


def category(reason: str | None) -> str:
    r = reason or ""
    for key, cat in CATEGORIES:
        if key in r:
            return cat
    return "other"


def outcome(side: str, entry: float, stop: float, target: float, candles: list[dict], start_t: int,
            end_t: int, spread: float, now_t: float) -> tuple[str, float] | None:
    """Walk candles with start_t <= t < end_t. None while the window is still open and nothing hit."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    cost = spread / risk
    long = side == "LONG"
    last = None
    for c in candles:
        if c["t"] < start_t:
            continue
        if c["t"] >= end_t:
            break
        last = c
        hit_stop = c["l"] <= stop if long else c["h"] >= stop
        hit_target = c["h"] >= target if long else c["l"] <= target
        if hit_stop:
            return "stop", -1.0 - cost
        if hit_target:
            return "target", abs(target - entry) / risk - cost
    if now_t < end_t:
        return None                                       # still playing out
    if last is None:
        return "expired", 0.0                             # market closed the whole window
    move = (last["c"] - entry) if long else (entry - last["c"])
    return "expired", move / risk - cost


def resolve(db: DB, settings: dict, now: datetime | None = None) -> int:
    """Fill in shadow outcomes for skipped decisions whose window has played out. Returns how many."""
    now = now or datetime.now(timezone.utc)
    rows = db.all("SELECT id, at, symbol, side, plan_entry, plan_stop, plan_target, plan_tf FROM decisions "
                  "WHERE accepted=0 AND shadow_result IS NULL AND plan_stop IS NOT NULL AND at >= ? "
                  "ORDER BY id LIMIT 2000", (iso(now - timedelta(days=LOOKBACK_DAYS)),))
    cache: dict[tuple[str, str], list[dict]] = {}
    done = 0
    for r in rows:
        tf = r["plan_tf"] or "15m"
        key = (r["symbol"], tf)
        if key not in cache:
            cache[key] = load_candles(db, r["symbol"], (LOOKBACK_DAYS + 3) * 86400 // tf_seconds(tf), tf)
        inst = INSTRUMENTS[r["symbol"]]
        t0 = int(parse_iso(r["at"]).timestamp())
        spread = settings["broker"].get(f"spread_{r['symbol']}", 0) * inst.point
        res = outcome(r["side"], r["plan_entry"], r["plan_stop"], r["plan_target"], cache[key],
                      t0, t0 + EXPIRE_HOURS * 3600, spread, now.timestamp())
        if res is None:
            continue
        db.execute("UPDATE decisions SET shadow_result=?, shadow_r=?, shadow_at=? WHERE id=?",
                   (res[0], round(res[1], 3), iso(now), r["id"]))
        done += 1
    return done


def summary(db: DB, account: str, days: int = 30) -> list[dict]:
    """Per skip category: how many, how many would have won/lost, total and average R."""
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = db.all("SELECT reject_reason, shadow_result, shadow_r FROM decisions WHERE account=? AND accepted=0 "
                  "AND at >= ? AND shadow_result IS NOT NULL", (account, since))
    out: dict[str, dict] = {}
    for r in rows:
        c = out.setdefault(category(r["reject_reason"]), {"category": category(r["reject_reason"]), "n": 0,
                                                            "wins": 0, "losses": 0, "total_r": 0.0})
        c["n"] += 1
        c["total_r"] += r["shadow_r"]
        if r["shadow_r"] > 0:
            c["wins"] += 1
        else:
            c["losses"] += 1
    for c in out.values():
        c["total_r"] = round(c["total_r"], 1)
        c["avg_r"] = round(c["total_r"] / c["n"], 2)
        # a skip filter is "saving money" when what it skipped would have lost
        c["verdict"] = "saving money" if c["total_r"] < -1 else "costing winners" if c["total_r"] > 1 else "neutral"
    return sorted(out.values(), key=lambda c: c["total_r"])
