"""Spot gold from Swissquote's public quote feed (no key), polled every 2 seconds.

XM's GOLD is a spot CFD. Binance's XAUUSDT perpetual (our candle source) trades a few dollars
ABOVE spot (measured 18 Oct 2026: perp 4149.7, Swissquote 4142.6, XM 4142), so levels, stops and
targets built on it were off XM's chart. So for instruments with `spot_feed`:
* the live quote is Swissquote's spot mid (the XM spread from settings is applied around it),
* the perp-spot gap ("basis") is tracked continuously (smoothed) and the perp candles are shifted
  by it (feeds/binance.py), so the chart, PDH/PDL, order blocks and stops sit on spot prices,
* if Swissquote stops answering, the quote falls back to perp minus the last known gap.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .prices import PriceCache

log = logging.getLogger("feeds.swissquote")

URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{pair}"
BINANCE_TICKER = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
UA = {"User-Agent": "Mozilla/5.0"}
POLL_SECONDS = 2
BASIS_ALPHA = 0.05            # smoothing of the perp-spot gap (it drifts slowly with funding)


def spot_mid(data: list) -> float:
    """Mid of the first spread profile of the first server in Swissquote's answer."""
    p = data[0]["spreadProfilePrices"][0]
    return (float(p["bid"]) + float(p["ask"])) / 2


class SwissquoteFeed:
    def __init__(self, cache: PriceCache, symbols: dict[str, str], perp_symbols: dict[str, str]):
        """symbols: internal symbol -> Swissquote pair ("XAU/USD"); perp_symbols: -> Binance perp."""
        self.cache = cache
        self.symbols = symbols
        self.perp_symbols = perp_symbols
        self.status = cache.feed("swissquote")

    def _update_basis(self, sym: str, spot: float) -> None:
        perp = self.cache.perp_mid.get(sym)
        if perp is None:
            return
        gap = perp - spot
        old = self.cache.basis.get(sym)
        self.cache.basis[sym] = gap if old is None else old + BASIS_ALPHA * (gap - old)

    async def prime_basis(self, sym: str) -> float:
        """One-off measurement of the perp-spot gap (startup, before history is loaded)."""
        async with httpx.AsyncClient(timeout=10, headers=UA) as client:
            r = await client.get(URL.format(pair=self.symbols[sym]))
            r.raise_for_status()
            spot = spot_mid(r.json())
            t = (await client.get(BINANCE_TICKER, params={"symbol": self.perp_symbols[sym]})).json()
            perp = (float(t["bidPrice"]) + float(t["askPrice"])) / 2
        self.cache.perp_mid.setdefault(sym, perp)
        self.cache.basis[sym] = perp - spot
        log.info("%s perp-spot gap %.2f (perp %.2f, spot %.2f)", sym, perp - spot, perp, spot)
        return perp - spot

    async def run_stream(self) -> None:
        if not self.symbols:
            return
        async with httpx.AsyncClient(timeout=10, headers=UA) as client:
            while True:
                for sym, pair in self.symbols.items():
                    try:
                        r = await client.get(URL.format(pair=pair))
                        r.raise_for_status()
                        mid = spot_mid(r.json())
                        self.cache.update(sym, mid, mid)
                        self._update_basis(sym, mid)
                        self.status.connected = True
                        self.status.last_message = datetime.now(timezone.utc)
                        self.status.last_error = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.status.connected = False
                        self.status.last_error = f"{type(e).__name__}: {e}"
                        log.debug("Swissquote %s failed: %s", pair, e)
                await asyncio.sleep(POLL_SECONDS)
