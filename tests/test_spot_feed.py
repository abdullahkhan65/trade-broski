"""Gold: live quote from spot (Swissquote), perp candles shifted by the perp-spot gap; delayed Yahoo futures."""
import asyncio

import pytest

from app.feeds import binance as bn
from app.feeds.binance import BinanceFeed
from app.feeds.prices import PriceCache
from app.feeds.swissquote import SwissquoteFeed, spot_mid
from app.instruments import INSTRUMENTS


def test_gold_is_spot_quoted():
    assert INSTRUMENTS["XAU_USD"].spot_feed == "XAU/USD" and "XAU_USD" in bn.SPOT_QUOTED
    assert "BTC_USDT" not in bn.SPOT_QUOTED


def test_spot_mid_parses_swissquote():
    data = [{"spreadProfilePrices": [{"bid": 4142.579, "ask": 4143.241}, {"bid": 1, "ask": 1}]}]
    assert spot_mid(data) == pytest.approx(4142.91)


def test_perp_ticks_do_not_overwrite_spot_quote_but_cover_for_it(monkeypatch):
    cache = PriceCache()
    feed = BinanceFeed(cache, {"XAU_USD": "XAUUSDT", "BTC_USDT": "BTCUSDT"})
    sq = SwissquoteFeed(cache, {"XAU_USD": "XAU/USD"}, {"XAU_USD": "XAUUSDT"})
    feed._quote("XAU_USD", 4149.5, 4149.9)                 # perp tick first: no basis yet -> no quote
    assert cache.get("XAU_USD") is None
    monkeypatch.setattr("app.feeds.prices.market_open", lambda inst, now: True)
    cache.update("XAU_USD", 4142.7, 4142.7)
    sq._update_basis("XAU_USD", 4142.7)
    assert cache.basis["XAU_USD"] == pytest.approx(7.0)
    feed._quote("XAU_USD", 4152.5, 4152.9)                 # spot is fresh: perp only moves perp_mid
    assert cache.get("XAU_USD").bid == pytest.approx(4142.7)
    cache.quotes["XAU_USD"].ts = cache.quotes["XAU_USD"].ts.replace(year=2020)   # spot feed went quiet
    feed._quote("XAU_USD", 4152.5, 4152.9)
    assert cache.get("XAU_USD").bid == pytest.approx(4145.5)    # perp minus the 7.0 gap
    feed._quote("BTC_USDT", 80000, 80010)                  # BTC unaffected
    assert cache.get("BTC_USDT").ask == 80010


def test_gold_candles_shifted_to_spot(monkeypatch):
    cache = PriceCache()
    feed = BinanceFeed(cache, {"XAU_USD": "XAUUSDT"})
    async def klines(*a, **k):
        return [{"t": 0, "o": 4150.0, "h": 4155.0, "l": 4148.0, "c": 4152.0, "v": 1}]
    monkeypatch.setattr(feed, "_binance_klines", klines)
    with pytest.raises(RuntimeError):
        asyncio.run(feed.fetch_candles("XAU_USD", 1))          # gap unknown: refuse, don't store perp prices
    cache.basis["XAU_USD"] = 7.0
    c = asyncio.run(feed.fetch_candles("XAU_USD", 1))[0]
    assert (c["o"], c["h"], c["l"], c["c"]) == (4143.0, 4148.0, 4141.0, 4145.0)
