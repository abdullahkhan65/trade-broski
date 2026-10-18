"""Lab -> allowlist research pipeline (weekly; proposals need your approval).

1. LAB LEAGUE TABLE: the Lab account trades every signal. Score each symbol x strategy on
   its live Lab trades (last 60 days).
2. CANDIDATES: pairs with enough Lab trades and a positive average R that some real account
   doesn't trade yet.
3. VERIFY: re-test each candidate on 2 YEARS of history under that account's own rules
   (python -m app.promoter verify ...). It must make money in BOTH years with 15+ trades in
   each - the same bar the current allowlists passed.
4. PROPOSE "add" for the ones that pass. Separately, propose "remove" for allowlisted pairs
   that are losing in the account's own live trades (12+ trades, average below -0.1R).

Nothing changes until you press Approve in the Learning tab. Approving edits that account's
allowlist (saved as a settings version you can revert).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone

from .db import DB, iso
from .signals import allowed

LAB = "lab"
NOT_BACKTESTABLE = {"news_momentum"}         # needs historical AI news digests we don't have
VERIFY_DAYS = 730
MIN_YEAR_TRADES = 15
LAB_DAYS = 60
LIVE_DAYS = 90
REMOVE_MIN_TRADES = 12
REMOVE_MAX_AVG_R = -0.1
REJECT_COOLDOWN_DAYS = 30
MAX_VERIFY_PER_RUN = 4


def lab_table(db: DB, days: int = LAB_DAYS) -> list[dict]:
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = db.all("SELECT symbol, signal, COUNT(*) AS n, SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
                  "COALESCE(SUM(pnl_r), 0) AS total_r FROM trades WHERE account=? AND closed_at >= ? "
                  "AND pnl_r IS NOT NULL GROUP BY symbol, signal", (LAB, since))
    for r in rows:
        r["avg_r"] = round(r["total_r"] / r["n"], 3) if r["n"] else None
        r["total_r"] = round(r["total_r"], 1)
    return sorted(rows, key=lambda r: r["total_r"], reverse=True)


def _recently_decided(db: DB, account: str, action: str, item: str) -> bool:
    since = iso(datetime.now(timezone.utc) - timedelta(days=REJECT_COOLDOWN_DAYS))
    return bool(db.scalar("SELECT COUNT(*) FROM proposals WHERE account=? AND action=? AND item=? AND "
                          "(status='pending' OR (status='rejected' AND decided_at >= ?))",
                          (account, action, item, since)))


def real_modes(settings: dict) -> list[str]:
    return [m for m, v in settings["modes"].items() if m != LAB and v["enabled"]]


def candidates(db: DB, settings: dict) -> list[dict]:
    """(account, item, lab stats) worth verifying on 2 years of history."""
    r = settings["research"]
    out = []
    for row in lab_table(db):
        if row["signal"] in NOT_BACKTESTABLE or row["n"] < r["min_lab_trades"] or (row["avg_r"] or 0) < r["min_lab_avg_r"]:
            continue
        item = f"{row['symbol']}:{row['signal']}"
        for mode in real_modes(settings):
            allow = settings["modes"][mode]["signals"].get("allowlist") or []
            if not allow or allowed(allow, row["symbol"], row["signal"]):
                continue                       # already trades it (or has no allowlist at all)
            if _recently_decided(db, mode, "add", item):
                continue
            out.append({"account": mode, "item": item, "symbol": row["symbol"], "strategy": row["signal"], "lab": row})
    out.sort(key=lambda c: c["lab"]["total_r"], reverse=True)
    return out


def removals(db: DB, settings: dict) -> list[dict]:
    """Allowlisted pairs that are losing in the account's own live trades."""
    since = iso(datetime.now(timezone.utc) - timedelta(days=LIVE_DAYS))
    out = []
    for mode in real_modes(settings):
        for item in settings["modes"][mode]["signals"].get("allowlist") or []:
            sym, _, strat = item.partition(":")
            if "*" in (sym, strat):
                continue
            row = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(pnl_r), 0) AS s FROM trades WHERE account=? AND symbol=? "
                         "AND signal=? AND closed_at >= ? AND pnl_r IS NOT NULL", (mode, sym, strat, since))
            n, s = int(row["n"]), float(row["s"])
            if n >= REMOVE_MIN_TRADES and s / n < REMOVE_MAX_AVG_R and not _recently_decided(db, mode, "remove", item):
                out.append({"account": mode, "item": item, "live": {"n": n, "total_r": round(s, 1),
                                                                      "avg_r": round(s / n, 3), "days": LIVE_DAYS}})
    return out


