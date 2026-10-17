"""Paper broker simulating an XM Standard (MT5) account. One instance serves all 3 accounts.

* Quotes: XM-style bid/ask around the feed mid, using XM's typical spread
  (settings -> Broker) - or the live feed spread if that's wider.
* Fills: buys at the ask, sells at the bid. No commission (XM Standard).
* Stops: checked every few seconds against bid/ask, only while the real market
  is open. If price gapped through the stop, the fill is the first price seen.
* Targets: filled at the target price (never better - conservative).
* Breakeven: optional per mode - at +X R the stop moves to the entry price.
* Swap: XM points per lot per night at 17:00 New York; forex/gold x3 on
  Wednesday, BTCUSD charged every night including weekends.
* Margin = notional / XM leverage. Stop-out: if margin level < 20% (setting),
  the biggest losing position is closed, as XM does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import DB, iso
from .feeds.prices import PriceCache, Quote
from .instruments import INSTRUMENTS, Instrument, pnl_usd, quote_to_usd
from .risk import Order
from .sessions import NY, market_open

log = logging.getLogger("broker")


@dataclass
class AccountState:
    account: str
    cash: float               # MT5 "Balance"
    equity: float
    peak: float
    margin_used: float
    unrealized: float
    status: str
    status_reason: str | None
    starting_balance: float

    @property
    def margin_level_pct(self) -> float | None:
        return 100 * self.equity / self.margin_used if self.margin_used > 0 else None

    @property
    def margin_used_pct(self) -> float:
        return 100 * self.margin_used / self.equity if self.equity > 0 else 0.0

    @property
    def free_margin(self) -> float:
        return self.equity - self.margin_used


class PaperBroker:
    def __init__(self, db: DB, prices: PriceCache):
        self.db = db
        self.prices = prices

    # ------------------------------------------------------------------ prices
    @staticmethod
    def broker_quote(cfg: dict, inst: Instrument, q: Quote) -> tuple[float, float]:
        """Bid/ask as XM would quote them: never tighter than XM's spread."""
        spread = cfg["broker"].get(f"spread_{inst.symbol}", 0) * inst.point
        if q.ask - q.bid >= spread:
            return q.bid, q.ask
        return q.mid - spread / 2, q.mid + spread / 2

    def entry_price(self, cfg: dict, inst: Instrument, side: str, q: Quote) -> float:
        bid, ask = self.broker_quote(cfg, inst, q)
        return ask if side == "LONG" else bid

    def exit_price(self, cfg: dict, inst: Instrument, side: str, q: Quote) -> float:
        bid, ask = self.broker_quote(cfg, inst, q)
        return bid if side == "LONG" else ask

    # --------------------------------------------------------------- positions
    def positions(self, account: str | None = None) -> list[dict]:
        if account:
            rows = self.db.all("SELECT * FROM positions WHERE account=? ORDER BY id", (account,))
        else:
            rows = self.db.all("SELECT * FROM positions ORDER BY id")
        for p in rows:
            p["asset_class"] = INSTRUMENTS[p["symbol"]].asset_class
        return rows

    def unrealized(self, cfg: dict, p: dict) -> float | None:
        q = self.prices.get(p["symbol"])
        if not q:
            return None
        inst = INSTRUMENTS[p["symbol"]]
        return pnl_usd(inst, p["side"], p["units"], p["entry"], self.exit_price(cfg, inst, p["side"], q))

    def state(self, cfg: dict, account: str) -> AccountState:
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (account,))
        positions = self.positions(account)
        unreal = sum((self.unrealized(cfg, p) or 0.0) for p in positions)
        margin = sum(p["margin"] for p in positions)
        equity = acc["cash"] + unreal
        return AccountState(account, acc["cash"], equity, max(acc["peak"], equity), margin, unreal,
                            acc["status"], acc["status_reason"], acc["starting_balance"])

    def save_equity(self, st: AccountState) -> None:
        self.db.execute("UPDATE accounts SET equity=?, peak=? WHERE id=?", (st.equity, st.peak, st.account))

    def _cash(self, account: str, delta: float) -> None:
        self.db.execute("UPDATE accounts SET cash = cash + ? WHERE id=?", (delta, account))

    def open(self, cfg: dict, account: str, order: Order, signal: str, ai_reason: str | None,
             decision_id: int | None, regime: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO positions(account, symbol, side, lots, units, leverage, entry, stop, initial_stop, target, "
            "margin, risk_usd, signal, regime, ai_reason, decision_id, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, order.symbol, order.side, order.lots, order.units, order.leverage, order.entry, order.stop,
             order.stop, order.target, order.margin, order.risk_usd, signal, regime, ai_reason, decision_id, iso()))
        return int(cur.lastrowid)

    def close(self, cfg: dict, p: dict, price: float, reason: str, fraction: float = 1.0) -> dict:
        """Close all (fraction=1) or part of a position at `price`. Returns the trade row."""
        inst = INSTRUMENTS[p["symbol"]]
        lots = round(p["lots"] * fraction, 2) if fraction < 1 else p["lots"]
        frac = lots / p["lots"] if p["lots"] else 1.0
        units = p["units"] * frac
        gross = pnl_usd(inst, p["side"], units, p["entry"], price)
        fees, swap, funding = p["fees"] * frac, p["swap"] * frac, p["funding"] * frac
        net = gross - fees + swap + funding
        risk = p["risk_usd"] * frac
        trade = {
            "account": p["account"], "ticket": p["id"], "symbol": p["symbol"], "side": p["side"],
            "lots": lots, "units": units, "leverage": p["leverage"],
            "entry": p["entry"], "exit": price, "stop": p["stop"], "target": p["target"],
            "pnl_usd": net, "pnl_r": net / risk if risk > 0 else None, "fees": fees, "swap": swap,
            "funding": funding, "signal": p["signal"], "regime": p.get("regime"), "ai_reason": p["ai_reason"],
            "exit_reason": reason, "opened_at": p["opened_at"], "closed_at": iso(),
        }
        cols = ",".join(trade)
        self.db.execute(f"INSERT INTO trades({cols}) VALUES ({','.join('?' * len(trade))})", list(trade.values()))
        self._cash(p["account"], gross)
        if frac >= 0.999:
            self.db.execute("DELETE FROM positions WHERE id=?", (p["id"],))
        else:
            keep = 1 - frac
            self.db.execute(
                "UPDATE positions SET lots=ROUND(lots-?, 2), units=units*?, margin=margin*?, risk_usd=risk_usd*?, fees=fees*?, "
                "swap=swap*?, funding=funding*?, weekend_halved=1 WHERE id=?",
                (lots, keep, keep, keep, keep, keep, keep, p["id"]))
        log.info("[%s] Closed %s %s %.2f lots @ %s (%s) net %.2f", p["account"], p["side"], p["symbol"],
                 lots, price, reason, net)
        return trade

    def close_at_market(self, cfg: dict, p: dict, reason: str, fraction: float = 1.0) -> dict | None:
        q = self.prices.get(p["symbol"])
        if not q:
            return None
        inst = INSTRUMENTS[p["symbol"]]
        if fraction < 1 and round(p["lots"] * fraction, 2) < inst.min_lot:
            fraction = 1.0          # can't halve 0.01 lot - close it
        return self.close(cfg, p, self.exit_price(cfg, inst, p["side"], q), reason, fraction)

    def check_exits(self, cfg_for, now: datetime | None = None) -> list[dict]:
        """Stops, targets and breakeven against live bid/ask - only while the real market is open.
        cfg_for: callable(account) -> that account's cfg."""
        closed = []
        now = now or datetime.now(timezone.utc)
        for p in self.positions():
            q = self.prices.get(p["symbol"])
            inst = INSTRUMENTS[p["symbol"]]
            if not q or not market_open(inst, now):
                continue
            cfg = cfg_for(p["account"])
            px = self.exit_price(cfg, inst, p["side"], q)
            long = p["side"] == "LONG"
            if (long and px <= p["stop"]) or (not long and px >= p["stop"]):
                be = abs(p["stop"] - p["entry"]) < inst.point / 2
                gap = (px < p["stop"] - inst.point) if long else (px > p["stop"] + inst.point)
                reason = "breakeven stop" if be else "stop loss"
                closed.append(self.close(cfg, p, px, reason + (" (gap)" if gap else "")))
                continue
            if (long and px >= p["target"]) or (not long and px <= p["target"]):
                closed.append(self.close(cfg, p, p["target"], "take profit"))
                continue
            be_r = cfg["exits"].get("breakeven_at_r", 0)
            if be_r and p["stop"] != p["entry"]:
                init_risk = abs(p["entry"] - p["initial_stop"])
                moved = (px - p["entry"]) if long else (p["entry"] - px)
                if init_risk > 0 and moved >= be_r * init_risk:
                    self.db.execute("UPDATE positions SET stop=? WHERE id=?", (p["entry"], p["id"]))
                    log.info("[%s] %s #%d stop moved to breakeven", p["account"], p["symbol"], p["id"])
        return closed

    def check_stop_out(self, cfg: dict, account: str) -> list[dict]:
        closed = []
        level = cfg["broker"]["stop_out_pct"]
        for _ in range(5):
            st = self.state(cfg, account)
            ml = st.margin_level_pct
            if ml is None or ml >= level:
                break
            losers = sorted(((self.unrealized(cfg, p) or 0, p) for p in self.positions(account)), key=lambda x: x[0])
            if not losers:
                break
            t = self.close_at_market(cfg, losers[0][1], f"stop out (margin level {ml:.0f}%)")
            if not t:
                break
            closed.append(t)
        return closed

    def close_all(self, cfg: dict, account: str, reason: str, only: set[str] | None = None) -> list[dict]:
        out = []
        for p in self.positions(account):
            if only and p["asset_class"] not in only:
                continue
            t = self.close_at_market(cfg, p, reason)
            if t:
                out.append(t)
        return out

    # -------------------------------------------------------------------- swap
    def apply_swap(self, cfg: dict, now: datetime) -> float:
        """Run once a night at 17:00 New York (XM rollover)."""
        ny = now.astimezone(NY)
        total = 0.0
        for p in self.positions():
            inst = INSTRUMENTS[p["symbol"]]
            if inst.asset_class == "crypto":
                mult = 1                                   # crypto CFDs: every night, incl. weekends
            else:
                if ny.weekday() >= 5:
                    continue                               # no FX/gold rollover on Sat/Sun
                mult = 3 if ny.weekday() == 2 else 1       # Wednesday triple swap
            points = cfg["broker"].get(f"swap_{p['symbol']}_{p['side'].lower()}", 0.0)
            q = self.prices.get(p["symbol"])
            price = q.mid if q else p["entry"]
            amount = quote_to_usd(inst, points * inst.point * inst.contract_size * p["lots"], price) * mult
            self.db.execute("UPDATE positions SET swap = swap + ? WHERE id=?", (amount, p["id"]))
            self._cash(p["account"], amount)
            total += amount
        return total
