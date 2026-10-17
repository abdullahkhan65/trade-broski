"""Live price feeds. Every feed has `run_stream()` (runs forever, fills the
PriceCache) and `fetch_candles(symbol, count)` (completed 15m candles, oldest first)."""
from __future__ import annotations

from ..instruments import INSTRUMENTS, Instrument
from .binance import BinanceFeed
from .kraken import KrakenFeed
from .prices import PriceCache
from .yahoo import YahooFeed

# how many candles to load at startup from each source (its maximum per request)
HISTORY_COUNT = {"binance": 1500, "kraken": 720, "yahoo": 2000}


def build_feeds(cache: PriceCache) -> dict:
    def syms(source: str) -> dict[str, str]:
        return {s: i.feed_symbol for s, i in INSTRUMENTS.items() if i.source == source}
    return {
        "binance": BinanceFeed(cache, syms("binance")),
        "kraken": KrakenFeed(cache, syms("kraken")),
        "yahoo": YahooFeed(cache, syms("yahoo")),
    }


async def fetch_candles(feeds: dict, inst: Instrument, count: int | None = None) -> list[dict]:
    return await feeds[inst.source].fetch_candles(inst.symbol, count or HISTORY_COUNT[inst.source])
