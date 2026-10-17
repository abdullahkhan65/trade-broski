"""BTC and gold prices from Binance USD-M futures (public, no key).

* BTC_USDT <- BTCUSDT perpetual: best bid/ask (priced like XM's BTCUSD CFD).
* XAU_USD  <- XAUUSDT gold perpetual: best bid/ask. It tracks spot gold; the bot
  trades gold on XM market hours and charges XM-style swap.
* REST: 5m/15m klines.
* Fallback: if Binance is unreachable (some server regions get HTTP 451), the
  same data is polled from Bybit's public API every 2 seconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone

import certifi
import httpx
import websockets

from .prices import PriceCache, tf_seconds

log = logging.getLogger("feeds.binance")

REST = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
FUNDING_SYMBOL = "BTC_USDT"     # only BTC is treated as a perp (funding); gold is simulated as a CFD


def _ms(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


class BinanceFeed:
    def __init__(self, cache: PriceCache, symbols: dict[str, str]):
        """symbols: internal symbol -> Binance symbol, e.g. {"BTC_USDT": "BTCUSDT"}"""
        self.cache = cache
        self.symbols = symbols
        self.by_feed = {v: k for k, v in symbols.items()}
        self.status = cache.feed("binance")
        self.use_bybit_candles = False

    @property
    def ws_url(self) -> str:
        streams = [f"{s.lower()}@bookTicker" for s in self.symbols.values()]
        funding = self.symbols.get(FUNDING_SYMBOL)
        if funding:
            streams.append(f"{funding.lower()}@markPrice@1s")
        return "wss://fstream.binance.com/stream?streams=" + "/".join(streams)

    async def run_stream(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, ssl=SSL_CTX) as ws:
                    self.status.connected = True
                    self.status.last_error = None
                    backoff = 1
                    log.info("Binance stream connected (%s)", ", ".join(self.symbols.values()))
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.connected = False
                self.status.last_error = f"{type(e).__name__}: {e}"
                self.status.reconnects += 1
                log.warning("Binance stream error: %s - polling Bybit for %ss", e, backoff)
                await self._poll_bybit_for(backoff)
                backoff = min(backoff * 2, 60)

    def _handle(self, msg: dict) -> None:
        self.status.last_message = datetime.now(timezone.utc)
        d = msg.get("data", msg)
        sym = self.by_feed.get(d.get("s", ""))
        if not sym:
            return
        ev = d.get("e")
        if ev == "bookTicker":
            q = self.cache.get(sym)
            self.cache.update(sym, float(d["b"]), float(d["a"]), mark=q.mark if q else None)
        elif ev == "markPriceUpdate":
            mark = float(d["p"])
            q = self.cache.get(sym)
            if q:
                q.mark = mark
            else:
                self.cache.update(sym, mark, mark, mark=mark)
            if sym == FUNDING_SYMBOL:
                if d.get("r") not in (None, ""):
                    self.cache.funding_rate = float(d["r"])
                if d.get("T"):
                    self.cache.next_funding_time = _ms(d["T"])

    async def _poll_bybit_for(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + max(seconds, 2)
        async with httpx.AsyncClient(timeout=10) as client:
            while loop.time() < end:
                for sym, feed_sym in self.symbols.items():
                    try:
                        r = await client.get(f"{BYBIT}/v5/market/tickers",
                                             params={"category": "linear", "symbol": feed_sym})
                        t = r.json()["result"]["list"][0]
                        self.cache.update(sym, float(t["bid1Price"]), float(t["ask1Price"]),
                                          mark=float(t["markPrice"]))
                        if sym == FUNDING_SYMBOL:
                            self.cache.funding_rate = float(t["fundingRate"])
                            self.cache.next_funding_time = _ms(t["nextFundingTime"])
                        self.cache.feed("bybit").last_message = datetime.now(timezone.utc)
                    except Exception as e:  # noqa: BLE001
                        self.cache.feed("bybit").last_error = str(e)
                await asyncio.sleep(2)

    async def fetch_candles(self, symbol: str, count: int = 1500, tf: str = "15m",
                            start: datetime | None = None) -> list[dict]:
        """Completed candles, oldest first. Falls back to Bybit if Binance fails."""
        feed_sym = self.symbols[symbol]
        if not self.use_bybit_candles:
            try:
                return await self._binance_klines(feed_sym, count, start, tf)
            except Exception as e:  # noqa: BLE001
                log.warning("Binance klines failed (%s) - switching to Bybit", e)
                self.use_bybit_candles = True
        return await self._bybit_klines(feed_sym, count, tf)

    async def _binance_klines(self, feed_sym: str, count: int, start: datetime | None,
                              tf: str = "15m") -> list[dict]:
        params: dict = {"symbol": feed_sym, "interval": tf, "limit": min(count, 1500)}
        if start:
            params["startTime"] = int(start.timestamp() * 1000)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{REST}/fapi/v1/klines", params=params)
            r.raise_for_status()
            rows = r.json()
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5])} for k in rows if int(k[6]) < now_ms]

    async def _bybit_klines(self, feed_sym: str, count: int, tf: str = "15m") -> list[dict]:
        params = {"category": "linear", "symbol": feed_sym, "interval": str(tf_seconds(tf) // 60),
                  "limit": min(count, 1000)}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{BYBIT}/v5/market/kline", params=params)
            r.raise_for_status()
            rows = r.json()["result"]["list"]
        now = datetime.now(timezone.utc).timestamp()
        out = [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": float(k[5])} for k in rows
               if int(k[0]) // 1000 + tf_seconds(tf) <= now]
        out.sort(key=lambda c: c["t"])
        return out

    async def fetch_funding(self) -> tuple[float, datetime] | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{REST}/fapi/v1/premiumIndex",
                                     params={"symbol": self.symbols[FUNDING_SYMBOL]})
                r.raise_for_status()
                d = r.json()
                return float(d["lastFundingRate"]), _ms(d["nextFundingTime"])
        except Exception:  # noqa: BLE001
            if self.cache.funding_rate is not None and self.cache.next_funding_time:
                return self.cache.funding_rate, self.cache.next_funding_time
            return None
