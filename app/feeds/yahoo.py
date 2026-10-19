"""USD/JPY from Yahoo Finance (public, no key). Off by default.

Kraken's USD/JPY market is too thin to use, and OANDA isn't available in
Pakistan, so USD/JPY falls back to Yahoo's chart API:
* polled every 15 seconds (it's not a stream, so prices lag a little),
* mid price only - the XM spread from settings is applied around it,
* unofficial: Yahoo can rate-limit or change it without notice.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .prices import PriceCache, tf_seconds

log = logging.getLogger("feeds.yahoo")

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = {"User-Agent": "Mozilla/5.0"}
POLL_SECONDS = 15
MAX_QUOTE_AGE = 1500      # seconds; futures quotes are ~600s old, Friday's close on weekends is hours old


class YahooFeed:
    def __init__(self, cache: PriceCache, symbols: dict[str, str]):
        self.cache = cache
        self.symbols = symbols
        self.status = cache.feed("yahoo")

    async def run_stream(self) -> None:
        if not self.symbols:
            return
        async with httpx.AsyncClient(timeout=15, headers=UA) as client:
            while True:
                for sym, ysym in self.symbols.items():
                    try:
                        r = await client.get(CHART.format(symbol=ysym), params={"interval": "1m", "range": "1d"})
                        r.raise_for_status()
                        meta = r.json()["chart"]["result"][0]["meta"]
                        px = float(meta["regularMarketPrice"])
                        ts = datetime.fromtimestamp(int(meta["regularMarketTime"]), tz=timezone.utc)
                        # only count it as live if Yahoo's own quote time is recent (on weekends it keeps
                        # returning Friday's close). CME/CBOT futures (US30/US100 via YM=F/NQ=F) are delayed
                        # ~10 minutes on Yahoo - so are their candles, so signal and fill use the same moment.
                        if (datetime.now(timezone.utc) - ts).total_seconds() < MAX_QUOTE_AGE or not self.cache.get(sym):
                            self.cache.update(sym, px, px)
                        self.status.connected = True
                        self.status.last_message = datetime.now(timezone.utc)
                        self.status.last_error = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.status.connected = False
                        self.status.last_error = f"{type(e).__name__}: {e}"
                        log.debug("Yahoo %s failed: %s", ysym, e)
                await asyncio.sleep(POLL_SECONDS)

    async def fetch_candles(self, symbol: str, count: int = 2000, tf: str = "15m") -> list[dict]:
        rng = "60d" if count > 400 else "5d"     # Yahoo keeps 60 days of intraday data
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.get(CHART.format(symbol=self.symbols[symbol]),
                                 params={"interval": tf, "range": rng})
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        now = datetime.now(timezone.utc).timestamp()
        out = []
        for i, t in enumerate(res.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c) or t + tf_seconds(tf) > now:
                continue
            out.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                        "v": float(q["volume"][i] or 0)})
        return out[-count:]
