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
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import adaptive, audit, lab, promoter, scorecard, shadow, smc
from .ai import coach, hypothesis, outlook, postmortem, research
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
            if store.cfg(m)["ai"]["use_ai"]:
                await telegram.send(await daily_review(ai, db, store.cfg(m), m, y))
    prune_candles(db)


async def job_weekly() -> None:
    for m in store.active_modes():
        cfg = store.cfg(m)
        if not cfg["ai"]["use_ai"]:
            continue
        if store.current["audit"]["coaching"]:          # clean the lesson list first; the playbook rewrite reads it
            try:
                await telegram.send(await coach.curate_lessons(ai, db, cfg, m, scorecard.as_text(db, m)))
            except Exception as e:  # noqa: BLE001
                log.warning("lesson curation for %s failed: %s", m, e)
        if store.current["ai"]["weekly_playbook"]:
            await telegram.send(await weekly_playbook(ai, db, cfg, m, scorecard.as_text(db, m),
                                                      coach.active_lessons(db, m)))


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
    scheduler.add_job(job_shadow, IntervalTrigger(minutes=15), misfire_grace_time=300, coalesce=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    scheduler.add_job(job_audit, CronTrigger(hour=0, minute=15), misfire_grace_time=3600, coalesce=True)
    # Sunday 04:00 UTC: markets closed, before the 12:00 playbook rewrite (which then sees the proposals)
    scheduler.add_job(job_outlook, CronTrigger(day_of_week="sun", hour=18, minute=0), misfire_grace_time=3 * 3600,
                      coalesce=True)
    scheduler.add_job(job_research, CronTrigger(day_of_week="sun", hour=4, minute=0), misfire_grace_time=3600,
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
        if store.current["modes"][m]["adaptive"]["auto_tune"]:
            await run_tuner(m)


async def job_shadow() -> None:
    n = await asyncio.to_thread(shadow.resolve, db, store.current)
    if n:
        log.info("Shadow outcomes resolved for %d skipped signals", n)


_researching = False


async def run_research() -> dict:
    """Weekly: Lab league table -> 2-year verification -> allowlist proposals; then Sonnet's review."""
    global _researching
    if _researching:
        return {"started": False, "reason": "already running"}
    _researching = True
    made = []
    try:
        settings = store.current
        for c in promoter.candidates(db, settings)[:promoter.MAX_VERIFY_PER_RUN]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "nice", "-n", "10", sys.executable, "-m", "app.promoter", "verify", "--symbol", c["symbol"],
                    "--strategy", c["strategy"], "--mode", c["account"], cwd=str(ROOT),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=2400)
                res = json.loads(out.decode().strip().splitlines()[-1])
            except Exception as e:  # noqa: BLE001
                log.warning("research verify %s for %s failed: %s", c["item"], c["account"], e)
                continue
            log.info("research: %s for %s -> %s", c["item"], c["account"], res.get("reason"))
            if res.get("passed"):
                pid = promoter.propose(db, c["account"], "add", c["item"], {"lab": c["lab"], "backtest": res},
                                       f"Lab {c['lab']['n']} trades avg {c['lab']['avg_r']:+.2f}R; 2-year test {res['reason']}")
                made.append(f"➕ add {c['item']} to {MODE_LABELS[c['account']]} (#{pid})")
        for rm in promoter.removals(db, settings):
            pid = promoter.propose(db, rm["account"], "remove", rm["item"], {"live": rm["live"]},
                                   f"losing live: {rm['live']['n']} trades, avg {rm['live']['avg_r']:+.2f}R in "
                                   f"{rm['live']['days']} days")
            made.append(f"➖ remove {rm['item']} from {MODE_LABELS[rm['account']]} (#{pid})")
        for cal in promoter.calibrations(db, settings):
            pid = promoter.propose(db, cal["account"], "set", cal["item"], cal["evidence"], cal["reason"])
            made.append(f"🎚 {cal['item']} for {MODE_LABELS[cal['account']]} (#{pid})")
        if settings["research"].get("hypotheses", True):
            try:
                made += await run_hypotheses(settings)
            except Exception as e:  # noqa: BLE001
                log.warning("hypothesis step failed: %s", e)
        if made:
            await telegram.send("🔬 <b>New proposals</b> (approve in the Learning tab)\n" + "\n".join(made))
        if settings["research"]["ai_review"]:
            await telegram.send(await research.weekly_review(ai, db, settings, store.cfg("standard")))
    finally:
        _researching = False
    return {"started": True, "proposals": made}


async def _run_json(args: list[str], timeout: float) -> dict:
    """Run `python -m <args>` at low priority and parse the JSON on its last output line."""
    proc = await asyncio.create_subprocess_exec("nice", "-n", "10", sys.executable, "-m", *args, cwd=str(ROOT),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return json.loads(out.decode().strip().splitlines()[-1])


async def run_hypotheses(settings: dict) -> list[str]:
    """Weekly: Sonnet proposes rules from the loss causes and year 1 of each setup's replay; year 2
    (unseen) decides. Passes become 'rule' proposals for every account that trades the setup."""
    allow = {m: settings["modes"][m]["signals"].get("allowlist") or [] for m in promoter.real_modes(settings)}
    setups = sorted({a for items in allow.values() for a in items if "*" not in a})
    if not setups:
        return []
    # the first run builds the 2-year signal cache per market (slow once, then only new candles)
    profiles = await _run_json(["app.hypothesis", "profile", "--setups", ",".join(setups)], timeout=4 * 3600)
    res = await hypothesis.propose(ai, db, settings, store.cfg("standard"), setups, profiles)
    log.info("hypotheses: %s", res)
    made = []
    for h in res["hypotheses"]:
        rule = f"{h['knob']}={h['value'].replace(' ', '')}"
        try:
            r = await _run_json(["app.hypothesis", "test", "--setup", h["setup"], "--rule", rule,
                                 "--allowlists", json.dumps(allow)], timeout=3600)
        except Exception as e:  # noqa: BLE001
            hypothesis.record(db, h["setup"], rule, h["why"], "error", {"reason": str(e)}, None)
            continue
        pid = None
        if r.get("passed"):
            item = f"{h['setup'].split('@')[0]} {rule}"
            for m, items in allow.items():
                if any(a.split("@")[0] == h["setup"].split("@")[0] for a in items) and \
                        not promoter._recently_decided(db, m, "rule", item):
                    pid = promoter.propose(db, m, "rule", item, {"hypothesis": h["why"], "test": r["detail"]},
                                           f"walk-forward test passed: {r['reason']}. Idea: {h['why']}")
                    made.append(f"🧪 rule {item} for {MODE_LABELS[m]} (#{pid})")
        hypothesis.record(db, h["setup"], rule, h["why"], "passed" if r.get("passed") else "failed",
                          {"reason": r.get("reason"), "detail": r.get("detail")}, pid)
    return made


async def run_audit(day: datetime | None = None) -> list[dict]:
    day = day or datetime.now(timezone.utc) - timedelta(days=1)
    reports = []
    for m in MODES:
        if not store.current["modes"][m]["enabled"]:
            continue
        cfg = store.cfg(m)
        if store.current["audit"]["post_mortems"] and cfg["ai"]["use_ai"]:
            try:
                await postmortem.run(ai, db, cfg, m, day)
            except Exception as e:  # noqa: BLE001
                log.warning("post-mortems for %s failed: %s", m, e)
        rep = audit.daily_report(db, store.current, m, day)
        if store.current["audit"].get("coaching") and cfg["ai"]["use_ai"]:
            try:
                rep["coach"] = (await coach.run_daily(ai, db, cfg, m, rep, day))["note"]
            except Exception as e:  # noqa: BLE001
                log.warning("coaching for %s failed: %s", m, e)
        audit.save_report(db, rep)
        reports.append(rep)
    await telegram.send(audit.telegram_text(reports))
    return reports


async def job_audit() -> None:
    if store.current["audit"]["enabled"]:
        await run_audit()


async def run_outlook() -> str:
    r = store.current["research"]
    msg = await outlook.run(ai, db, store.cfg("standard"), web_search=r["outlook_web_search"],
                            max_searches=r["outlook_searches"])
    await telegram.send(msg)
    return msg


async def job_outlook() -> None:
    if store.current["research"].get("weekly_outlook"):
        await run_outlook()


async def job_research() -> None:
    if store.current["research"]["enabled"]:
        await run_research()


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


for page in ("dashboard", "settings", "news", "floor"):
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
            "contract": inst.contract_size, "asset": inst.asset_class, "min_lot": inst.min_lot,
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
    by_purpose = db.all("SELECT purpose, model, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, "
                        "SUM(cost_usd) cost FROM cost_log WHERE at >= ? GROUP BY purpose, model ORDER BY cost DESC",
                        (month,))
    a = cfg["ai"]
    return {"month_spend": ai.month_spend(), "monthly_budget": a["monthly_budget_usd"],
            "calls_today": ai.calls_today(), "max_calls_per_day": a["max_calls_per_day"],
            "blocked": ai.budget_block(cfg), "by_purpose": by_purpose, "model": ENV.ai_model,
            "strong_model": ENV.ai_strong_model, "strong_enabled": a["strong_enabled"],
            "strong_spend": ai.month_spend("strong"), "strong_budget": a["strong_budget_usd"],
            "strong_calls_today": ai.calls_today("strong"), "strong_calls_per_day": a["strong_calls_per_day"],
            "strong_blocked": ai.budget_block(cfg, "strong")}


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
            payload = {"ts": time.time(), "quotes": _quotes(account), "account": _account(account),
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


@app.get("/api/levels")
def api_levels(symbol: str, tf: str = "15m", _: str = Depends(auth)) -> dict:
    """SMC map for the chart: day/session levels, liquidity pools, FVGs and order blocks."""
    if symbol not in INSTRUMENTS or tf not in ("5m", "15m"):
        raise HTTPException(404)
    candles = load_candles(db, symbol, 2400 if tf == "5m" else 800, tf)
    atr = engine.current_atr(symbol, tf)
    if not candles or not atr:
        return {"levels": [], "zones": []}
    return smc.to_json(candles, atr)


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
            "skips": shadow.summary(db, account, days=30),
            "tuning": runs, "tuning_running": account in _tuning,
            "exits": {k: cfg["exits"][k] for k in ("default_stop_atr", "default_target_atr", "breakeven_at_r")},
            "adaptive": cfg["adaptive"]}


@app.get("/api/audit")
def api_audit(account: str = "standard", _: str = Depends(auth)) -> dict:
    account = _acc(account)
    rows = db.all("SELECT day, data FROM audit_reports WHERE account=? ORDER BY id DESC LIMIT 14", (account,))
    reports = [json.loads(r["data"]) for r in rows]
    seen, uniq = set(), []
    for r in reports:                      # latest report per day
        if r["day"] not in seen:
            seen.add(r["day"])
            uniq.append(r)
    today = audit.daily_report(db, store.current, account, datetime.now(timezone.utc))
    pms = db.all("SELECT trade_id, at, verdict, data FROM post_mortems WHERE account=? ORDER BY id DESC LIMIT 20", (account,))
    for p in pms:
        p["data"] = json.loads(p["data"])
    reviews = db.all("SELECT kind, at, verdict, data FROM coach_reviews WHERE account=? ORDER BY id DESC LIMIT 20", (account,))
    for r in reviews:
        r["data"] = json.loads(r["data"])
    coached = next((r for r in uniq if r.get("coach")), None)
    return {"today": today, "history": uniq, "post_mortems": pms, "reviews": reviews,
            "coach": {"day": coached["day"], **coached["coach"]} if coached else None,
            "lessons": coach.active_lessons(db, account)}


@app.get("/api/floor")
def api_floor(_: str = Depends(auth)) -> list[dict]:
    """The floor manager's view: every trader side by side - today's audit, signal funnel, decision log,
    recent trades, post-mortems and the last daily reports."""
    now = datetime.now(timezone.utc)
    out = []
    for m in MODES:
        reports, seen = [], set()
        for r in db.all("SELECT data FROM audit_reports WHERE account=? ORDER BY id DESC LIMIT 14", (m,)):
            rep = json.loads(r["data"])
            if rep["day"] not in seen:
                seen.add(rep["day"])
                reports.append(rep)
        pms = db.all("SELECT trade_id, at, verdict, data FROM post_mortems WHERE account=? ORDER BY id DESC LIMIT 6", (m,))
        for p in pms:
            p["data"] = json.loads(p["data"])
        reviews = db.all("SELECT kind, at, verdict, data FROM coach_reviews WHERE account=? ORDER BY id DESC LIMIT 8", (m,))
        for r in reviews:
            r["data"] = json.loads(r["data"])
        coached = next((r for r in reports if r.get("coach")), None)
        out.append({
            "account": _account(m),
            "today": audit.daily_report(db, store.current, m, now),
            "history": reports[:7],
            "post_mortems": pms,
            "reviews": reviews,
            "coach": {"day": coached["day"], **coached["coach"]} if coached else None,
            "lessons": coach.active_lessons(db, m),
            # the week in R: what was taken vs what every skipped setup would have done (shadow outcomes)
            "week": {"skips": shadow.summary(db, m, days=7),
                     "taken": db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl_r),0) r, COALESCE(SUM(pnl_usd),0) usd, "
                                     "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins FROM trades "
                                     "WHERE account=? AND closed_at >= ?", (m, iso(now - timedelta(days=7))))},
            "decisions": db.all("SELECT at, symbol, signal, side, accepted, reject_reason, signal_note FROM decisions "
                                "WHERE account=? ORDER BY id DESC LIMIT 25", (m,)),
            "trades": db.all("SELECT symbol, side, signal, pnl_usd, pnl_r, exit_reason, closed_at, COALESCE(trial,0) trial, "
                             "causes FROM trades WHERE account=? ORDER BY id DESC LIMIT 8", (m,)),
        })
    return out


