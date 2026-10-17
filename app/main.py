"""Entry point: `uvicorn app.main:app` (or `python -m app.main`).

One process runs everything: price feeds, engine loops, scheduled jobs,
the dashboard/settings web pages and the JSON API behind them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import scorecard
from .ai.client import AIClient
from .ai.review import current_playbook, daily_review, weekly_playbook
from .alerts import telegram
from .config import ENV
from .db import DB, iso
from .engine import Engine
from .feeds import build_feeds
from .feeds.prices import PriceCache, prune_candles
from .instruments import INSTRUMENTS
from .news.digest import latest_digest, run_digest
from .news.sources import events_between, recent_headlines, refresh_calendar, refresh_headlines
from .sessions import market_open, session_name
from .settings_store import PRESETS, SettingsStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
STATIC = Path(__file__).parent / "static"

if not ENV.paper_only:
    raise SystemExit("PAPER_ONLY must be true. This app has no live-trading code; refusing to start.")

db = DB(ENV.db_path)
db.ensure_account(ENV.starting_balance)
store = SettingsStore(db)
prices = PriceCache()
feeds = build_feeds(prices)
ai = AIClient(db)
engine = Engine(db, store, prices, feeds, ai, telegram)
scheduler = AsyncIOScheduler(timezone="UTC")


# ============================================================ scheduled jobs
async def job_swap() -> None:
    total = engine.broker.apply_swap(store.current, datetime.now(timezone.utc))
    if total:
        log.info("Swap applied: %.4f", total)


async def job_funding() -> None:
    res = await feeds["binance"].fetch_funding()
    if res:
        total = engine.broker.apply_funding(res[0])
        if total:
            log.info("Funding applied at rate %.6f: %.4f", res[0], total)


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
    cfg = store.current
    y = datetime.now(timezone.utc) - timedelta(days=1)
    d0, d1 = y.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl FROM trades WHERE closed_at >= ? AND closed_at < ?",
                 (d0, d1))
    msg = [f"📊 <b>Daily summary {d0}</b>", engine.status_text(),
           f"Trades closed: {row['n']} · P&L ${row['pnl']:+.2f}"]
    switched = scorecard.auto_disable(db, cfg)
    if switched:
        msg.append("Auto-disabled (negative expectancy): " + ", ".join(switched))
    await telegram.send("\n".join(msg))
    if cfg["ai"]["daily_review"]:
        await telegram.send(await daily_review(ai, db, cfg, y))
    prune_candles(db)


async def job_weekly() -> None:
    if store.current["ai"]["weekly_playbook"]:
        await telegram.send(await weekly_playbook(ai, db, store.current, scorecard.as_text(db)))


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
    ny = "America/New_York"
    scheduler.add_job(job_swap, CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone=ny))
    scheduler.add_job(job_funding, CronTrigger(hour="7,15,23", minute=59, second=45))
    scheduler.add_job(job_calendar, IntervalTrigger(hours=1), next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(job_news, IntervalTrigger(minutes=1), next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20))
    scheduler.add_job(job_daily, CronTrigger(hour=0, minute=5))
    scheduler.add_job(job_weekly, CronTrigger(day_of_week="sun", hour=12, minute=0))
    scheduler.add_job(job_backup, CronTrigger(hour=0, minute=30))
    for job in scheduler.get_jobs():
        job.modify(misfire_grace_time=300, coalesce=True)


# ========================================================= telegram commands
@telegram.command("status")
async def _tg_status() -> str:
    return engine.status_text()


@telegram.command("pause")
async def _tg_pause() -> str:
    engine.pause()
    return "⏸ Paused. Open positions keep their stops."


@telegram.command("resume")
async def _tg_resume() -> str:
    engine.resume()
    return "▶️ Resumed."


@telegram.command("closeall")
async def _tg_closeall() -> str:
    return f"Closed {await engine.close_all()} positions."


@telegram.command("help")
async def _tg_help() -> str:
    return "Commands: /status /pause /resume /closeall"


# ===================================================================== app
@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(f.run_stream()) for f in feeds.values()]
    tasks.append(asyncio.create_task(telegram.poll_commands()))
    await engine.bootstrap()
    tasks += [asyncio.create_task(engine.fast_loop()), asyncio.create_task(engine.minute_loop())]
    schedule_jobs()
    scheduler.start()
    await telegram.send("🤖 Paper trader started.")
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
def _quotes() -> dict:
    now = datetime.now(timezone.utc)
    out = {}
    for sym, inst in INSTRUMENTS.items():
        q = prices.get(sym)
        out[sym] = {
            "display": inst.display, "decimals": inst.decimals, "source": inst.source,
            "bid": q.bid if q else None, "ask": q.ask if q else None,
            "mid": q.mid if q else None, "mark": q.mark if q else None,
            "age": prices.age_seconds(sym), "open": market_open(inst, now),
            "enabled": store.current["instruments"].get(sym, False),
            "last_check": engine.last_filter_reason.get(sym),
        }
    return out


def _account() -> dict:
    cfg = store.current
    st = engine.broker.state(cfg)
    day0 = db.kv_get("day_start_equity", st.equity)
    acc = db.one("SELECT started_at FROM account WHERE id=1")
    return {
        "equity": st.equity, "cash": st.cash, "peak": st.peak, "unrealized": st.unrealized,
        "starting_balance": st.starting_balance, "pnl_total": st.equity - st.starting_balance,
        "pnl_today": st.equity - day0, "margin_used": st.margin_used,
        "margin_used_pct": st.margin_used_pct, "margin_level_pct": st.margin_level_pct,
        "status": st.status, "status_reason": st.status_reason, "started_at": acc["started_at"],
        "drawdown_pct": 100 * (st.peak - st.equity) / st.peak if st.peak else 0,
        "profile": cfg["profile"], "trading_mode": cfg["trading_mode"],
    }


def _positions() -> list[dict]:
    cfg = store.current
    out = []
    for p in engine.broker.positions():
        q = prices.get(p["symbol"])
        p["price"] = q.mid if q else None
        p["unrealized"] = engine.broker.unrealized(cfg, p)
        p["decimals"] = INSTRUMENTS[p["symbol"]].decimals
        out.append(p)
    return out


def _budget() -> dict:
    cfg = store.current
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    by_purpose = db.all("SELECT purpose, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, "
                        "SUM(cost_usd) cost FROM cost_log WHERE at >= ? GROUP BY purpose", (month,))
    return {"month_spend": ai.month_spend(), "monthly_budget": cfg["ai"]["monthly_budget_usd"],
            "calls_today": ai.calls_today(), "max_calls_per_day": cfg["ai"]["max_calls_per_day"],
            "blocked": ai.budget_block(cfg), "by_purpose": by_purpose, "model": ENV.ai_model}


@app.get("/api/state")
def api_state(_: str = Depends(auth)) -> dict:
    return {
        "now": iso(), "session": session_name(datetime.now(timezone.utc)),
        "account": _account(), "positions": _positions(), "quotes": _quotes(),
        "budget": _budget(),
        "feeds": {k: {"connected": v.connected, "last_message": iso(v.last_message) if v.last_message else None,
                      "last_error": v.last_error, "reconnects": v.reconnects} for k, v in prices.status.items()},
        "funding_rate": prices.funding_rate,
        "config_warnings": [w for w in [
            None if ENV.has_ai else "ANTHROPIC_API_KEY missing - AI gate off (rules only / skip per settings)",
            None if ENV.has_telegram else "Telegram not configured - alerts only go to the log",
        ] if w],
    }


@app.get("/api/stream")
async def api_stream(request: Request, _: str = Depends(auth)):
    """Server-sent events: live quotes + equity every second."""
    async def gen():
        while not await request.is_disconnected():
            acc = _account()
            pos = [{"id": p["id"], "price": p["price"], "unrealized": p["unrealized"]} for p in _positions()]
            yield f"data: {json.dumps({'quotes': _quotes(), 'account': acc, 'positions': pos})}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/equity")
def api_equity(days: int = 90, _: str = Depends(auth)) -> list[dict]:
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    return db.all("SELECT * FROM equity_snapshots WHERE at >= ? ORDER BY at", (since,))


@app.get("/api/trades")
def api_trades(limit: int = 100, _: str = Depends(auth)) -> list[dict]:
    return db.all("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/decisions")
def api_decisions(limit: int = 100, _: str = Depends(auth)) -> list[dict]:
    return db.all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/scorecard")
def api_scorecard(_: str = Depends(auth)) -> dict:
    return {"rows": scorecard.compute(db),
            "disabled": db.all("SELECT * FROM disabled_signals ORDER BY at DESC")}


@app.post("/api/scorecard/enable")
def api_enable_signal(body: dict, _: str = Depends(auth)) -> dict:
    scorecard.enable(db, body["symbol"], body["signal"])
    return {"ok": True}


@app.get("/api/playbook")
def api_playbook(_: str = Depends(auth)) -> dict:
    return {"current": current_playbook(db),
            "versions": db.all("SELECT id, at, source FROM playbook_versions ORDER BY id DESC LIMIT 20"),
            "lessons": db.all("SELECT * FROM lessons ORDER BY id DESC LIMIT 30")}


@app.put("/api/playbook")
def api_playbook_edit(body: dict, _: str = Depends(auth)) -> dict:
    text = str(body.get("content", "")).strip()[:4000]
    if not text:
        raise HTTPException(400, "Playbook can't be empty")
    db.execute("INSERT INTO playbook_versions(at, content, source) VALUES (?,?,?)", (iso(), text, "manual"))
    return {"ok": True}


@app.post("/api/playbook/rollback/{vid}")
def api_playbook_rollback(vid: int, _: str = Depends(auth)) -> dict:
    row = db.one("SELECT content FROM playbook_versions WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404)
    db.execute("INSERT INTO playbook_versions(at, content, source) VALUES (?,?,?)", (iso(), row["content"], "rollback"))
    return {"ok": True}


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
    return {"current": store.current, "schema": store.schema(), "presets": list(PRESETS),
            "history": store.history()}


@app.put("/api/settings")
def api_settings_save(body: dict, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.save(body, "ui")}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/settings/preset/{name}")
def api_settings_preset(name: str, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.apply_preset(name)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/settings/revert/{vid}")
def api_settings_revert(vid: int, _: str = Depends(auth)) -> dict:
    try:
        return {"current": store.revert(vid)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/settings/version/{vid}")
def api_settings_version(vid: int, _: str = Depends(auth)) -> dict:
    row = db.one("SELECT * FROM settings_versions WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404)
    row["data"] = json.loads(row["data"])
    return row


# ---------------------------------------------------------------- controls
@app.post("/api/control/{action}")
async def api_control(action: str, body: dict | None = None, _: str = Depends(auth)) -> dict:
    if action == "pause":
        engine.pause()
    elif action == "resume":
        engine.resume()
    elif action == "close_all":
        return {"closed": await engine.close_all()}
    elif action == "kill":
        return {"closed": await engine.kill()}
    elif action == "close_position":
        return {"ok": await engine.close_position(int((body or {}).get("id", 0)))}
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=ENV.host, port=ENV.port, log_level="info")