def propose(db: DB, account: str, action: str, item: str, evidence: dict, reason: str) -> int:
    cur = db.execute("INSERT INTO proposals(at, account, action, item, evidence, reason) VALUES (?,?,?,?,?,?)",
                     (iso(), account, action, item, json.dumps(evidence), reason))
    return int(cur.lastrowid)


def apply_proposal(settings: dict, proposal: dict) -> dict:
    """Return new settings with the proposal applied to that account's allowlist."""
    data = copy.deepcopy(settings)
    sig = data["modes"][proposal["account"]]["signals"]
    allow = list(sig.get("allowlist") or [])
    strategy = proposal["item"].partition(":")[2]
    if proposal["action"] == "add":
        if proposal["item"] not in allow:
            allow.append(proposal["item"])
        if strategy in sig:
            sig[strategy] = True               # make sure the strategy itself is switched on
    else:
        allow = [x for x in allow if x != proposal["item"]]
    sig["allowlist"] = allow
    return data


# ------------------------------------------------------------- verification (subprocess)
def verify(symbol: str, strategy: str, mode: str) -> dict:
    from .backtest import HIST, WINDOW, load_history, load_settings, run
    from .instruments import INSTRUMENTS
    from .settings_store import cfg_for

    cfg = copy.deepcopy(cfg_for(load_settings(), mode))
    cfg["signals"][strategy] = True
    cfg["signals"]["allowlist"] = [f"{symbol}:{strategy}"]
    tf = cfg["timeframe"]
    path = HIST / f"{symbol}-{tf}.csv"
    stale = not path.exists() or time.time() - os.path.getmtime(path) > 6 * 86400
    candles = load_history(symbol, VERIFY_DAYS, tf, refresh=stale)
    res = {"symbol": symbol, "strategy": strategy, "mode": mode, "timeframe": tf, "days": VERIFY_DAYS}
    if len(candles) < WINDOW + 500:
        return {**res, "passed": False, "reason": "not enough history"}
    r = run(INSTRUMENTS[symbol], candles, cfg, 250.0)
    t = r["trades"]
    mid = candles[WINDOW]["t"] + (candles[-1]["t"] - candles[WINDOW]["t"]) / 2
    y1 = [x for x in t if datetime.fromisoformat(x["opened"]).timestamp() < mid]
    y2 = [x for x in t if datetime.fromisoformat(x["opened"]).timestamp() >= mid]
    y1r, y2r = sum(x["r"] for x in y1), sum(x["r"] for x in y2)
    passed = len(y1) >= MIN_YEAR_TRADES and len(y2) >= MIN_YEAR_TRADES and y1r > 0 and y2r > 0
    if passed:
        reason = f"won both years: {y1r:+.1f}R ({len(y1)} trades) and {y2r:+.1f}R ({len(y2)} trades)"
    elif len(y1) < MIN_YEAR_TRADES or len(y2) < MIN_YEAR_TRADES:
        reason = f"too few trades to judge ({len(y1)} / {len(y2)} per year, need {MIN_YEAR_TRADES})"
    else:
        reason = f"not profitable in both years: {y1r:+.1f}R then {y2r:+.1f}R"
    return {**res, "passed": passed, "reason": reason, "trades": len(t),
            "avg_r": round(sum(x["r"] for x in t) / len(t), 3) if t else None,
            "y1_n": len(y1), "y1_r": round(y1r, 1), "y2_n": len(y2), "y2_r": round(y2r, 1),
            "equity": round(r["equity"], 2), "max_dd_pct": round(100 * r["max_dd"], 1)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="2-year both-years test of one symbol:strategy under one account's rules")
    v.add_argument("--symbol", required=True)
    v.add_argument("--strategy", required=True)
    v.add_argument("--mode", required=True)
    a = p.parse_args()
    print(json.dumps(verify(a.symbol, a.strategy, a.mode)))


if __name__ == "__main__":
    main()
