"""The audit engine ("floor manager"): judges the PROCESS, not single outcomes.

Daily, per account (rules only, free):
* Signal funnel - candles checked -> setups -> why each was blocked -> trades. Separates a
  quiet market (no setups) from setups being blocked (worth a look).
* Inactivity - no proven trade for 14 days is amber, 21 days red, with the top blockers.
* Losing streak - is it statistically normal for this account's win rate? Only an unlikely
  streak is flagged, and then the shared causes are listed (same strategy/symbol/regime, gaps).
* Drift - are live results inside the range the backtest predicts? (2 standard errors)
* Health - engine errors, AI errors / budget blocks, stale prices, weekend-rule breaches.
* Exploration - how much of the weekly trial budget is used.

Post-mortems (Haiku, daily, capped): every losing trade is classified as "bad luck" (a valid
setup that didn't work) or "bad decision" (a rule broken, into a level, ignored news...).
Only bad-decision lessons go into the playbook - so the bot learns from mistakes, not noise.

Findings are reported (Audit tab, Telegram, weekly research review). Nothing here changes
settings; fixes go through the proposal system or to you.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import shadow
from .db import DB, iso, parse_iso
from .settings_store import MODE_LABELS

DEFAULT_WIN_RATE = 0.38          # gold breakout backtest
EXPECTED_AVG_R = 0.10            # backtested edge (per trade, after costs)
R_STDEV = 1.1                    # typical spread of single-trade R for 1:2-ish trades

FILTER_KEYS = [("dead market", "dead market (low volatility)"), ("news spike", "news spike candle"),
               ("outside London/NY", "outside London/NY hours"), ("not enough", "not enough history"),
               ("ATR not ready", "not enough history"), ("conflicting", "conflicting signals"),
               ("allowlist", "setup not on allowlist"), ("H1 trend", "against the H1 trend"),
               ("SMC", "SMC filter")]


def filter_key(why_not: str | None) -> str:
    if not why_not:
        return "no setup"
    for key, label in FILTER_KEYS:
        if key in why_not:
            return label
    return why_not.split("(")[0].strip()[:40]


def _day(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ pieces
def funnel(db: DB, account: str, since_day: str, until_day: str) -> dict:
    c = {r["key"]: r["n"] for r in db.all("SELECT key, SUM(n) n FROM audit_counters WHERE account=? AND day >= ? "
                                          "AND day <= ? GROUP BY key", (account, since_day, until_day))}
    filters = sorted(((k[7:], v) for k, v in c.items() if k.startswith("filter:")), key=lambda x: -x[1])
    rows = db.all("SELECT accepted, reject_reason FROM decisions WHERE account=? AND at >= ? AND at < ?",
                  (account, since_day, (datetime.fromisoformat(until_day) + timedelta(days=1)).strftime("%Y-%m-%d")))
    blocked = Counter(shadow.category(r["reject_reason"]) for r in rows if not r["accepted"])
    opened = sum(1 for r in rows if r["accepted"])
    return {"checked": c.get("checked", 0), "no_setup": dict(filters).get("no setup", 0),
            "filtered": [{"reason": k, "n": v} for k, v in filters if k != "no setup"],
            "signals": c.get("signals", 0), "blocked": [{"reason": k, "n": v} for k, v in blocked.most_common()],
            "opened": opened}


def streak_check(db: DB, account: str) -> dict:
    trades = db.all("SELECT symbol, signal, regime, pnl_usd, pnl_r, exit_reason FROM trades WHERE account=? "
                    "AND COALESCE(trial,0)=0 AND pnl_r IS NOT NULL ORDER BY closed_at DESC, id DESC LIMIT 60", (account,))
    streak = []
    for t in trades:
        if (t["pnl_usd"] or 0) < 0 and not str(t["exit_reason"] or "").startswith("breakeven"):
            streak.append(t)
        elif str(t["exit_reason"] or "").startswith("breakeven"):
            continue
        else:
            break
    k = len(streak)
    decided = [t for t in trades if not str(t["exit_reason"] or "").startswith("breakeven")]
    p_win = (sum(1 for t in decided if t["pnl_usd"] > 0) / len(decided)) if len(decided) >= 20 else DEFAULT_WIN_RATE
    p_win = min(max(p_win, 0.2), 0.8)
    prob = (1 - p_win) ** k if k else 1.0
    out = {"streak": k, "win_rate": round(p_win, 2), "probability": round(prob, 4)}
    if k < 3 or prob >= 0.1:
        out.update(level="green", verdict=f"{k} losses in a row is normal variance for a {p_win:.0%} win rate" if k
                   else "no losing streak")
        return out
    out["level"] = "red" if prob < 0.02 else "amber"
    causes = []
    for field in ("signal", "symbol", "regime"):
        top, n = Counter(t[field] for t in streak).most_common(1)[0]
        if n >= max(3, 0.75 * k):
            causes.append(f"{n}/{k} were {field} = {top}")
    gaps = sum(1 for t in streak if "gap" in str(t["exit_reason"]))
    if gaps:
        causes.append(f"{gaps} stopped out through a gap")
    out["verdict"] = (f"{k} losses in a row: only a {prob:.1%} chance at a {p_win:.0%} win rate"
                      + (f" - shared cause: {'; '.join(causes)}" if causes else " - no shared cause found (could still be luck)"))
    out["causes"] = causes
    return out


def drift_check(db: DB, account: str, expected: float = EXPECTED_AVG_R) -> dict:
    rows = db.all("SELECT pnl_r FROM trades WHERE account=? AND COALESCE(trial,0)=0 AND pnl_r IS NOT NULL "
                  "ORDER BY closed_at DESC LIMIT 40", (account,))
    n = len(rows)
    if n < 15:
        return {"level": "green", "n": n, "verdict": f"{n} trades - too few to compare with the backtest yet"}
    avg = sum(r["pnl_r"] for r in rows) / n
    floor = expected - 2 * R_STDEV / math.sqrt(n)
    if avg < floor:
        return {"level": "red", "n": n, "avg_r": round(avg, 3), "floor": round(floor, 3),
                "verdict": f"last {n} trades average {avg:+.2f}R, below the backtest's plausible range "
                           f"(>= {floor:+.2f}R): the edge may be fading"}
    return {"level": "green", "n": n, "avg_r": round(avg, 3), "floor": round(floor, 3),
            "verdict": f"last {n} trades average {avg:+.2f}R, within the backtest's range"}


def health_check(db: DB, account: str, day: str, settings: dict) -> list[dict]:
    items = []
    errs = db.scalar("SELECT COALESCE(SUM(n),0) FROM audit_counters WHERE account='system' AND key='errors' AND day=?", (day,))
    if errs:
        items.append({"level": "red" if errs > 5 else "amber", "area": "engine",
                      "message": f"{errs} engine errors on {day} - check the logs (journalctl -u trade-broski)"})
    ai_err = db.scalar("SELECT COUNT(*) FROM cost_log WHERE ok=0 AND at >= ? AND at < ? AND purpose LIKE ?",
                       (day, _day(datetime.fromisoformat(day) + timedelta(days=1)), f"%{account}%"))
    if ai_err:
        items.append({"level": "amber", "area": "AI", "message": f"{ai_err} failed AI calls"})
    stale = db.scalar("SELECT COUNT(*) FROM decisions WHERE account=? AND at >= ? AND at < ? AND reject_reason LIKE "
                      "'%no fresh live price%'", (account, day, _day(datetime.fromisoformat(day) + timedelta(days=1))))
    if stale:
        items.append({"level": "amber", "area": "prices", "message": f"{stale} setups skipped because prices were stale"})
    unavailable = db.scalar("SELECT COUNT(*) FROM decisions WHERE account=? AND at >= ? AND at < ? AND "
                            "(reject_reason LIKE 'AI unavailable%' OR reject_reason LIKE '[rules only%')",
                            (account, day, _day(datetime.fromisoformat(day) + timedelta(days=1))))
    if unavailable:
        items.append({"level": "amber", "area": "AI", "message": f"{unavailable} setups without AI (budget/cap reached?)"})
    return items


def exploration_check(db: DB, account: str, settings: dict) -> dict | None:
    ex = settings["modes"][account].get("explore") or {}
    if not ex.get("enabled"):
        return None
    now = datetime.now(timezone.utc)
    monday = _day(now - timedelta(days=now.weekday()))
    row = db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) usd, COALESCE(SUM(pnl_r),0) r FROM trades WHERE account=? "
                 "AND trial=1 AND closed_at >= ?", (account, monday))
    week0 = db.kv_get(f"week_start_equity:{account}", None) or 250.0
    budget = week0 * ex["weekly_budget_pct"] / 100
    used = max(0.0, -row["usd"])
    return {"trades": row["n"], "pnl_usd": round(row["usd"], 2), "total_r": round(row["r"], 1),
            "budget_usd": round(budget, 2), "used_pct": round(100 * used / budget, 0) if budget else 0}


# ------------------------------------------------------------------ report
def daily_report(db: DB, settings: dict, account: str, day: datetime | None = None) -> dict:
    day = (day or datetime.now(timezone.utc) - timedelta(days=1)).astimezone(timezone.utc)
    d = _day(day)
    f_day = funnel(db, account, d, d)
    items: list[dict] = []

    # activity
    if f_day["opened"]:
        items.append({"level": "green", "area": "activity", "message": f"{f_day['opened']} trades opened"})
    elif f_day["signals"] == 0:
        items.append({"level": "green", "area": "activity",
                      "message": f"no trades: quiet market - {f_day['checked']} candles checked, no setups"
                                 + (f" (most common: {f_day['filtered'][0]['reason']})" if f_day["filtered"] else "")})
    else:
        top = ", ".join(f"{b['reason']} x{b['n']}" for b in f_day["blocked"][:3]) or "see Journal"
        items.append({"level": "green", "area": "activity",
                      "message": f"no trades: {f_day['signals']} setups, all blocked ({top})"})
    since14 = _day(day - timedelta(days=13))
    proven14 = db.scalar("SELECT COUNT(*) FROM trades WHERE account=? AND COALESCE(trial,0)=0 AND opened_at >= ?",
                         (account, since14)) + db.scalar("SELECT COUNT(*) FROM positions WHERE account=? AND "
                                                        "COALESCE(trial,0)=0 AND opened_at >= ?", (account, since14))
    started = db.scalar("SELECT started_at FROM accounts WHERE id=?", (account,))
    running_days = (day - parse_iso(started)).days if started else 99
    if proven14 == 0 and running_days >= 14 and account != "lab":
        f14 = funnel(db, account, since14, d)
        top = ", ".join(f"{b['reason']} x{b['n']}" for b in (f14["blocked"] + f14["filtered"])[:4])
        since21 = _day(day - timedelta(days=20))
        none21 = running_days >= 21 and not db.scalar(
            "SELECT COUNT(*) FROM trades WHERE account=? AND COALESCE(trial,0)=0 AND opened_at >= ?", (account, since21))
        items.append({"level": "red" if none21 else "amber", "area": "activity",
                      "message": f"no proven-setup trade for {21 if none21 else 14}+ days (backtest: ~1 a week). "
                                 f"Top blockers: {top or 'none - no setups at all'}"})

    streak = streak_check(db, account)
    if streak["streak"]:
        items.append({"level": streak["level"], "area": "losing streak", "message": streak["verdict"]})
    drift = drift_check(db, account)
    if drift["level"] != "green" or drift["n"] >= 15:
        items.append({"level": drift["level"], "area": "edge", "message": drift["verdict"]})
    items += health_check(db, account, d, settings)
    explore = exploration_check(db, account, settings)
    if explore:
        lvl = "amber" if explore["used_pct"] >= 100 else "green"
        items.append({"level": lvl, "area": "exploration",
                      "message": f"trials this week: {explore['trades']} trades, {explore['total_r']:+.1f}R, "
                                 f"{explore['used_pct']:.0f}% of the ${explore['budget_usd']:.2f} budget used"})
    pms = db.all("SELECT verdict FROM post_mortems WHERE account=? AND at >= ?", (account, d))
    if pms:
        c = Counter(p["verdict"] for p in pms)
        items.append({"level": "amber" if c.get("bad decision") else "green", "area": "post-mortems",
                      "message": ", ".join(f"{v}: {n}" for v, n in c.items())})
    rank = {"red": 0, "amber": 1, "green": 2}
    items.sort(key=lambda x: rank[x["level"]])
    return {"day": d, "account": account, "items": items, "funnel": f_day, "streak": streak, "drift": drift,
            "explore": explore,
            "grade": "red" if any(i["level"] == "red" for i in items) else
                     "amber" if any(i["level"] == "amber" for i in items) else "green"}


def save_report(db: DB, rep: dict) -> None:
    db.execute("INSERT INTO audit_reports(day, account, at, data) VALUES (?,?,?,?)",
               (rep["day"], rep["account"], iso(), json.dumps(rep)))


def telegram_text(reports: list[dict]) -> str:
    lines = ["🧾 <b>Daily audit</b>"]
    icon = {"red": "🔴", "amber": "🟠", "green": "🟢"}
    for r in reports:
        lines.append(f"{icon[r['grade']]} <b>{MODE_LABELS.get(r['account'], r['account'])}</b>")
        for i in r["items"]:
            if i["level"] != "green" or i["area"] == "activity":
                lines.append(f"  {icon[i['level']]} {i['area']}: {i['message']}")
        if r.get("coach") and r["coach"].get("focus"):
            lines.append(f"  🎯 focus: {r['coach']['focus']}")
    return "\n".join(lines)