@app.get("/api/loop")
def api_loop(_: str = Depends(auth)) -> dict:
    """The learning loop: the Lab's season and loss causes, and the weekly hypotheses with their test results."""
    return {"lab": lab.summary(db, days=7), "hypotheses": hypothesis.recent(db, 12),
            "pending": db.scalar("SELECT COUNT(*) FROM proposals WHERE status='pending'") or 0}


_outlook_running = False


@app.get("/api/outlook")
def api_outlook(_: str = Depends(auth)) -> dict:
    return {"outlook": outlook.latest(db), "running": _outlook_running}


@app.post("/api/outlook/run")
async def api_outlook_run(_: str = Depends(auth)) -> dict:
    global _outlook_running
    if _outlook_running:
        return {"started": False, "reason": "already running"}

    async def go() -> None:
        global _outlook_running
        _outlook_running = True
        try:
            await run_outlook()
        except Exception:  # noqa: BLE001
            log.exception("weekly outlook failed")
        finally:
            _outlook_running = False
    asyncio.create_task(go())
    return {"started": True}


@app.post("/api/audit/run")
async def api_audit_run(_: str = Depends(auth)) -> dict:
    asyncio.create_task(run_audit())
    return {"started": True}


@app.get("/api/research")
def api_research(_: str = Depends(auth)) -> dict:
    props = db.all("SELECT * FROM proposals ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT 30")
    for p in props:
        p["evidence"] = json.loads(p["evidence"]) if p["evidence"] else None
    return {"proposals": props, "lab": promoter.lab_table(db), "review": research.latest(db),
            "running": _researching, "allowlists": {m: store.current["modes"][m]["signals"].get("allowlist") or []
                                                    for m in MODES if m != "lab"}}


@app.post("/api/research/run")
async def api_research_run(_: str = Depends(auth)) -> dict:
    if _researching:
        return {"started": False, "reason": "already running"}
    asyncio.create_task(run_research())
    return {"started": True}


@app.post("/api/proposals/{pid}/{action}")
def api_proposal(pid: int, action: str, _: str = Depends(auth)) -> dict:
    p = db.one("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] != "pending" or action not in ("approve", "reject"):
        raise HTTPException(400, "No such pending proposal")
    if action == "approve":
        store.save(promoter.apply_proposal(store.current, p), f"proposal:{pid}")
    db.execute("UPDATE proposals SET status=?, decided_at=? WHERE id=?",
               ("approved" if action == "approve" else "rejected", iso(), pid))
    return {"ok": True, "allowlist": store.current["modes"][p["account"]]["signals"].get("allowlist")}


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
