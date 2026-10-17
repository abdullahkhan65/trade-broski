"""Live quote cache + 15m candle storage shared by both feeds."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..db import DB

CANDLE_SECONDS = 15 * 60


@dataclass
class Quote:
    bid: float
    ask: float
    ts: datetime
    mark: float | None = None      # BTC mark price

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
    funding_rate: float | None = None           # BTC last funding rate (per 8h)
    next_funding_time: datetime | None = None
    status: dict[str, FeedStatus] = field(default_factory=dict)

    def update(self, symbol: str, bid: float, ask: float, ts: datetime | None = None,
               mark: float | None = None) -> None:
        q = self.quotes.get(symbol)
        now = ts or datetime.now(timezone.utc)
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


def store_candles(db: DB, symbol: str, candles: list[dict]) -> None:
    """candles: [{t, o, h, l, c, v}] - only completed candles should be passed."""
    db.executemany(
        "INSERT INTO prices(symbol, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, t) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, "
        "c=excluded.c, v=excluded.v",
        [(symbol, c["t"], c["o"], c["h"], c["l"], c["c"], c.get("v", 0)) for c in candles],
    )


def load_candles(db: DB, symbol: str, n: int = 2000) -> list[dict]:
    rows = db.all("SELECT t, o, h, l, c, v FROM prices WHERE symbol=? ORDER BY t DESC LIMIT ?", (symbol, n))
    rows.reverse()
    return rows


def prune_candles(db: DB, keep: int = 3000) -> None:
    for (sym,) in [(r["symbol"],) for r in db.all("SELECT DISTINCT symbol FROM prices")]:
        db.execute(
            "DELETE FROM prices WHERE symbol=? AND t < (SELECT t FROM prices WHERE symbol=? "
            "ORDER BY t DESC LIMIT 1 OFFSET ?)", (sym, sym, keep))
