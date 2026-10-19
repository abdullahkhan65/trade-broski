"""Live quote cache + candle storage shared by all feeds.

Gold and forex quotes FREEZE while the real market is closed (weekend, gold's
daily break): Binance/Kraken keep trading 24/7, but XM doesn't, so the bot and
the dashboard show the last price before the close, like MT5 does. Candles
from closed hours are dropped for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..db import DB
from ..instruments import INSTRUMENTS
from ..sessions import market_open

TIMEFRAMES = {"5m": 5 * 60, "15m": 15 * 60}
CANDLE_SECONDS = TIMEFRAMES["15m"]


def tf_seconds(tf: str) -> int:
    return TIMEFRAMES[tf]


@dataclass
class Quote:
    bid: float
    ask: float
    ts: datetime
    mark: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class FeedStatus:
    name: str
    connected: bool = False
    last_message: datetime | None = None
    last_error: str | None = None
    reconnects: int = 0


@dataclass
class PriceCache:
    quotes: dict[str, Quote] = field(default_factory=dict)
    funding_rate: float | None = None
    next_funding_time: datetime | None = None
    status: dict[str, FeedStatus] = field(default_factory=dict)
    # instruments quoted from a spot feed while their candles come from a perp (gold): perp mid - spot mid
    basis: dict[str, float] = field(default_factory=dict)
    perp_mid: dict[str, float] = field(default_factory=dict)

    def update(self, symbol: str, bid: float, ask: float, ts: datetime | None = None,
               mark: float | None = None) -> None:
        now = ts or datetime.now(timezone.utc)
        q = self.quotes.get(symbol)
        inst = INSTRUMENTS.get(symbol)
        if q and inst and not market_open(inst, now):
            return                      # frozen at the last pre-close price
        if q:
            q.bid, q.ask, q.ts = bid, ask, now
            if mark is not None:
                q.mark = mark
        else:
            self.quotes[symbol] = Quote(bid, ask, now, mark)

    def get(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def age_seconds(self, symbol: str) -> float | None:
        q = self.quotes.get(symbol)
        if not q:
            return None
        return (datetime.now(timezone.utc) - q.ts).total_seconds()

    def feed(self, name: str) -> FeedStatus:
        return self.status.setdefault(name, FeedStatus(name))


def in_market_hours(symbol: str, candle_t: int, tf: str) -> bool:
    inst = INSTRUMENTS.get(symbol)
    if not inst:
        return True
    start = datetime.fromtimestamp(candle_t, tz=timezone.utc)
    end = datetime.fromtimestamp(candle_t + tf_seconds(tf) - 1, tz=timezone.utc)
    return market_open(inst, start) or market_open(inst, end)


def store_candles(db: DB, symbol: str, candles: list[dict], tf: str = "15m") -> None:
    """candles: [{t, o, h, l, c, v}] - only completed candles should be passed."""
    rows = [(symbol, tf, c["t"], c["o"], c["h"], c["l"], c["c"], c.get("v", 0))
            for c in candles if in_market_hours(symbol, c["t"], tf)]
    db.executemany(
        "INSERT INTO prices(symbol, tf, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, tf, t) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, "
        "c=excluded.c, v=excluded.v", rows)


def load_candles(db: DB, symbol: str, n: int = 2000, tf: str = "15m") -> list[dict]:
    rows = db.all("SELECT t, o, h, l, c, v FROM prices WHERE symbol=? AND tf=? ORDER BY t DESC LIMIT ?",
                  (symbol, tf, n))
    rows.reverse()
    return rows


def prune_candles(db: DB, keep: int = 6000) -> None:
    for r in db.all("SELECT DISTINCT symbol, tf FROM prices"):
        db.execute(
            "DELETE FROM prices WHERE symbol=? AND tf=? AND t < (SELECT t FROM prices WHERE symbol=? AND tf=? "
            "ORDER BY t DESC LIMIT 1 OFFSET ?)", (r["symbol"], r["tf"], r["symbol"], r["tf"], keep))
