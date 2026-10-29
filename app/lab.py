"""The Lab as the learning account: it never stops trading, and every loss gets a cause.

* SEASONS: the Lab takes every signal, so it loses money by design (that's the price of data). When it
  runs low (below REFILL_BELOW of its start balance, or the weekly pause / kill switch hits) and has no
  open position, it is refilled to the start balance and a new season begins. Seasons and the total
  refilled are logged, so its results are never hidden: season P&L and all-time P&L (refills counted
  as losses) are both on the Floor page.
* LOSS CAUSES (every account, free, no AI): each losing trade is tagged from the signal's features at
  entry and how the trade went - against the H1 trend, into a level / order block, volatile or dead
  market, news nearby, gave back +1R, wrong from the start, late session. The weekly hypothesis step
  (ai/hypothesis.py) counts which causes repeat and proposes rules against them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import DB, iso, parse_iso

LAB = "lab"
REFILL_BELOW = 0.6               # refill when equity < 60% of the start balance
LATE_HOUR_UTC = 19


def loss_causes(f: dict | None, trade: dict, news_near: bool = False) -> list[str]:
    """Why a losing trade lost, as tags. `f` = the signal's features at entry (decisions.features)."""
    f = f or {}
    out = []
    side, h1 = trade.get("side"), f.get("h1")
    if h1 in ("up", "down") and {"up": "LONG", "down": "SHORT"}[h1] != side:
        out.append("against H1 trend")
    elif h1 == "flat":
        out.append("H1 flat")
    if f.get("smc"):
        out.append("into level/OB")
    if f.get("regime") == "volatile":
        out.append("volatile market")
    elif f.get("atr_pct") is not None and f["atr_pct"] < 30:
        out.append("quiet market")
    if news_near:
        out.append("news nearby")
    mfe = trade.get("mfe_r")
    if mfe is not None and mfe >= 1.0:
        out.append("gave back +1R")
    try:
        mins = (parse_iso(trade["closed_at"]) - parse_iso(trade["opened_at"])).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        mins = None
    if mins is not None and mins <= 30 and (mfe or 0) < 0.3:
        out.append("wrong from the start")
    if f.get("hour") is not None and f["hour"] >= LATE_HOUR_UTC:
        out.append("late session")
    return out or ["normal loss (no flag)"]


def tag_trade(db: DB, trade: dict, currencies: tuple[str, ...]) -> str | None:
    """Cause tags for a closed trade (None for winners)."""
    if (trade.get("pnl_usd") or 0) >= 0:
        return None
    f = None
    if trade.get("decision_id"):
        raw = db.scalar("SELECT features FROM decisions WHERE id=?", (trade["decision_id"],))
        f = json.loads(raw) if raw else None
    news = False
    try:
        t0 = parse_iso(trade["opened_at"])
        if currencies:
            q = ",".join("?" * len(currencies))
            news = bool(db.scalar(
                f"SELECT COUNT(*) FROM calendar_events WHERE impact='High' AND currency IN ({q}) AND at BETWEEN ? AND ?",
                (*currencies, iso(t0 - timedelta(minutes=60)), iso(t0 + timedelta(minutes=90)))))
    except Exception:  # noqa: BLE001
        news = False
    return ",".join(loss_causes(f, trade, news))


# ------------------------------------------------------------------ seasons
def current_season(db: DB) -> dict:
    row = db.one("SELECT * FROM lab_seasons WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    if row:
        return row
    acc = db.one("SELECT starting_balance, started_at FROM accounts WHERE id=?", (LAB,))
    start = acc["starting_balance"] if acc else 250.0
    db.execute("INSERT INTO lab_seasons(started_at, start_equity) VALUES (?,?)",
               (acc["started_at"] if acc else iso(), start))
    return db.one("SELECT * FROM lab_seasons WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")


def needs_refill(equity: float, start: float, status: str, open_positions: int) -> str | None:
    if open_positions:
        return None
    if status in ("halted", "paused_weekly"):
        return f"Lab {status.replace('_', ' ')}"
    if equity < REFILL_BELOW * start:
        return f"Lab equity ${equity:.2f} below {100 * REFILL_BELOW:.0f}% of ${start:.2f}"
    return None


def refill(db: DB, equity: float, reason: str) -> dict:
    """Close this season, top the Lab's cash back up to its start balance, start the next season."""
    season = current_season(db)
    acc = db.one("SELECT cash, starting_balance FROM accounts WHERE id=?", (LAB,))
    start = acc["starting_balance"]
    added = start - acc["cash"]
    db.execute("UPDATE lab_seasons SET ended_at=?, end_equity=?, reason=? WHERE id=?",
               (iso(), equity, reason, season["id"]))
    db.execute("UPDATE accounts SET cash=?, equity=?, peak=?, status='active', status_reason=NULL WHERE id=?",
               (start, start, start, LAB))
    db.kv_set(f"day_start_equity:{LAB}", start)
    db.kv_set(f"week_start_equity:{LAB}", start)
    db.kv_set("lab_refilled_usd", float(db.kv_get("lab_refilled_usd", 0.0) or 0.0) + added)
    db.execute("INSERT INTO lab_seasons(started_at, start_equity) VALUES (?,?)", (iso(), start))
    return {"season": season["id"], "added": round(added, 2), "reason": reason, "next": season["id"] + 1}


def summary(db: DB, days: int = 7) -> dict:
    """Season, all-time P&L with refills, and loss causes over the last `days` (Lab and real accounts)."""
    season = current_season(db)
    acc = db.one("SELECT equity, starting_balance FROM accounts WHERE id=?", (LAB,)) or {"equity": 0, "starting_balance": 250}
    refilled = float(db.kv_get("lab_refilled_usd", 0.0) or 0.0)
    n_seasons = int(db.scalar("SELECT COUNT(*) FROM lab_seasons") or 1)
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    causes: dict[str, dict] = {}
    by_acc: dict[str, dict[str, int]] = {}
    for t in db.all("SELECT account, signal, symbol, pnl_r, causes FROM trades WHERE closed_at >= ? AND pnl_usd < 0 "
                    "AND causes IS NOT NULL", (since,)):
        for c in t["causes"].split(","):
            row = causes.setdefault(c, {"cause": c, "n": 0, "r": 0.0, "setups": {}})
            row["n"] += 1
            row["r"] += t["pnl_r"] or 0
            k = f"{t['symbol']}:{t['signal']}"
            row["setups"][k] = row["setups"].get(k, 0) + 1
            by_acc.setdefault(t["account"], {}).setdefault(c, 0)
            by_acc[t["account"]][c] += 1
    rows = sorted(causes.values(), key=lambda r: -r["n"])
    for r in rows:
        r["r"] = round(r["r"], 1)
        r["setups"] = sorted(r["setups"].items(), key=lambda kv: -kv[1])[:3]
    season_trades = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(pnl_r),0) AS r, COALESCE(SUM(pnl_usd),0) AS usd "
                           "FROM trades WHERE account=? AND closed_at >= ?", (LAB, season["started_at"]))
    return {"season": n_seasons, "season_started": season["started_at"], "season_trades": season_trades["n"],
            "season_r": round(season_trades["r"], 1), "season_usd": round(season_trades["usd"], 2),
            "equity": round(acc["equity"], 2), "refilled": round(refilled, 2),
            "all_time_usd": round(acc["equity"] - acc["starting_balance"] - refilled, 2),
            "refill_below": round(REFILL_BELOW * acc["starting_balance"], 2),
            "causes": rows, "causes_by_account": by_acc, "days": days}
