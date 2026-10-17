"""The trading engine: ties feeds, signals, news, AI gate, risk and paper broker together.

Loops (all in one asyncio process):
* fast loop  (every few seconds): stops/targets, stop-out, equity, loss limits
* minute loop (every 60s): refresh 15m candles, weekend handling, feed health,
  evaluate signals on newly closed candles, equity snapshot every 15 min
* scheduled jobs (APScheduler, see main.py): swap, funding, news, daily review,
  weekly playbook, backups
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import risk, scorecard
from .ai import gate
from .ai.client import AIClient
from .ai.review import current_playbook
from .alerts import Telegram
from .broker import PaperBroker
from .db import DB, iso
from .feeds import fetch_candles
from .feeds.prices import PriceCache, load_candles, store_candles
from .instruments import INSTRUMENTS, Instrument
from .news.digest import bias_for
from .news.sources import blackout, events_between, recent_headlines
from .sessions import is_forex_like, market_open, minutes_to_weekly_close, session_name
from .settings_store import SettingsStore
from .signals import Signal, evaluate

log = logging.getLogger("engine")

AI_SKIP_COOLDOWN = timedelta(hours=2)
STALE_PRICE_SECONDS = 120
FEED_DOWN_SECONDS = 300


def fmt_price(inst: Instrument, p: float) -> str:
    return f"{p:.{inst.decimals}f}"


class Engine:
    def __init__(self, db: DB, store: SettingsStore, prices: PriceCache, feeds: dict,
                 ai: AIClient, tg: Telegram):
        self.db, self.store, self.prices = db, store, prices
        self.feeds, self.ai, self.tg = feeds, ai, tg
        self.broker = PaperBroker(db, prices)
        self.last_eval: dict[str, int] = {}
        self.cooldown: dict[tuple[str, str], datetime] = {}
        self.feed_down_alerted: set[str] = set()
        self.last_snapshot: datetime | None = None
        self.last_filter_reason: dict[str, str] = {}
        self._signal_lock = asyncio.Lock()

    @property
    def cfg(self) -> dict:
        return self.store.current

    # ================================================================ startup
    async def bootstrap(self) -> None:
        current_playbook(self.db)
        for sym, inst in INSTRUMENTS.items():
            try:
                candles = await fetch_candles(self.feeds, inst)
                if candles:
                    store_candles(self.db, sym, candles)
                    # don't fire on the history we just loaded
                    self.last_eval[sym] = candles[-1]["t"]
                    self.last_filter_reason[sym] = "waiting for the next 15m candle"
                log.info("Loaded %d candles for %s", len(candles), sym)
            except Exception as e:  # noqa: BLE001
                log.warning("History load failed for %s: %s", sym, e)
        st = self.broker.state(self.cfg)
        now = datetime.now(timezone.utc)
        if self.db.kv_get("day") is None:
            self.db.kv_set("day", now.strftime("%Y-%m-%d"))
            self.db.kv_set("day_start_equity", st.equity)
        if self.db.kv_get("week") is None:
            self.db.kv_set("week", now.strftime("%G-W%V"))
            self.db.kv_set("week_start_equity", st.equity)

    # ============================================================= fast loop
    async def fast_loop(self) -> None:
        while True:
            try:
                await self.fast_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("fast tick failed")
            await asyncio.sleep(self.cfg["engine"]["stop_check_seconds"])

    async def fast_tick(self) -> None:
        cfg = self.cfg
        for t in self.broker.check_exits(cfg):
            await self._alert_close(t)
        for t in self.broker.check_stop_out(cfg):
            await self._alert_close(t)
        st = self.broker.state(cfg)
        self.broker.save_equity(st)
        await self._check_limits(st)

    async def _check_limits(self, st) -> None:
        if st.status == "halted":
            return
        hit = risk.account_limit_status(self.cfg, st.equity, st.peak,
                                        self.db.kv_get("day_start_equity", st.equity),
                                        self.db.kv_get("week_start_equity", st.equity))
        if not hit:
            return
        status, reason = hit
        # only escalate: active -> paused_daily -> paused_weekly -> halted
        rank = {"active": 0, "paused": 1, "paused_daily": 1, "paused_weekly": 2, "halted": 3}
        if rank[status] <= rank.get(st.status, 0):
            return
        self.set_status(status, reason)
        if status == "halted":
            closed = self.broker.close_all(self.cfg, "kill switch")
            await self.tg.send(f"🛑 <b>KILL SWITCH</b>\n{reason}\nClosed {len(closed)} positions. "
                               f"Bot halted until you press Resume.")
        else:
            await self.tg.send(f"⏸ <b>Paused</b> ({status})\n{reason}\nOpen positions keep their stops.")

    def set_status(self, status: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE account SET status=?, status_reason=? WHERE id=1", (status, reason))
        log.info("Status -> %s (%s)", status, reason)

    # =========================================================== minute loop
    async def minute_loop(self) -> None:
        while True:
            started = asyncio.get_event_loop().time()
            try:
                await self.minute_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("minute tick failed")
            await asyncio.sleep(max(5, 60 - (asyncio.get_event_loop().time() - started)))

    async def minute_tick(self) -> None:
        now = datetime.now(timezone.utc)
        await self._rollover(now)
        await self._refresh_candles(now)
        await self._weekend(now)
        await self._feed_health(now)
        await self._evaluate_signals(now)
        if not self.last_snapshot or now - self.last_snapshot >= timedelta(minutes=15):
            self.snapshot()
            self.last_snapshot = now

    async def _rollover(self, now: datetime) -> None:
        st = self.broker.state(self.cfg)
        day, week = now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")
        if self.db.kv_get("day") != day:
            self.db.kv_set("day", day)
            self.db.kv_set("day_start_equity", st.equity)
            if st.status == "paused_daily":
                self.set_status("active", None)
                await self.tg.send("▶️ New day - daily pause lifted.")
        if self.db.kv_get("week") != week:
            self.db.kv_set("week", week)
            self.db.kv_set("week_start_equity", st.equity)
            self.db.execute("UPDATE positions SET weekend_halved=0")
            if st.status in ("paused_weekly", "paused_daily"):
                self.set_status("active", None)
                await self.tg.send("▶️ New week - weekly pause lifted. Check the weekly review on the dashboard.")

    async def _refresh_candles(self, now: datetime) -> None:
        for sym, inst in INSTRUMENTS.items():
            if not market_open(inst, now - timedelta(minutes=16)) and not market_open(inst, now):
                continue
            try:
                candles = await fetch_candles(self.feeds, inst, 6)
                if candles:
                    store_candles(self.db, sym, candles)
            except Exception as e:  # noqa: BLE001
                log.warning("candle refresh %s failed: %s", sym, e)

    async def _weekend(self, now: datetime) -> None:
        w = self.cfg["weekend"]
        mins = minutes_to_weekly_close(now)
        if mins is None or mins > w["minutes_before_close"] or w["mode"] == "hold":
            return
        for p in self.broker.positions():
            inst = INSTRUMENTS[p["symbol"]]
            if not is_forex_like(inst):
                continue
            if w["mode"] == "close":
                t = self.broker.close_at_market(self.cfg, p, "weekend close")
            elif w["mode"] == "halve" and not p["weekend_halved"]:
                t = self.broker.close_at_market(self.cfg, p, "weekend halve", fraction=0.5)
            else:
                t = None
            if t:
                await self._alert_close(t)

    async def _feed_health(self, now: datetime) -> None:
        for sym, inst in INSTRUMENTS.items():
            if not market_open(inst, now):
                continue
            age = self.prices.age_seconds(sym)
            down = age is None or age > FEED_DOWN_SECONDS
            # a quiet market at night is fine as long as the stream itself is alive (heartbeats)
            feed = self.prices.feed(inst.source)
            if down and feed.last_message and (now - feed.last_message).total_seconds() < FEED_DOWN_SECONDS:
                down = False
            if down and sym not in self.feed_down_alerted:
                self.feed_down_alerted.add(sym)
                await self.tg.send(f"⚠️ Data feed down: no {sym} price for 5+ min ({feed.last_error or 'no error'})")
            elif not down and sym in self.feed_down_alerted:
                self.feed_down_alerted.discard(sym)
                await self.tg.send(f"✅ {sym} price feed back.")

    # ========================================================= signal engine
    async def _evaluate_signals(self, now: datetime) -> None:
        cfg = self.cfg
        async with self._signal_lock:
            for sym, inst in INSTRUMENTS.items():
                if not cfg["instruments"].get(sym) or not market_open(inst, now):
                    continue
                candles = load_candles(self.db, sym, 2000)
                if not candles or candles[-1]["t"] <= self.last_eval.get(sym, 0):
                    continue
                self.last_eval[sym] = candles[-1]["t"]
                news_bias = bias_for(self.db, sym) if cfg["news"]["enabled"] else None
                sigs, why_not = evaluate(inst, candles, cfg, now, news_bias, scorecard.disabled_for(self.db, sym))
                self.last_filter_reason[sym] = why_not or ("signal" if sigs else "no setup")
                for sig in sigs:
                    await self.handle_signal(inst, sig, candles, now)

    def _record(self, sig: Signal, accepted: bool, reason: str | None, ai_used: bool = False,
                ai_reply: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions(at, symbol, signal, side, signal_note, ai_used, ai_reply, accepted, reject_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (iso(), sig.symbol, sig.type, sig.side, sig.note, int(ai_used), ai_reply, int(accepted), reason))
        return int(cur.lastrowid)

    def _pre_checks(self, inst: Instrument, sig: Signal, now: datetime, positions: list[dict]) -> str | None:
        cfg = self.cfg
        acc = self.db.one("SELECT status, status_reason FROM account WHERE id=1")
        if acc["status"] != "active":
            return f"bot {acc['status']}"
        if is_forex_like(inst):
            mins = minutes_to_weekly_close(now)
            if mins is not None and mins <= cfg["weekend"]["no_new_trades_minutes"]:
                return "too close to the weekly close"
        if cfg["news"]["enabled"]:
            ev = blackout(self.db, cfg, inst.currencies, now)
            if ev:
                return f"news blackout: {ev['currency']} {ev['title']} at {ev['at'][11:16]} UTC"
            b = bias_for(self.db, inst.symbol)
            if cfg["news"]["block_on_extreme_risk"] and b and b.get("risk") == "extreme":
                return f"news risk extreme: {b.get('summary', '')}"
        why = risk.portfolio_check(inst, cfg, positions)
        if why:
            return why
        until = self.cooldown.get((sig.symbol, sig.type))
        if until and now < until:
            return f"cooldown after AI skip until {until.strftime('%H:%M')} UTC"
        age = self.prices.age_seconds(sig.symbol)
        if age is None or age > STALE_PRICE_SECONDS:
            return "no fresh live price"
        return None

    async def handle_signal(self, inst: Instrument, sig: Signal, candles: list[dict], now: datetime) -> None:
        cfg = self.cfg
        positions = self.broker.positions()
        why = self._pre_checks(inst, sig, now, positions)
        if why:
            self._record(sig, False, why)
            return

        st = self.broker.state(cfg)
        q = self.prices.get(sig.symbol)
        entry = self.broker.entry_price(cfg, inst, sig.side, q)
        # cheap dry-run with default stops so we don't pay for AI on an impossible trade
        dry = risk.size_order(inst, cfg, sig.side, entry, sig.atr, st.equity, st.margin_used)
        if isinstance(dry, risk.Rejection) and "leverage" not in dry.reason:
            self._record(sig, False, dry.reason)
            return

        ai_reply, ai_used, reason = None, False, sig.note
        stop_atr = target_atr = None
        block = self.ai.budget_block(cfg)
        if block:
            if cfg["ai"]["when_unavailable"] != "rules_only":
                self._record(sig, False, f"AI unavailable ({block}) and settings say skip")
                return
            reason = f"[rules only - {block}] {sig.note}"
        else:
            prompt = gate.build_prompt(
                inst, sig, candles,
                {"equity": st.equity, "margin_used_pct": st.margin_used_pct, "status": st.status},
                positions,
                self.db.all("SELECT * FROM trades ORDER BY id DESC LIMIT 10"),
                scorecard.line_for(self.db, sig.symbol, sig.type),
                current_playbook(self.db),
                self._news_context(inst, now),
                session_name(now),
                round(q.spread, inst.decimals + 1) if q else None,
            )
            res = await gate.ask(self.ai, cfg, prompt)
            ai_used = True
            ai_reply = gate.dumps(res.data) if res.ok else res.error
            if not res.ok:
                self._record(sig, False, f"AI error -> SKIP ({res.error})", ai_used, ai_reply)
                return
            take, why_not = gate.validate_reply(res.data, sig, cfg["ai"]["min_confidence"])
            if not take:
                self.cooldown[(sig.symbol, sig.type)] = now + AI_SKIP_COOLDOWN
                self._record(sig, False, why_not, ai_used, ai_reply)
                return
            reason = str(res.data.get("reason", ""))[:300]
            stop_atr, target_atr = res.data["stop_atr"], res.data["target_atr"]

        # re-price at the live quote after the AI call (a few seconds may have passed)
        q = self.prices.get(sig.symbol)
        entry = self.broker.entry_price(cfg, inst, sig.side, q)
        st = self.broker.state(cfg)
        order = risk.size_order(inst, cfg, sig.side, entry, sig.atr, st.equity, st.margin_used,
                                stop_atr, target_atr)
        if isinstance(order, risk.Rejection):
            self._record(sig, False, order.reason, ai_used, ai_reply)
            return

        if cfg["trading_mode"] == "signals_only":
            self._record(sig, False, "signals_only mode - would have taken this", ai_used, ai_reply)
            await self.tg.send(self._open_text(inst, order, sig, reason, would=True))
            return

        decision_id = self._record(sig, True, None, ai_used, ai_reply)
        self.broker.open(cfg, order, sig.type, reason, decision_id)
        await self.tg.send(self._open_text(inst, order, sig, reason))

    def _news_context(self, inst: Instrument, now: datetime) -> dict:
        if not self.cfg["news"]["enabled"]:
            return {}
        return {
            "digest": bias_for(self.db, inst.symbol),
            "events": events_between(self.db, now, now + timedelta(hours=4), inst.currencies, ("High", "Medium")),
            "headlines": recent_headlines(self.db, inst.symbol, hours=6, limit=6),
        }

    # =============================================================== helpers
    def _open_text(self, inst: Instrument, o: risk.Order, sig: Signal, reason: str, would: bool = False) -> str:
        head = "👀 <b>Would open</b> (signals-only)" if would else "🟢 <b>Opened</b>"
        return (f"{head} {o.side} {inst.display}\n"
                f"entry {fmt_price(inst, o.entry)} · stop {fmt_price(inst, o.stop)} · target {fmt_price(inst, o.target)}\n"
                f"size {o.units:.4g} units · {o.effective_leverage:.1f}x · risk ${o.risk_usd:.2f}"
                f"{' (shrunk to fit cap)' if o.shrunk else ''}\n"
                f"{sig.type}: {reason}")

    async def _alert_close(self, t: dict) -> None:
        inst = INSTRUMENTS[t["symbol"]]
        icon = "✅" if t["pnl_usd"] > 0 else "🔴"
        r = f" ({t['pnl_r']:+.2f}R)" if t.get("pnl_r") is not None else ""
        await self.tg.send(f"{icon} <b>Closed</b> {t['side']} {inst.display} - {t['exit_reason']}\n"
                           f"{fmt_price(inst, t['entry'])} → {fmt_price(inst, t['exit'])} · "
                           f"P&L ${t['pnl_usd']:+.2f}{r}")

    def snapshot(self) -> None:
        st = self.broker.state(self.cfg)
        bench = self.db.kv_get("bench_start")
        btc, gold = self.prices.get("BTC_USDT"), self.prices.get("XAU_USD")
        if not bench and btc and gold:
            bench = {"btc": btc.mid, "gold": gold.mid, "balance": st.starting_balance}
            self.db.kv_set("bench_start", bench)
        b_btc = bench["balance"] * btc.mid / bench["btc"] if bench and btc else None
        b_gold = bench["balance"] * gold.mid / bench["gold"] if bench and gold else None
        self.db.execute("INSERT OR REPLACE INTO equity_snapshots(at, equity, cash, margin_used, btc_benchmark, "
                        "gold_benchmark) VALUES (?,?,?,?,?,?)",
                        (iso(), st.equity, st.cash, st.margin_used, b_btc, b_gold))

    # ============================================================== controls
    def pause(self) -> None:
        self.set_status("paused", "paused manually")

    def resume(self) -> None:
        st = self.broker.state(self.cfg)
        # after a kill switch, the drawdown is measured from here so it doesn't re-trigger at once
        self.db.execute("UPDATE account SET peak=? WHERE id=1", (st.equity,))
        self.db.kv_set("day_start_equity", st.equity)
        self.db.kv_set("week_start_equity", st.equity)
        self.set_status("active", None)

    async def close_all(self, reason: str = "closed manually") -> int:
        closed = self.broker.close_all(self.cfg, reason)
        for t in closed:
            await self._alert_close(t)
        return len(closed)

    async def kill(self) -> int:
        n = await self.close_all("kill switch (manual)")
        self.set_status("halted", "kill switch pressed manually")
        await self.tg.send("🛑 Kill switch pressed from the dashboard. Bot halted.")
        return n

    async def close_position(self, pos_id: int) -> bool:
        p = next((p for p in self.broker.positions() if p["id"] == pos_id), None)
        if not p:
            return False
        t = self.broker.close_at_market(self.cfg, p, "closed manually")
        if t:
            await self._alert_close(t)
        return bool(t)

    def status_text(self) -> str:
        st = self.broker.state(self.cfg)
        pos = self.broker.positions()
        lines = [f"<b>Status</b>: {st.status}" + (f" ({st.status_reason})" if st.status_reason else ""),
                 f"Equity ${st.equity:.2f} (start ${st.starting_balance:.2f}, {100 * (st.equity / st.starting_balance - 1):+.1f}%)",
                 f"Margin used {st.margin_used_pct:.0f}% · open {len(pos)}",
                 f"AI spend this month ${self.ai.month_spend():.3f} · calls today {self.ai.calls_today()}"]
        for p in pos:
            u = self.broker.unrealized(self.cfg, p)
            lines.append(f"• {p['side']} {p['symbol']} @ {p['entry']} · P&L ${u or 0:+.2f}")
        return "\n".join(lines)
