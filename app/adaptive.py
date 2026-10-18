"""Adaptive strategy selection: which strategy works in which market regime, per account.

Every trade is tagged with its strategy (signal type) and the market regime at entry
(trend / range / volatile / mixed). For each (account, strategy, regime):

  score = sum of R / (trades + 10)      <- "shrunk" average: few trades pull it towards 0,
                                           so 3 lucky wins don't count as an edge

  active     normal risk
  cautious   score < 0 after min_trades      -> half risk
  off        score < -0.15 after min_trades  -> no trades in this regime for 14 days
  probation  after 'off' expires             -> half risk; after 8 trades: back to
                                                active if avg R > 0, else off for 28 days

Nothing here can raise risk above the account's own risk % - it only lowers it or
switches strategies off. Updated daily and after every closed trade.

Pooled learning (adaptive.pool_lab): the Lab account trades every signal, so it collects
evidence far faster than a real account that trades once a week. A real account's score
also counts the Lab's trades of the same strategy + regime, on the symbols that account
is allowed to trade it on, at half weight (LAB_WEIGHT) - its exits and filters differ.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import DB, iso, parse_iso
from .signals import allowed

SHRINK = 10              # pseudo-trades at 0R
OFF_SCORE = -0.15
OFF_DAYS = 14
OFF_DAYS_REPEAT = 28
PROBATION_TRADES = 8
LOOKBACK_DAYS = 120
REGIMES = ("trend", "range", "volatile", "mixed")
LAB = "lab"
LAB_WEIGHT = 0.5


def _own(db: DB, account: str, signal: str, regime: str, since: str) -> tuple[int, float]:
    r = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(pnl_r), 0) AS s FROM trades "
               "WHERE account=? AND signal=? AND regime=? AND closed_at >= ? AND pnl_r IS NOT NULL",
               (account, signal, regime, since))
    return int(r["n"]), float(r["s"])


def _lab(db: DB, cfg: dict, signal: str, regime: str, since: str) -> tuple[int, float]:
    """Lab trades of this strategy+regime on the symbols this account may trade it on."""
    allow = cfg["signals"].get("allowlist") or []
    rows = db.all("SELECT symbol, COUNT(*) AS n, COALESCE(SUM(pnl_r), 0) AS s FROM trades WHERE account=? "
                  "AND signal=? AND regime=? AND closed_at >= ? AND pnl_r IS NOT NULL GROUP BY symbol",
                  (LAB, signal, regime, since))
    rows = [r for r in rows if not allow or allowed(allow, r["symbol"], signal)]
    return sum(int(r["n"]) for r in rows), sum(float(r["s"]) for r in rows)


def _stats(db: DB, account: str, signal: str, regime: str, since: str, cfg: dict | None = None
           ) -> tuple[float, float, str]:
    """(effective trades, effective total R, description). Own trades count 1, Lab trades LAB_WEIGHT."""
    n, total = _own(db, account, signal, regime, since)
    if not cfg or account == LAB or not cfg["adaptive"].get("pool_lab"):
        return n, total, f"{n} trades"
    ln, ls = _lab(db, cfg, signal, regime, since)
    if not ln:
        return n, total, f"{n} trades"
    return n + LAB_WEIGHT * ln, total + LAB_WEIGHT * ls, f"{n} own + {ln} Lab trades"


def _save(db: DB, account: str, signal: str, regime: str, status: str, mult: float, since: str | None,
          until: str | None, n: int, avg_r: float | None, reason: str) -> None:
    db.execute(
        "INSERT INTO strategy_state(account, signal, regime, status, risk_mult, since, until, n, avg_r, reason, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account, signal, regime) DO UPDATE SET status=excluded.status, "
        "risk_mult=excluded.risk_mult, since=excluded.since, until=excluded.until, n=excluded.n, "
        "avg_r=excluded.avg_r, reason=excluded.reason, updated_at=excluded.updated_at",
        (account, signal, regime, status, mult, since, until, n, avg_r, reason, iso()))


def update(db: DB, account: str, cfg: dict, now: datetime | None = None) -> list[str]:
    """Re-score every (strategy, regime) this account has traded. Returns human-readable changes."""
    now = now or datetime.now(timezone.utc)
    min_trades = cfg["adaptive"]["min_trades"]
    lookback = iso(now - timedelta(days=LOOKBACK_DAYS))
    pairs = {(r["signal"], r["regime"]) for r in db.all(
        "SELECT DISTINCT signal, regime FROM trades WHERE account=? AND regime IS NOT NULL", (account,))}
    pairs |= {(r["signal"], r["regime"]) for r in db.all(
        "SELECT signal, regime FROM strategy_state WHERE account=?", (account,))}
    if account != LAB and cfg["adaptive"].get("pool_lab"):
        allow = cfg["signals"].get("allowlist") or []
        pairs |= {(r["signal"], r["regime"]) for r in db.all(
            "SELECT DISTINCT symbol, signal, regime FROM trades WHERE account=? AND regime IS NOT NULL", (LAB,))
            if not allow or allowed(allow, r["symbol"], r["signal"])}
    changes = []
    for signal, regime in sorted(pairs):
        cur = db.one("SELECT * FROM strategy_state WHERE account=? AND signal=? AND regime=?",
                     (account, signal, regime))
        status = cur["status"] if cur else "active"
        label = f"{signal} in {regime} markets"

        if status == "off":
            if cur["until"] and now >= parse_iso(cur["until"]):
                _save(db, account, signal, regime, "probation", 0.5, iso(now), None, 0, None,
                      "probation: half risk until it proves itself again")
                changes.append(f"{label}: back on probation (half risk)")
            continue

        if status == "probation":
            n, total = _own(db, account, signal, regime, cur["since"])
            if n >= PROBATION_TRADES:
                if total / n > 0:
                    _save(db, account, signal, regime, "active", 1.0, iso(now), None, n, total / n,
                          f"passed probation: {n} trades, avg {total / n:+.2f}R")
                    changes.append(f"{label}: passed probation -> active")
                else:
                    _save(db, account, signal, regime, "off", 0.0, iso(now), iso(now + timedelta(days=OFF_DAYS_REPEAT)),
                          n, total / n, f"failed probation: {n} trades, avg {total / n:+.2f}R")
                    changes.append(f"{label}: failed probation -> off for {OFF_DAYS_REPEAT} days")
            else:
                _save(db, account, signal, regime, "probation", 0.5, cur["since"], None, n,
                      total / n if n else None, f"probation: {n}/{PROBATION_TRADES} trades")
            continue

        n, total, desc = _stats(db, account, signal, regime, lookback, cfg)
        score = total / (n + SHRINK)
        avg = total / n if n else None
        shown = round(n)
        if n >= min_trades and score < OFF_SCORE:
            _save(db, account, signal, regime, "off", 0.0, iso(now), iso(now + timedelta(days=OFF_DAYS)), shown, avg,
                  f"losing: {desc}, avg {avg:+.2f}R")
            changes.append(f"{label}: switched off for {OFF_DAYS} days (avg {avg:+.2f}R, {desc})")
        elif n >= min_trades and score < 0:
            if status != "cautious":
                changes.append(f"{label}: weak (avg {avg:+.2f}R, {desc}) -> half risk")
            _save(db, account, signal, regime, "cautious", 0.5, cur["since"] if cur and status == "cautious" else iso(now),
                  None, shown, avg, f"weak: {desc}, avg {avg:+.2f}R -> half risk")
        else:
            if status == "cautious":
                changes.append(f"{label}: recovered -> full risk")
            note = f"{desc}, avg {avg:+.2f}R" if n else "no trades yet"
            if n < min_trades:
                note += f" (judged after {min_trades})"
            _save(db, account, signal, regime, "active", 1.0, cur["since"] if cur and status == "active" else iso(now),
                  None, shown, avg, note)
    return changes


def lookup(db: DB, account: str, cfg: dict, signal: str, regime: str) -> tuple[str, float, str]:
    """(status, risk multiplier, reason) for a signal right now."""
    if not cfg["adaptive"]["enabled"]:
        return "active", 1.0, "strategy switching off"
    row = db.one("SELECT status, risk_mult, reason FROM strategy_state WHERE account=? AND signal=? AND regime=?",
                 (account, signal, regime))
    if not row:
        return "active", 1.0, "no history yet"
    return row["status"], float(row["risk_mult"]), row["reason"] or ""


def score(db: DB, account: str, signal: str, regime: str) -> float:
    """Shrunk average R - used to pick between signals that fire together."""
    n, total = _stats(db, account, signal, regime, iso(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)))
    return total / (n + SHRINK)


def states(db: DB, account: str) -> list[dict]:
    return db.all("SELECT * FROM strategy_state WHERE account=? ORDER BY signal, regime", (account,))


def reset(db: DB, account: str, signal: str, regime: str) -> None:
    db.execute("DELETE FROM strategy_state WHERE account=? AND signal=? AND regime=?", (account, signal, regime))
