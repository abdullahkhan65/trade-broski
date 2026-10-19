"""End-to-end through the engine's choose-and-handle path (19 Oct 2026: adaptive.score crashed here
on every setup that passed the filters, so no account could open a trade)."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.db import DB, iso
from app.engine import Engine
from app.feeds.prices import PriceCache
from app.instruments import INSTRUMENTS
from app.settings_store import MODES, SettingsStore
from app.signals import Signal
from app import adaptive

TUE = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


class TG:
    async def send(self, *a, **k):
        pass


def engine():
    db = DB(":memory:")
    db.ensure_accounts(MODES, 250)
    prices = PriceCache()
    eng = Engine(db, SettingsStore(db), prices, {}, None, TG())
    prices.update("XAU_USD", 4142.0, 4142.0)
    return eng


def gold_signal():
    return Signal("XAU_USD", "LONG", "session_breakout", 4142.0, 4.0, int(TUE.timestamp()), "Asian range break",
                  regime="trend")


def test_score_works_with_pooled_stats():
    eng = engine()
    t = iso(datetime.now(timezone.utc) - timedelta(days=2))
    for acc in ("standard", "lab"):
        eng.db.execute("INSERT INTO trades(account, symbol, side, lots, units, leverage, entry, exit, pnl_usd, pnl_r, "
                       "signal, regime, exit_reason, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (acc, "XAU_USD", "LONG", 0.01, 1, 1, 1, 1, 2, 1.0, "session_breakout", "trend", "tp", t, t))
    cfg = eng.cfg("standard")
    assert adaptive.score(eng.db, "standard", "session_breakout", "trend", cfg) > 0
    assert adaptive.score(eng.db, "standard", "session_breakout", "trend") > 0


def test_lab_signal_reaches_the_broker():
    eng = engine()
    asyncio.run(eng._choose_and_handle("lab", INSTRUMENTS["XAU_USD"], [gold_signal()], [], TUE))
    assert len(eng.broker.positions("lab")) == 1
    assert eng.db.scalar("SELECT accepted FROM decisions WHERE account='lab'") == 1
