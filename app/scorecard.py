"""Scorecard: win rate and average R per signal type per instrument.

A signal type with negative expectancy (average R < 0) after N trades on an
instrument is switched off automatically (N is on the settings page). You can
re-enable it from the dashboard.
"""
from __future__ import annotations

from .db import DB, iso


def compute(db: DB) -> list[dict]:
    rows = db.all(
        "SELECT symbol, signal, COUNT(*) AS n, "
        "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
        "AVG(pnl_r) AS avg_r, SUM(pnl_usd) AS pnl, SUM(fees) AS fees, SUM(swap + funding) AS carry "
        "FROM trades GROUP BY symbol, signal ORDER BY symbol, signal")
    disabled = {(r["symbol"], r["signal"]) for r in db.all("SELECT symbol, signal FROM disabled_signals")}
    for r in rows:
        r["win_rate"] = r["wins"] / r["n"] if r["n"] else 0
        r["disabled"] = (r["symbol"], r["signal"]) in disabled
    return rows


def line_for(db: DB, symbol: str, signal: str) -> str:
    r = db.one("SELECT COUNT(*) AS n, AVG(pnl_r) AS avg_r, "
               "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins FROM trades WHERE symbol=? AND signal=?",
               (symbol, signal))
    if not r or not r["n"]:
        return "no trades yet"
    return f"{r['n']} trades, win rate {100 * r['wins'] / r['n']:.0f}%, avg {r['avg_r'] or 0:+.2f}R"


def as_text(db: DB) -> str:
    rows = compute(db)
    if not rows:
        return "no trades yet"
    return "\n".join(f"{r['symbol']} {r['signal']}: {r['n']} trades, win {100 * r['win_rate']:.0f}%, "
                     f"avg {r['avg_r'] or 0:+.2f}R, ${r['pnl']:+.2f}" for r in rows)


def disabled_for(db: DB, symbol: str) -> set[str]:
    return {r["signal"] for r in db.all("SELECT signal FROM disabled_signals WHERE symbol=?", (symbol,))}


def auto_disable(db: DB, cfg: dict) -> list[str]:
    n_min = cfg["signals"]["auto_disable_after_trades"]
    switched = []
    for r in compute(db):
        if r["disabled"] or r["n"] < n_min or r["avg_r"] is None:
            continue
        if r["avg_r"] < 0:
            db.execute("INSERT OR IGNORE INTO disabled_signals(symbol, signal, at, reason) VALUES (?,?,?,?)",
                       (r["symbol"], r["signal"], iso(),
                        f"avg {r['avg_r']:+.2f}R after {r['n']} trades"))
            switched.append(f"{r['symbol']} {r['signal']} ({r['avg_r']:+.2f}R over {r['n']} trades)")
    return switched


def enable(db: DB, symbol: str, signal: str) -> None:
    db.execute("DELETE FROM disabled_signals WHERE symbol=? AND signal=?", (symbol, signal))
