"""Entry point: `uvicorn app.main:app` (or `python -m app.main`).

One process runs everything: price feeds, the engine (3 paper accounts),
scheduled jobs, the MT5-style dashboard / settings / news pages and the JSON API.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import adaptive, scorecard
from .ai.client import AIClient
from .ai.review import current_playbook, daily_review, weekly_playbook
from .alerts import telegram
from .config import ENV, ROOT
from .db import DB, iso, parse_iso
from .engine import Engine
from .feeds import build_feeds
from .feeds.prices import PriceCache, load_candles, prune_candles
from .instruments import INSTRUMENTS
from .news.digest import latest_digest, run_digest
from .news.sources import events_between, recent_headlines, refresh_calendar, refresh_headlines
from .sessions import market_open, session_name
from .settings_store import MODE_LABELS, MODES, SettingsStore
from .signals import detect_regime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpx2", "apscheduler.executors.default", "apscheduler.scheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("main")
STATIC = Path(__file__).parent / "static"

if not ENV.paper_only:
    raise SystemExit("PAPER_ONLY must be true. This app has no live-trading code; refusing to start.")

db = DB(ENV.db_path)
if db.migrated_from:
    log.warning("Old database moved to %s - starting fresh 3-account database", db.migrated_from)
db.ensure_accounts(MODES, ENV.starting_balance)
store = SettingsStore(db)
prices = PriceCache()
feeds = build_feeds(prices)
ai = AIClient(db)
engine = Engine(db, store, prices, feeds, ai, telegram)
scheduler = AsyncIOScheduler(timezone="UTC")


def _acc(account: str | None) -> str:
    account = account or "standard"
    if account not in MODES:
        raise HTTPException(404, f"Unknown account {account}")
    return account


# ============================================================ scheduled jobs
async def job_swap() -> None:
    total = engine.broker.apply_swap(store.current, datetime.now(timezone.utc))
    if total:
        log.info("Swap applied: %.4f", total)


async def job_calendar() -> None:
    if store.current["news"]["enabled"]:
        try:
            n = await refresh_calendar(db)
            log.info("Calendar refreshed: %d events", n)
        except Exception as e:  # noqa: BLE001
            log.warning("Calendar refresh failed: %s", e)


_last_headlines = datetime.min.replace(tzinfo=timezone.utc)
_last_digest = datetime.min.replace(tzinfo=timezone.utc)


async def job_news() -> None:
    """Runs every minute; respects the poll/digest intervals from settings."""
    global _last_headlines, _last_digest
    n = store.current["news"]
    if not n["enabled"]:
        return
    now = datetime.now(timezone.utc)
    if now - _last_headlines >= timedelta(minutes=n["headline_poll_min"]):
        _last_headlines = now
        new = await refresh_headlines(db, n["feeds"])
        if new:
            log.info("Headlines: %d new", new)
    if n["digest_enabled"] and now - _last_digest >= timedelta(minutes=n["digest_interval_min"]):
        _last_digest = now
        log.info("News digest: %s", await run_digest(ai, db, store.current))


async def job_daily() -> None:
    y = datetime.now(timezone.utc) - timedelta(days=1)
    d0, d1 = y.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = [f"📊 <b>Daily summary {d0}</b>", engine.status_text()]
    for m in MODES:
        cfg = store.cfg(m)
        row = db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl FROM trades "
                     "WHERE account=? AND closed_at >= ? AND closed_at < ?", (m, d0, d1))
        msg.append(f"{MODE_LABELS[m]}: {row['n']} trades, P&L ${row['pnl']:+.2f}")
        await engine.learn(m)          # re-score strategies per regime (probation, switch-offs)
    await telegram.send("\n".join(msg))
    if store.current["ai"]["daily_review"]:
        for m in store.active_modes():
            await telegram.send(await daily_review(ai, db, store.cfg(m), m, y))
    prune_candles(db)


async def job_weekly() -> None:
    if store.current["ai"]["weekly_playbook"]:
        for m in store.active_modes():
            await telegram.send(await weekly_playbook(ai, db, store.cfg(m), m, scorecard.as_text(db, m)))


async def job_backup() -> None:
    ENV.backup_dir.mkdir(parents=True, exist_ok=True)
    dest = ENV.backup_dir / f"trader-{datetime.now(timezone.utc):%Y%m%d}.db"
    db.backup_to(dest)
    for old in sorted(ENV.backup_dir.glob("trader-*.db"))[:-14]:
        old.unlink()
    if ENV.backup_to_telegram:
        await telegram.send_file(dest, f"Nightly backup {dest.name}")
    log.info("Backup written to %s", dest)


def schedule_jobs() -> None:
    # XM rollover ~ 17:00 New York; every day (BTCUSD swaps weekends too; FX/gold skipped inside)
    scheduler.add_job(job_swap, CronTrigger(hour=17, minute=0, timezone="America/New_York"))
    scheduler.add_job(job_calendar, IntervalTrigger(hours=1), next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(job_news, IntervalTrigger(minutes=1), next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20))
    scheduler.add_job(job_daily, CronTrigger(hour=0, minute=5))
    scheduler.add_job(job_weekly, CronTrigger(day_of_week="sun", hour=12, minute=0))
    scheduler.add_job(job_backup, CronTrigger(hour=0, minute=30))
    for job in scheduler.get_jobs():
        job.modify(misfire_grace_time=300, coalesce=True)
    # weekly self-tuning on Saturday morning UTC (forex closed, server quiet)
    scheduler.add_job(job_tune, CronTrigger(day_of_week="sat", hour=6, minute=0), misfire_grace_time=3600,
                      coalesce=True)


_tuning: set[str] = set()


async def run_tuner(mode: str) -> dict:
    """Run app.tuner in a separate low-priority process, then apply its recommendation if allowed."""
    if mode in _tuning:
        return {"mode": mode, "apply": False, "reason": "already running"}
    _tuning.add(mode)
    try:
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "10", sys.executable, "-m", "app.tuner", "--mode", mode, cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
        res = json.loads(out.decode().strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        res = {"mode": mode, "apply": False, "reason": f"tuner failed: {type(e).__name__}: {e}"}
    finally:
        _tuning.discard(mode)
    applied = False
    if res.get("apply") and res.get("best") and store.current["modes"][mode]["adaptive"]["auto_tune"]:
        data = copy.deepcopy(store.current)
        data["modes"][mode]["exits"].update(res["best"])
        store.save(data, f"tuner:{mode}")
        applied = True
    elif res.get("apply"):
        res["reason"] += " (not applied: auto-tune is off for this mode)"
    db.execute("INSERT INTO tuning_runs(at, account, applied, current, best, result, reason) VALUES (?,?,?,?,?,?,?)",
               (iso(), mode, int(applied), json.dumps(res.get("current")), json.dumps(res.get("best")),
                json.dumps({k: res.get(k) for k in ("train_best", "train_current", "test_best", "test_current",
                                                    "combos_tried", "days", "timeframe")}), res.get("reason")))
    icon = "🔧" if applied else "🔍"
    await telegram.send(f"{icon} <b>[{MODE_LABELS[mode]}] weekly tuning</b>\n"
                        f"{'Applied' if applied else 'No change'}: {res.get('reason')}")
    log.info("[%s] tuner: applied=%s %s", mode, applied, res.get("reason"))
    return res


async def job_tune() -> None:
    for m in store.active_modes():
        await run_tuner(m)


# ========================================================= telegram commands
@telegram.command("status")
async def _tg_status() -> str:
    return engine.status_text()


@telegram.command("pause")
async def _tg_pause() -> str:
    for m in MODES:
        engine.pause(m)
    return "⏸ All accounts paused. Open positions keep their stops."


@telegram.command("resume")
async def _tg_resume() -> str:
    for m in MODES:
        engine.resume(m)
    return "▶️ All accounts resumed."


@telegram.command("closeall")
async def _tg_closeall() -> str:
    n = sum([await engine.close_all(m) for m in MODES])
    return f"Closed {n} positions."


@telegram.command("help")
async def _tg_help() -> str:
    return "Commands: /status /pause /resume /closeall (all 3 accounts)"


# ===================================================================== app
@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(f.run_stream()) for f in feeds.values()]
    tasks.append(asyncio.create_task(telegram.poll_commands()))
    await engine.bootstrap()
    tasks += [asyncio.create_task(engine.fast_loop()), asyncio.create_task(engine.minute_loop())]
    schedule_jobs()
    scheduler.start()
    await telegram.send("🤖 Paper trader started (3 accounts: Standard, Moderate, Aggressive).")
    yield
    scheduler.shutdown(wait=False)
    for t in tasks:
        t.cancel()


app = FastAPI(title="AI Paper Trader", lifespan=lifespan, docs_url=None, redoc_url=None)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    if not ENV.dashboard_password:
        raise HTTPException(503, "Set DASHBOARD_PASSWORD in .env")
    ok = secrets.compare_digest(creds.username.encode(), ENV.dashboard_user.encode()) and \
        secrets.compare_digest(creds.password.encode(), ENV.dashboard_password.encode())
    if not ok:
        raise HTTPException(401, "Wrong login", headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "feeds": {k: v.connected for k, v in prices.status.items()}}


for page in ("dashboard", "settings", "news"):
    def _make(name: str):
        def handler(_: str = Depends(auth)):
            return FileResponse(STATIC / f"{name}.html")
        return handler
    app.add_api_route("/" if page == "dashboard" else f"/{page}", _make(page), methods=["GET"],
                      include_in_schema=False)


@app.get("/static/{name}")
def static(name: str, _: str = Depends(auth)):
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ------------------------------------------------------------------- state
def _quotes(account: str) -> dict:
    now = datetime.now(timezone.utc)
    cfg = store.cfg(account)
    st = engine.broker.state(cfg, account)
    out = {}
    for sym, inst in INSTRUMENTS.items():
        q = prices.get(sym)
        bid = ask = None
        if q:
            bid, ask = engine.broker.broker_quote(cfg, inst, q)
        out[sym] = {
            "xm": inst.xm, "display": inst.display, "digits": inst.digits, "source": inst.source,
            "bid": bid, "ask": ask, "spread": round((ask - bid) / inst.point) if q else None,
            "age": prices.age_seconds(sym), "open": market_open(inst, now),
            "enabled": store.current["instruments"].get(sym, False),
            "last_check": engine.last_check.get((account, sym)),
            "min_lot_risk": engine.min_lot_risk(account, sym, st.equity),
            "regime": engine.regimes.get((sym, cfg["timeframe"])),
            "contract": inst.contract_size,
        }
    return out


def _account(account: str) -> dict:
    cfg = store.cfg(account)
    st = engine.broker.state(cfg, account)
    day0 = db.kv_get(f"day_start_equity:{account}", st.equity)
    acc = db.one("SELECT started_at FROM accounts WHERE id=?", (account,))
    return {
        "id": account, "label": MODE_LABELS[account], "timeframe": cfg["timeframe"],
        "enabled": store.current["modes"][account]["enabled"],
        "balance": st.cash, "equity": st.equity, "peak": st.peak, "profit": st.unrealized,
        "margin": st.margin_used, "free_margin": st.free_margin, "margin_level": st.margin_level_pct,
        "starting_balance": st.starting_balance, "pnl_total": st.equity - st.starting_balance,
        "pnl_today": st.equity - day0, "margin_used_pct": st.margin_used_pct,
        "status": st.status, "status_reason": st.status_reason, "started_at": acc["started_at"],
        "drawdown_pct": 100 * (st.peak - st.equity) / st.peak if st.peak else 0,
        "trading_mode": cfg["trading_mode"],
    }


def _positions(account: str | None) -> list[dict]:
    out = []
    for p in engine.broker.positions(account):
        cfg = store.cfg(p["account"])
        q = prices.get(p["symbol"])
        inst = INSTRUMENTS[p["symbol"]]
        p["price"] = engine.broker.exit_price(cfg, inst, p["side"], q) if q else None
        p["profit"] = engine.broker.unrealized(cfg, p)
        p["digits"] = inst.digits
        p["xm"] = inst.xm
        out.append(p)
    return out


def _budget() -> dict:
    cfg = store.cfg("standard")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    by_purpose = db.all("SELECT purpose, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, "
                        "SUM(cost_usd) cost FROM cost_log WHERE at >= ? GROUP BY purpose", (month,))
    return {"month_spend": ai.month_spend(), "monthly_budget": cfg["ai"]["monthly_budget_usd"],
            "calls_today": ai.calls_today(), "max_calls_per_day": cfg["ai"]["max_calls_per_day"],
            "blocked": ai.budget_block(cfg), "by_purpose": by_purpose, "model": ENV.ai_model}


@app.get("/api/state")
def api_state(account: str = "standard", _: str = Depends(auth)) -> dict:
    account = _acc(account)
    return {
        "now": iso(), "session": session_name(datetime.now(timezone.utc)),
        "account": _account(account), "accounts": {m: _account(m) for m in MODES},
        "positions": _positions(account), "quotes": _quotes(account), "budget": _budget(),
        "feeds": {k: {"connected": v.connected, "last_message": iso(v.last_message) if v.last_message else None,
                      "last_error": v.last_error, "reconnects": v.reconnects} for k, v in prices.status.items()},
        "config_warnings": [w for w in [
            None if ENV.has_ai else "ANTHROPIC_API_KEY missing - AI gate off (rules only / skip per mode)",
        ] if w],
    }


@app.get("/api/stream")
async def api_stream(request: Request, account: str = "standard", _: str = Depends(auth)):
    """Server-sent events: live quotes, account and position P&L every second."""
    account = _acc(account)

    async def gen():
        while not await request.is_disconnected():
            pos = [{"id": p["id"], "price": p["price"], "profit": p["profit"], "stop": p["stop"]}
                   for p in _positions(account)]
            payload = {"quotes": _quotes(account), "account": _account(account),
                       "accounts": {m: _account(m) for m in MODES}, "positions": pos}
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/candles")
def api_candles(symbol: str, tf: str = "15m", n: int = 300, _: str = Depends(auth)) -> list[dict]:
    if symbol not in INSTRUMENTS or tf not in ("5m", "15m"):
        raise HTTPException(404)
    return load_candles(db, symbol, min(n, 2000), tf)


@app.get("/api/equity")
def api_equity(days: int = 90, _: str = Depends(auth)) -> list[dict]:
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    return db.all("SELECT * FROM equity_snapshots WHERE at >= ? ORDER BY at", (since,))


@app.get("/api/compare")
def api_compare(_: str = Depends(auth)) -> list[dict]:
    out = []
    for m in MODES:
        a = _account(m)
        s = scorecard.summary(db, m)
        snaps = db.all("SELECT equity FROM equity_snapshots WHERE account=? ORDER BY at", (m,))
        peak, max_dd = 0.0, 0.0
        for r in snaps:
            peak = max(peak, r["equity"])
            max_dd = max(max_dd, (peak - r["equity"]) / peak if peak else 0)
        days = max(1.0, (datetime.now(timezone.utc) - parse_iso(a["started_at"])).total_seconds() / 86400)
        out.append({**a, **s, "max_drawdown_pct": 100 * max_dd, "days": days,
                    "avg_daily_pct": 100 * ((a["equity"] / a["starting_balance"]) ** (1 / days) - 1)
                    if a["equity"] > 0 else None})
    return out


@app.get("/api/trades")
def api_trades(account: str | None = None, limit: int = 200, _: str = Depends(auth)) -> list[dict]:
    if account:
        return db.all("SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT ?", (_acc(account), limit))
    return db.all("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/decisions")
def api_decisions(account: str | None = None, limit: int = 150, _: str = Depends(auth)) -> list[dict]:
    if account:
        return db.all("SELECT * FROM decisions WHERE account=? ORDER BY id DESC LIMIT ?", (_acc(account), limit))
    return db.all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/scorecard")
def api_scorecard(account: str = "standard", _: str = Depends(auth)) -> dict:
    account = _acc(account)
    return {"rows": scorecard.compute(db, account),
            "disabled": db.all("SELECT * FROM disabled_signals WHERE account=? ORDER BY at DESC", (account,))}


@app.post("/api/scorecard/enable")
def api_enable_signal(body: dict, _: str = Depends(auth)) -> dict:
    scorecard.enable(db, _acc(body.get("account")), body["symbol"], body["signal"])
    return {"ok": True}


@app.get("/api/playbook")
def api_playbook(mode: str = "standard", _: str = Depends(auth)) -> dict:
    mode = _acc(mode)
    return {"current": current_playbook(db, mode),
            "versions": db.all("SELECT id, at, source FROM playbook_versions WHERE mode=? ORDER BY id DESC LIMIT 20",
                               (mode,)),
            "lessons": db.all("SELECT * FROM lessons WHERE mode=? ORDER BY id DESC LIMIT 30", (mode,))}


@app.put("/api/playbook")
def api_playbook_edit(body: dict, _: str = Depends(auth)) -> dict:
    mode = _acc(body.get("mode"))
    text = str(body.get("content", "")).strip()[:4000]
    if not text:
        raise HTTPException(400, "Playbook can't be empty")
    db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
               (mode, iso(), text, "manual"))
    return {"ok": True}


@app.post("/api/playbook/rollback/{vid}")
def api_playbook_rollback(vid: int, _: str = Depends(auth)) -> dict:
    row = db.one("SELECT mode, content FROM playbook_versions WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404)
    db.execute("INSERT INTO playbook_versions(mode, at, content, source) VALUES (?,?,?,?)",
               (row["mode"], iso(), row["content"], "rollback"))
    return {"ok": True}


@app.get("/api/learning")
def api_learning(account: str = "standard", _: str = Depends(auth)) -> dict:
    account = _acc(account)
    cfg = store.cfg(account)
    tf = cfg["timeframe"]
    regimes = {}
    for sym, inst in INSTRUMENTS.items():
        reg = detect_regime(load_candles(db, sym, 2000 if tf == "15m" else 6000, tf), cfg)
        regimes[sym] = {"xm": inst.xm, "regime": reg.name if reg else None, "detail": str(reg) if reg else "not enough data"}
    runs = db.all("SELECT * FROM tuning_runs WHERE account=? ORDER BY id DESC LIMIT 20", (account,))
    for r in runs:
        for k in ("current", "best", "result"):
            r[k] = json.loads(r[k]) if r[k] else None
    by_regime = db.all("SELECT signal, regime, COUNT(*) n, AVG(pnl_r) avg_r, SUM(pnl_usd) pnl FROM trades "
                       "WHERE account=? GROUP BY signal, regime ORDER BY signal, regime", (account,))
    return {"timeframe": tf, "regimes": regimes, "states": adaptive.states(db, account), "by_regime": by_regime,
            "tuning": runs, "tuning_running": account in _tuning,
            "exits": {k: cfg["exits"][k] for k in ("default_stop_atr", "default_target_atr", "breakeven_at_r")},
            "adaptive": cfg["adaptive"]}


@app.post("/api/learning/reset")
def api_learning_reset(body: dict, _: str = Depends(auth)) -> dict:
    adaptive.reset(db, _acc(body.get("account")), body["signal"], body["regime"])
    return {"ok": True}


@app.post("/api/tune/{mode}")
async def api_tune(mode: str, _: str = Depends(auth)) -> dict:
    mode = _acc(mode)
    if mode in _tuning:
        return {"started": False, "reason": "already running"}
    asyncio.create_task(run_tuner(mode))
    return {"started": True}


@app.get("/api/news")
def api_news(_: str = Depends(auth)) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "digest": latest_digest(db, max_age_hours=24),
        "headlines": recent_headlines(db, None, hours=24, limit=80),
        "events": events_between(db, now - timedelta(hours=12), now + timedelta(days=7),
                                 ("USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF"),
                                 ("High", "Medium")),
    }


@app.post("/api/news/digest")
async def api_news_digest(_: str = Depends(auth)) -> dict:
    await refresh_headlines(db, store.current["news"]["feeds"])
    return {"result": await run_digest(ai, db, store.current, force=True)}


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def api_settings(_: str = Depends(auth)) -> dict:
    return {"current": store.current, "schema": store.schema(), "history": store.history()}


@app.put("/api/settings")
def api_settings_save(body: dict, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.save(body, "ui")}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/settings/reset/{mode}")
def api_settings_reset(mode: str, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.reset_mode(mode)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/settings/revert/{vid}")
def api_settings_revert(vid: int, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.revert(vid)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ---------------------------------------------------------------- controls
@app.post("/api/control/{action}")
async def api_control(action: str, account: str = "standard", body: dict | None = None,
                      _: str = Depends(auth)) -> dict:
    account = _acc(account)
    if action == "pause":
        engine.pause(account)
    elif action == "resume":
        engine.resume(account)
    elif action == "close_all":
        return {"closed": await engine.close_all(account)}
    elif action == "kill":
        return {"closed": await engine.kill(account)}
    elif action == "close_position":
        return {"ok": await engine.close_position(int((body or {}).get("id", 0)))}
    elif action == "reset_account":
        db.reset_account(account, ENV.starting_balance)
        engine.cooldown = {k: v for k, v in engine.cooldown.items() if k[0] != account}
        return {"ok": True}
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=ENV.host, port=ENV.port, log_level="info")
