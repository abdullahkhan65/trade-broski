"""Live price feeds. Every feed has `run_stream()` (runs forever, fills the
PriceCache) and `fetch_candles(symbol, count, tf)` (completed candles, oldest first)."""
from __future__ import annotations

from ..instruments import INSTRUMENTS, Instrument
from .binance import BinanceFeed
from .kraken import KrakenFeed
from .prices import PriceCache
from .swissquote import SwissquoteFeed
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
        # live spot quote for instruments whose candles come from a perp (gold)
        "swissquote": SwissquoteFeed(cache, {s: i.spot_feed for s, i in INSTRUMENTS.items() if i.spot_feed},
                                     {s: i.feed_symbol for s, i in INSTRUMENTS.items() if i.spot_feed}),
    }


async def fetch_candles(feeds: dict, inst: Instrument, count: int | None = None, tf: str = "15m") -> list[dict]:
    if inst.spot_feed and inst.symbol not in feeds[inst.source].cache.basis:
        await feeds["swissquote"].prime_basis(inst.symbol)      # candles are shifted to spot by this gap
    return await feeds[inst.source].fetch_candles(inst.symbol, count or HISTORY_COUNT[inst.source], tf=tf)
