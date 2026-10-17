"""The trading engine: one set of prices, three paper accounts (standard / moderate / aggressive).

Loops (all in one asyncio process):
* fast loop  (every few seconds): stops/targets/breakeven, stop-out, equity,
  loss limits and daily profit target - for every account
* minute loop (every 60s): refresh M5 + M15 candles, weekend handling, feed
  health, run each account's signals on newly closed candles of ITS timeframe,
  equity snapshots every 15 min
* scheduled jobs (APScheduler, see main.py): swap, news, daily review,
  weekly playbook, backups
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import adaptive, risk, scorecard
from .ai import gate
from .ai.client import AIClient
from .ai.review import current_playbook
from .alerts import Telegram
from .broker import PaperBroker
from .db import DB, iso
from .feeds import fetch_candles
from .feeds.prices import PriceCache, Quote, load_candles, store_candles, tf_seconds
from .indicators import atr as atr_series
from .instruments import INSTRUMENTS, Instrument
from .news.digest import bias_for
from .news.sources import blackout, events_between, recent_headlines
from .sessions import is_forex_like, market_open, minutes_to_weekly_close, session_name
from .settings_store import MODE_LABELS, MODES, SettingsStore
from .signals import Signal, detect_regime, evaluate

log = logging.getLogger("engine")

AI_SKIP_COOLDOWN = timedelta(hours=2)
STALE_PRICE_SECONDS = 120
FEED_DOWN_SECONDS = 300
HISTORY = {"15m": None, "5m": 1500}      # candles to load at startup (None = source maximum)


def fmt_price(inst: Instrument, p: float) -> str:
    return f"{p:.{inst.decimals}f}"


class Engine:
    def __init__(self, db: DB, store: SettingsStore, prices: PriceCache, feeds: dict,
                 ai: AIClient, tg: Telegram):
        self.db, self.store, self.prices = db, store, prices
        self.feeds, self.ai, self.tg = feeds, ai, tg
        self.broker = PaperBroker(db, prices)
        self.last_eval: dict[tuple[str, str, str], int] = {}       # (account, symbol, tf) -> candle t
        self.cooldown: dict[tuple[str, str, str], datetime] = {}   # (account, symbol, signal)
        self.feed_down_alerted: set[str] = set()
        self.last_snapshot: datetime | None = None
        self.last_check: dict[tuple[str, str], str] = {}           # (account, symbol) -> last filter result
        self.last_atr: dict[tuple[str, str], tuple[int, float]] = {}  # (symbol, tf) -> (candle t, ATR)
        self.regimes: dict[tuple[str, str], str] = {}              # (symbol, tf) -> "trend (ADX 31, ...)"
        self._signal_lock = asyncio.Lock()

    def cfg(self, account: str) -> dict:
        return self.store.cfg(account)

    def timeframes(self) -> set[str]:
        return {self.cfg(m)["timeframe"] for m in self.store.active_modes()} or {"15m"}

    # ================================================================ startup
    async def bootstrap(self) -> None:
        for m in MODES:
            current_playbook(self.db, m)
        for tf in ("15m", "5m"):
            for sym, inst in INSTRUMENTS.items():
                try:
                    candles = await fetch_candles(self.feeds, inst, HISTORY[tf], tf)
                    store_candles(self.db, sym, candles, tf)
                    log.info("Loaded %d %s candles for %s", len(candles), tf, sym)
                except Exception as e:  # noqa: BLE001
                    log.warning("History load failed for %s %s: %s", sym, tf, e)
        # markets closed right now (weekend): show Friday's close, like MT5, not a 24/7 venue's weekend price
        now = datetime.now(timezone.utc)
        for sym, inst in INSTRUMENTS.items():
            if not market_open(inst, now):
                last = load_candles(self.db, sym, 1, "15m")
                if last:
                    c = last[-1]["c"]
                    closed_at = datetime.fromtimestamp(last[-1]["t"] + tf_seconds("15m"), tz=timezone.utc)
                    self.prices.quotes[sym] = Quote(c, c, closed_at)
        for tf in ("15m", "5m"):
            cfg = self.cfg("aggressive" if tf == "5m" else "standard")
            for sym in INSTRUMENTS:
                reg = detect_regime(load_candles(self.db, sym, 6000 if tf == "5m" else 2000, tf), cfg)
                if reg:
                    self.regimes[(sym, tf)] = str(reg)
        # don't fire signals on the history we just loaded
        for m in MODES:
            tf = self.cfg(m)["timeframe"]
            for sym in INSTRUMENTS:
                rows = load_candles(self.db, sym, 1, tf)
                if rows:
                    self.last_eval[(m, sym, tf)] = rows[-1]["t"]
                self.last_check[(m, sym)] = f"waiting for the next {tf} candle"
        now = datetime.now(timezone.utc)
        for m in MODES:
            st = self.broker.state(self.cfg(m), m)
            if self.db.kv_get(f"day:{m}") is None:
                self.db.kv_set(f"day:{m}", now.strftime("%Y-%m-%d"))
                self.db.kv_set(f"day_start_equity:{m}", st.equity)
            if self.db.kv_get(f"week:{m}") is None:
                self.db.kv_set(f"week:{m}", now.strftime("%G-W%V"))
                self.db.kv_set(f"week_start_equity:{m}", st.equity)

    # ============================================================= fast loop
    async def fast_loop(self) -> None:
        while True:
            try:
                await self.fast_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("fast tick failed")
            await asyncio.sleep(self.store.current["engine"]["stop_check_seconds"])

    async def fast_tick(self) -> None:
        closed = self.broker.check_exits(self.cfg)
        for m in MODES:
            closed += self.broker.check_stop_out(self.cfg(m), m)
        for t in closed:
            await self._alert_close(t)
        for m in {t["account"] for t in closed}:
            await self.learn(m)
        for m in MODES:
            cfg = self.cfg(m)
            st = self.broker.state(cfg, m)
            self.broker.save_equity(st)
            await self._check_limits(m, st)

    async def _check_limits(self, account: str, st) -> None:
        if st.status == "halted":
            return
        hit = risk.account_limit_status(self.cfg(account), st.equity, st.peak,
                                        self.db.kv_get(f"day_start_equity:{account}", st.equity),
                                        self.db.kv_get(f"week_start_equity:{account}", st.equity))
        if not hit:
            return
        status, reason = hit
        # only escalate: active -> target_hit/paused_daily -> paused_weekly -> halted
        rank = {"active": 0, "paused": 1, "target_hit": 1, "paused_daily": 1, "paused_weekly": 2, "halted": 3}
        if rank[status] <= rank.get(st.status, 0):
            return
        self.set_status(account, status, reason)
        label = MODE_LABELS[account]
        if status == "halted":
            closed = self.broker.close_all(self.cfg(account), account, "kill switch")
            await self.tg.send(f"🛑 <b>[{label}] KILL SWITCH</b>\n{reason}\nClosed {len(closed)} positions. "
                               f"Account halted until you press Resume.")
        elif status == "target_hit":
            await self.tg.send(f"🎯 <b>[{label}] Daily target hit</b>\n{reason}\nNo new trades until tomorrow; "
                               f"open positions keep their stops.")
        else:
            await self.tg.send(f"⏸ <b>[{label}] Paused</b> ({status})\n{reason}\nOpen positions keep their stops.")

    def set_status(self, account: str, status: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE accounts SET status=?, status_reason=? WHERE id=?", (status, reason, account))
        log.info("[%s] status -> %s (%s)", account, status, reason)

    # =========================================================== minute loop
    async def minute_loop(self) -> None:
        while True:
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                await self.minute_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("minute tick failed")
            await asyncio.sleep(max(5, 60 - (loop.time() - started)))

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
        day, week = now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")
        for m in MODES:
            st = self.broker.state(self.cfg(m), m)
            label = MODE_LABELS[m]
            if self.db.kv_get(f"day:{m}") != day:
                self.db.kv_set(f"day:{m}", day)
                self.db.kv_set(f"day_start_equity:{m}", st.equity)
                if st.status in ("paused_daily", "target_hit"):
                    self.set_status(m, "active", None)
                    await self.tg.send(f"▶️ [{label}] New day - trading resumed.")
            if self.db.kv_get(f"week:{m}") != week:
                self.db.kv_set(f"week:{m}", week)
                self.db.kv_set(f"week_start_equity:{m}", st.equity)
                self.db.execute("UPDATE positions SET weekend_halved=0 WHERE account=?", (m,))
                if st.status in ("paused_weekly", "paused_daily", "target_hit"):
                    self.set_status(m, "active", None)
                    await self.tg.send(f"▶️ [{label}] New week - weekly pause lifted.")

    async def _refresh_candles(self, now: datetime) -> None:
        for tf in self.timeframes():
            for sym, inst in INSTRUMENTS.items():
                if not market_open(inst, now - timedelta(minutes=16)) and not market_open(inst, now):
                    continue
                try:
                    candles = await fetch_candles(self.feeds, inst, 6, tf)
                    if candles:
                        store_candles(self.db, sym, candles, tf)
                except Exception as e:  # noqa: BLE001
                    log.warning("candle refresh %s %s failed: %s", sym, tf, e)

    async def _weekend(self, now: datetime) -> None:
        mins = minutes_to_weekly_close(now)
        if mins is None:
            return
        for m in MODES:
            cfg = self.cfg(m)
            w = cfg["weekend"]
            if mins > w["minutes_before_close"] or w["mode"] == "hold":
                continue
            for p in self.broker.positions(m):
                if not is_forex_like(INSTRUMENTS[p["symbol"]]):
                    continue
                if w["mode"] == "close":
                    t = self.broker.close_at_market(cfg, p, "weekend close")
                elif w["mode"] == "halve" and not p["weekend_halved"]:
                    t = self.broker.close_at_market(cfg, p, "weekend halve", fraction=0.5)
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
            feed = self.prices.feed(inst.source)
            if down and feed.last_message and (now - feed.last_message).total_seconds() < FEED_DOWN_SECONDS:
                down = False
            if down and sym not in self.feed_down_alerted:
                self.feed_down_alerted.add(sym)
                await self.tg.send(f"⚠️ Data feed down: no {inst.xm} price for 5+ min ({feed.last_error or 'no error'})")
            elif not down and sym in self.feed_down_alerted:
                self.feed_down_alerted.discard(sym)
                await self.tg.send(f"✅ {inst.xm} price feed back.")

    # ========================================================= signal engine
    async def _evaluate_signals(self, now: datetime) -> None:
        async with self._signal_lock:
            for m in self.store.active_modes():
                cfg = self.cfg(m)
                tf = cfg["timeframe"]
                for sym, inst in INSTRUMENTS.items():
                    if not cfg["instruments"].get(sym) or not market_open(inst, now):
                        continue
                    candles = load_candles(self.db, sym, 6000 if tf == "5m" else 2000, tf)
                    if not candles or candles[-1]["t"] <= self.last_eval.get((m, sym, tf), 0):
                        continue
                    self.last_eval[(m, sym, tf)] = candles[-1]["t"]
                    news_bias = bias_for(self.db, sym) if cfg["news"]["enabled"] else None
                    sigs, why_not = evaluate(inst, candles, cfg, now, news_bias)
                    reg = detect_regime(candles, cfg)
                    if reg:
                        self.regimes[(sym, tf)] = str(reg)
                    self.last_check[(m, sym)] = why_not or ("signal" if sigs else "no setup")
                    await self._choose_and_handle(m, inst, sigs, candles, now)

    async def _choose_and_handle(self, account: str, inst: Instrument, sigs: list[Signal],
                                 candles: list[dict], now: datetime) -> None:
        """Adaptive layer: drop strategies that are switched off for this regime, then take the
        one with the best track record here. Probation / weak ones trade at reduced risk."""
        cfg = self.cfg(account)
        allowed = []
        for sig in sigs:
            status, mult, why = adaptive.lookup(self.db, account, cfg, sig.type, sig.regime)
            if status == "off":
                self._record(account, sig, False, f"adaptive: {sig.type} is off in {sig.regime} markets ({why})")
                continue
            allowed.append((adaptive.score(self.db, account, sig.type, sig.regime), mult, status, sig))
        if not allowed:
            return
        allowed.sort(key=lambda x: x[0], reverse=True)
        _, mult, status, sig = allowed[0]
        sig.context["adaptive"] = f"{status}" + (f", risk x{mult:g}" if mult < 1 else "")
        await self.handle_signal(account, inst, sig, candles, now, risk_mult=mult)

    async def learn(self, account: str) -> list[str]:
        changes = adaptive.update(self.db, account, self.cfg(account))
        for c in changes:
            log.info("[%s] adaptive: %s", account, c)
        if changes:
            await self.tg.send(f"🧠 <b>[{MODE_LABELS[account]}] strategy update</b>\n" + "\n".join(f"• {c}" for c in changes))
        return changes

    def _record(self, account: str, sig: Signal, accepted: bool, reason: str | None, ai_used: bool = False,
                ai_reply: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions(at, account, symbol, signal, side, regime, signal_note, ai_used, ai_reply, accepted, "
            "reject_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iso(), account, sig.symbol, sig.type, sig.side, sig.regime, sig.note, int(ai_used), ai_reply,
             int(accepted), reason))
        return int(cur.lastrowid)

    def _pre_checks(self, account: str, inst: Instrument, sig: Signal, now: datetime,
                    positions: list[dict]) -> str | None:
        cfg = self.cfg(account)
        acc = self.db.one("SELECT status FROM accounts WHERE id=?", (account,))
        if acc["status"] != "active":
            return f"account {acc['status']}"
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
        until = self.cooldown.get((account, sig.symbol, sig.type))
        if until and now < until:
            return f"cooldown after AI skip until {until.strftime('%H:%M')} UTC"
        age = self.prices.age_seconds(sig.symbol)
        if age is None or age > STALE_PRICE_SECONDS:
            return "no fresh live price"
        return None

    async def handle_signal(self, account: str, inst: Instrument, sig: Signal, candles: list[dict],
                            now: datetime, risk_mult: float = 1.0) -> None:
        cfg = self.cfg(account)
        positions = self.broker.positions(account)
        why = self._pre_checks(account, inst, sig, now, positions)
        if why:
            self._record(account, sig, False, why)
            return

        st = self.broker.state(cfg, account)
        q = self.prices.get(sig.symbol)
        entry = self.broker.entry_price(cfg, inst, sig.side, q)
        # cheap dry-run with default stops so we don't pay for AI on an impossible trade
        dry = risk.size_order(inst, cfg, sig.side, entry, sig.atr, st.equity, st.margin_used, risk_mult=risk_mult)
        if isinstance(dry, risk.Rejection):
            self._record(account, sig, False, dry.reason)
            return

        ai_reply, ai_used, reason = None, False, sig.note
        stop_atr = target_atr = None
        block = self.ai.budget_block(cfg)
        if block:
            if cfg["ai"]["when_unavailable"] != "rules_only":
                self._record(account, sig, False, f"AI unavailable ({block}) and this mode skips without AI")
                return
            reason = f"[rules only - {block}] {sig.note}"
        else:
            bid, ask = self.broker.broker_quote(cfg, inst, q)
            prompt = gate.build_prompt(
                inst, sig, candles,
                {"equity": st.equity, "margin_used_pct": st.margin_used_pct, "status": st.status},
                positions,
                self.db.all("SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT 10", (account,)),
                scorecard.line_for(self.db, account, sig.symbol, sig.type),
                current_playbook(self.db, account),
                self._news_context(cfg, inst, now),
                session_name(now),
                round((ask - bid) / inst.point),
                cfg,
            )
            res = await gate.ask(self.ai, cfg, prompt)
            ai_used = True
            ai_reply = gate.dumps(res.data) if res.ok else res.error
            if not res.ok:
                self._record(account, sig, False, f"AI error -> SKIP ({res.error})", ai_used, ai_reply)
                return
            take, why_not = gate.validate_reply(res.data, sig, cfg["ai"]["min_confidence"])
            if not take:
                self.cooldown[(account, sig.symbol, sig.type)] = now + AI_SKIP_COOLDOWN
                self._record(account, sig, False, why_not, ai_used, ai_reply)
                return
            reason = str(res.data.get("reason", ""))[:300]
            stop_atr, target_atr = res.data["stop_atr"], res.data["target_atr"]

        # re-price at the live quote after the AI call (a few seconds may have passed)
        q = self.prices.get(sig.symbol)
        entry = self.broker.entry_price(cfg, inst, sig.side, q)
        st = self.broker.state(cfg, account)
        order = risk.size_order(inst, cfg, sig.side, entry, sig.atr, st.equity, st.margin_used,
                                stop_atr, target_atr, risk_mult)
        if isinstance(order, risk.Rejection):
            self._record(account, sig, False, order.reason, ai_used, ai_reply)
            return

        if cfg["trading_mode"] == "signals_only":
            self._record(account, sig, False, "signals_only mode - would have taken this", ai_used, ai_reply)
            await self.tg.send(self._open_text(account, inst, order, sig, reason, would=True))
            return

        decision_id = self._record(account, sig, True, order.note or None, ai_used, ai_reply)
        self.broker.open(cfg, account, order, sig.type, reason, decision_id, sig.regime)
        await self.tg.send(self._open_text(account, inst, order, sig, reason))

    def _news_context(self, cfg: dict, inst: Instrument, now: datetime) -> dict:
        if not cfg["news"]["enabled"]:
            return {}
        return {
            "digest": bias_for(self.db, inst.symbol),
            "events": events_between(self.db, now, now + timedelta(hours=4), inst.currencies, ("High", "Medium")),
            "headlines": recent_headlines(self.db, inst.symbol, hours=6, limit=6),
        }

    # =============================================================== helpers
    def _open_text(self, account: str, inst: Instrument, o: risk.Order, sig: Signal, reason: str,
                   would: bool = False) -> str:
        head = "👀 <b>Would open</b> (signals-only)" if would else "🟢 <b>Opened</b>"
        side = "BUY" if o.side == "LONG" else "SELL"
        return (f"{head} [{MODE_LABELS[account]}] {side} {o.lots:.2f} {inst.xm}\n"
                f"price {fmt_price(inst, o.entry)} · S/L {fmt_price(inst, o.stop)} · T/P {fmt_price(inst, o.target)}\n"
                f"risk ${o.risk_usd:.2f} ({o.risk_pct:.1f}%) · {o.leverage:.1f}x"
                f"{' · ' + o.note if o.note else ''}\n"
                f"{sig.type}: {reason}")

    async def _alert_close(self, t: dict) -> None:
        inst = INSTRUMENTS[t["symbol"]]
        icon = "✅" if t["pnl_usd"] > 0 else "🔴"
        r = f" ({t['pnl_r']:+.2f}R)" if t.get("pnl_r") is not None else ""
        side = "BUY" if t["side"] == "LONG" else "SELL"
        await self.tg.send(f"{icon} <b>Closed</b> [{MODE_LABELS[t['account']]}] {side} {t['lots']:.2f} {inst.xm} "
                           f"- {t['exit_reason']}\n"
                           f"{fmt_price(inst, t['entry'])} → {fmt_price(inst, t['exit'])} · "
                           f"P&L ${t['pnl_usd']:+.2f}{r}")

    def snapshot(self) -> None:
        btc, gold = self.prices.get("BTC_USDT"), self.prices.get("XAU_USD")
        bench = self.db.kv_get("bench_start")
        if not bench and btc and gold:
            bench = {"btc": btc.mid, "gold": gold.mid}
            self.db.kv_set("bench_start", bench)
        at = iso()
        for m in MODES:
            st = self.broker.state(self.cfg(m), m)
            b_btc = st.starting_balance * btc.mid / bench["btc"] if bench and btc else None
            b_gold = st.starting_balance * gold.mid / bench["gold"] if bench and gold else None
            self.db.execute("INSERT OR REPLACE INTO equity_snapshots(at, account, equity, cash, margin_used, "
                            "btc_benchmark, gold_benchmark) VALUES (?,?,?,?,?,?,?)",
                            (at, m, st.equity, st.cash, st.margin_used, b_btc, b_gold))

    def current_atr(self, symbol: str, tf: str) -> float | None:
        rows = load_candles(self.db, symbol, 60, tf)
        if len(rows) < 20:
            return None
        cached = self.last_atr.get((symbol, tf))
        if cached and cached[0] == rows[-1]["t"]:
            return cached[1]
        period = self.store.current["indicators"]["atr_period"]
        value = atr_series([r["h"] for r in rows], [r["l"] for r in rows], [r["c"] for r in rows], period)[-1]
        if value:
            self.last_atr[(symbol, tf)] = (rows[-1]["t"], value)
        return value

    def min_lot_risk(self, account: str, symbol: str, equity: float) -> float | None:
        """% of equity a 0.01 lot risks with this account's default stop (shown in Market Watch)."""
        cfg = self.cfg(account)
        atr = self.current_atr(symbol, cfg["timeframe"])
        q = self.prices.get(symbol)
        if not atr or not q:
            return None
        return risk.min_lot_risk_pct(INSTRUMENTS[symbol], cfg, atr, q.mid, equity)

    # ============================================================== controls
    def pause(self, account: str) -> None:
        self.set_status(account, "paused", "paused manually")

    def resume(self, account: str) -> None:
        st = self.broker.state(self.cfg(account), account)
        # after a kill switch, drawdown is measured from here so it doesn't re-trigger at once
        self.db.execute("UPDATE accounts SET peak=? WHERE id=?", (st.equity, account))
        self.db.kv_set(f"day_start_equity:{account}", st.equity)
        self.db.kv_set(f"week_start_equity:{account}", st.equity)
        self.set_status(account, "active", None)

    async def close_all(self, account: str, reason: str = "closed manually") -> int:
        closed = self.broker.close_all(self.cfg(account), account, reason)
        for t in closed:
            await self._alert_close(t)
        return len(closed)

    async def kill(self, account: str) -> int:
        n = await self.close_all(account, "kill switch (manual)")
        self.set_status(account, "halted", "kill switch pressed manually")
        await self.tg.send(f"🛑 [{MODE_LABELS[account]}] Kill switch pressed from the dashboard. Account halted.")
        return n

    async def close_position(self, pos_id: int) -> bool:
        p = next((p for p in self.broker.positions() if p["id"] == pos_id), None)
        if not p:
            return False
        t = self.broker.close_at_market(self.cfg(p["account"]), p, "closed manually")
        if t:
            await self._alert_close(t)
        return bool(t)

    def status_text(self) -> str:
        lines = []
        for m in MODES:
            cfg = self.cfg(m)
            st = self.broker.state(cfg, m)
            pos = self.broker.positions(m)
            lines.append(f"<b>{MODE_LABELS[m]}</b>: {st.status} · equity ${st.equity:.2f} "
                         f"({100 * (st.equity / st.starting_balance - 1):+.1f}%) · open {len(pos)}")
            for p in pos:
                u = self.broker.unrealized(cfg, p)
                side = "BUY" if p["side"] == "LONG" else "SELL"
                lines.append(f"  • {side} {p['lots']:.2f} {INSTRUMENTS[p['symbol']].xm} @ {p['entry']} · ${u or 0:+.2f}")
        lines.append(f"AI spend this month ${self.ai.month_spend():.3f} · calls today {self.ai.calls_today()}")
        return "\n".join(lines)
