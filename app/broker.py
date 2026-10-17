"""Paper broker: simulates a leveraged account honestly.

* Fills: buys at the ask, sells at the bid. BTC also pays slippage + taker fee.
* Stops: checked every few seconds against bid/ask; if price has jumped past
  the stop (gap), the fill is the first price we see, not the stop.
* Targets: filled at the target price (never better - conservative).
* Swap (gold/forex): charged at 17:00 New York, x3 on Wednesday.
* Funding (BTC perp): every 8h from Binance's live funding rate.
* Stop-out: if margin level < X%, the largest losing position is closed.

Fees, swap and funding hit `cash` the moment they happen, and are also
accumulated on the position so each closed trade shows its full cost.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import DB, iso
from .feeds.prices import PriceCache, Quote
from .instruments import INSTRUMENTS, Instrument, notional_usd, pnl_usd
from .risk import Order
from .sessions import NY, market_open

log = logging.getLogger("broker")


@dataclass
class AccountState:
    cash: float
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


class PaperBroker:
    def __init__(self, db: DB, prices: PriceCache):
        self.db = db
        self.prices = prices

    # ------------------------------------------------------------------ prices
    @staticmethod
    def _slip(cfg: dict, inst: Instrument) -> float:
        return cfg["costs"]["btc_slippage_pct"] / 100 if inst.asset_class == "crypto" else 0.0

    @staticmethod
    def broker_quote(cfg: dict, inst: Instrument, q: Quote) -> tuple[float, float]:
        """Bid/ask as a retail broker would quote them: never tighter than the min spread."""
        min_spread = cfg["costs"].get(f"min_spread_{inst.symbol}", 0.0)
        if q.ask - q.bid >= min_spread:
            return q.bid, q.ask
        return q.mid - min_spread / 2, q.mid + min_spread / 2

    def entry_price(self, cfg: dict, inst: Instrument, side: str, q: Quote) -> float:
        slip = self._slip(cfg, inst)
        bid, ask = self.broker_quote(cfg, inst, q)
        return ask * (1 + slip) if side == "LONG" else bid * (1 - slip)

    def exit_price(self, cfg: dict, inst: Instrument, side: str, q: Quote) -> float:
        slip = self._slip(cfg, inst)
        bid, ask = self.broker_quote(cfg, inst, q)
        return bid * (1 - slip) if side == "LONG" else ask * (1 + slip)

    @staticmethod
    def fee(cfg: dict, inst: Instrument, notional: float) -> float:
        if inst.asset_class == "crypto":
            return notional * cfg["costs"]["btc_taker_fee_pct"] / 100
        return 0.0      # forex/gold cost is in the spread

    # --------------------------------------------------------------- positions
    def positions(self) -> list[dict]:
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

    def state(self, cfg: dict) -> AccountState:
        acc = self.db.one("SELECT * FROM account WHERE id=1")
        positions = self.positions()
        unreal = 0.0
        for p in positions:
            u = self.unrealized(cfg, p)
            unreal += u or 0.0
        margin = sum(p["margin"] for p in positions)
        equity = acc["cash"] + unreal
        return AccountState(acc["cash"], equity, max(acc["peak"], equity), margin, unreal,
                            acc["status"], acc["status_reason"], acc["starting_balance"])

    def save_equity(self, st: AccountState) -> None:
        self.db.execute("UPDATE account SET equity=?, peak=? WHERE id=1", (st.equity, st.peak))

    def _cash(self, delta: float) -> None:
        self.db.execute("UPDATE account SET cash = cash + ? WHERE id=1", (delta,))

    def open(self, cfg: dict, order: Order, signal: str, ai_reason: str | None,
             decision_id: int | None) -> int:
        inst = INSTRUMENTS[order.symbol]
        fee = self.fee(cfg, inst, order.notional)
        cur = self.db.execute(
            "INSERT INTO positions(symbol, side, units, leverage, entry, stop, target, margin, risk_usd, "
            "fees, signal, ai_reason, decision_id, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.symbol, order.side, order.units, order.leverage, order.entry, order.stop, order.target,
             order.margin, order.risk_usd, fee, signal, ai_reason, decision_id, iso()))
        if fee:
            self._cash(-fee)
        return int(cur.lastrowid)

    def close(self, cfg: dict, p: dict, price: float, reason: str, fraction: float = 1.0) -> dict:
        """Close all (fraction=1) or part of a position at `price`. Returns the trade row."""
        inst = INSTRUMENTS[p["symbol"]]
        units = p["units"] * fraction
        gross = pnl_usd(inst, p["side"], units, p["entry"], price)
        close_fee = self.fee(cfg, inst, notional_usd(inst, units, price))
        fees = p["fees"] * fraction + close_fee
        swap = p["swap"] * fraction
        funding = p["funding"] * fraction
        net = gross - fees + swap + funding
        risk = p["risk_usd"] * fraction
        trade = {
            "symbol": p["symbol"], "side": p["side"], "units": units, "leverage": p["leverage"],
            "entry": p["entry"], "exit": price, "stop": p["stop"], "target": p["target"],
            "pnl_usd": net, "pnl_r": net / risk if risk > 0 else None, "fees": fees, "swap": swap,
            "funding": funding, "signal": p["signal"], "ai_reason": p["ai_reason"],
            "exit_reason": reason, "opened_at": p["opened_at"], "closed_at": iso(),
        }
        cols = ",".join(trade)
        self.db.execute(f"INSERT INTO trades({cols}) VALUES ({','.join('?' * len(trade))})",
                        list(trade.values()))
        self._cash(gross - close_fee)
        if fraction >= 0.999:
            self.db.execute("DELETE FROM positions WHERE id=?", (p["id"],))
        else:
            keep = 1 - fraction
            self.db.execute(
                "UPDATE positions SET units=units*?, margin=margin*?, risk_usd=risk_usd*?, fees=fees*?, "
                "swap=swap*?, funding=funding*?, weekend_halved=1 WHERE id=?",
                (keep, keep, keep, keep, keep, keep, p["id"]))
        log.info("Closed %s %s %.4f @ %s (%s) net %.2f", p["side"], p["symbol"], units, price, reason, net)
        return trade

    def close_at_market(self, cfg: dict, p: dict, reason: str, fraction: float = 1.0) -> dict | None:
        q = self.prices.get(p["symbol"])
        if not q:
            return None
        inst = INSTRUMENTS[p["symbol"]]
        return self.close(cfg, p, self.exit_price(cfg, inst, p["side"], q), reason, fraction)

    def check_exits(self, cfg: dict, now: datetime | None = None) -> list[dict]:
        """Stops and targets against live bid/ask - only while the real market is open.
        (Binance/Kraken quote gold and FX at weekends; a real broker wouldn't fill then,
        so held positions take the Monday gap instead.)"""
        closed = []
        now = now or datetime.now(timezone.utc)
        for p in self.positions():
            q = self.prices.get(p["symbol"])
            if not q:
                continue
            inst = INSTRUMENTS[p["symbol"]]
            if not market_open(inst, now):
                continue
            px = self.exit_price(cfg, inst, p["side"], q)
            if p["side"] == "LONG":
                if px <= p["stop"]:
                    closed.append(self.close(cfg, p, px, "stop" if px >= p["stop"] * 0.9999 else "stop (gap)"))
                elif px >= p["target"]:
                    closed.append(self.close(cfg, p, p["target"], "target"))
            else:
                if px >= p["stop"]:
                    closed.append(self.close(cfg, p, px, "stop" if px <= p["stop"] * 1.0001 else "stop (gap)"))
                elif px <= p["target"]:
                    closed.append(self.close(cfg, p, p["target"], "target"))
        return closed

    def check_stop_out(self, cfg: dict) -> list[dict]:
        closed = []
        level = cfg["risk"]["stop_out_margin_level_pct"]
        for _ in range(5):
            st = self.state(cfg)
            ml = st.margin_level_pct
            if ml is None or ml >= level:
                break
            losers = [(self.unrealized(cfg, p) or 0, p) for p in self.positions()]
            losers.sort(key=lambda x: x[0])
            if not losers:
                break
            t = self.close_at_market(cfg, losers[0][1], f"stop-out (margin level {ml:.0f}%)")
            if not t:
                break
            closed.append(t)
        return closed

    def close_all(self, cfg: dict, reason: str, only: set[str] | None = None) -> list[dict]:
        out = []
        for p in self.positions():
            if only and p["asset_class"] not in only:
                continue
            t = self.close_at_market(cfg, p, reason)
            if t:
                out.append(t)
        return out

    # ---------------------------------------------------------- swap / funding
    def apply_swap(self, cfg: dict, now: datetime) -> float:
        """Run once per day at 17:00 NY. Wednesday is charged triple (weekend)."""
        mult = 3 if now.astimezone(NY).weekday() == 2 else 1
        total = 0.0
        for p in self.positions():
            inst = INSTRUMENTS[p["symbol"]]
            if inst.asset_class == "crypto":
                continue
            q = self.prices.get(p["symbol"])
            price = q.mid if q else p["entry"]
            rate = cfg["costs"].get(f"swap_{p['symbol']}_{p['side'].lower()}", 0.0)
            amount = notional_usd(inst, p["units"], price) * rate / 100 / 365 * mult
            self.db.execute("UPDATE positions SET swap = swap + ? WHERE id=?", (amount, p["id"]))
            self._cash(amount)
            total += amount
        return total

    def apply_funding(self, rate: float) -> float:
        """BTC perp funding: positive rate = longs pay shorts."""
        total = 0.0
        for p in self.positions():
            inst = INSTRUMENTS[p["symbol"]]
            if inst.asset_class != "crypto":
                continue
            q = self.prices.get(p["symbol"])
            mark = (q.mark or q.mid) if q else p["entry"]
            sign = 1 if p["side"] == "LONG" else -1
            amount = -sign * notional_usd(inst, p["units"], mark) * rate
            self.db.execute("UPDATE positions SET funding = funding + ? WHERE id=?", (amount, p["id"]))
            self._cash(amount)
            total += amount
        return total
