"""EUR/USD and GBP/USD from Kraken (public, no key, works from Pakistan).

* WebSocket v2 `ticker` channel: best bid/ask on every change.
* REST OHLC: 15m candles (Kraken returns the last 720 = 7.5 days; the bot keeps
  storing them, so its own history grows over time).

Kraken's fiat pairs trade 24/7 and track the interbank rate closely. The bot
only trades them during real forex hours and widens the spread to typical
broker levels (settings -> Costs), so results aren't flattered.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import websockets

from .binance import SSL_CTX
from .prices import CANDLE_SECONDS, PriceCache

log = logging.getLogger("feeds.kraken")

WS_URL = "wss://ws.kraken.com/v2"
REST = "https://api.kraken.com/0/public"


class KrakenFeed:
    def __init__(self, cache: PriceCache, symbols: dict[str, str]):
        """symbols: internal symbol -> Kraken symbol, e.g. {"EUR_USD": "EUR/USD"}"""
        self.cache = cache
        self.symbols = symbols
        self.by_feed = {v: k for k, v in symbols.items()}
        self.status = cache.feed("kraken")

    async def run_stream(self) -> None:
        backoff = 1
        sub = {"method": "subscribe",
               "params": {"channel": "ticker", "symbol": list(self.symbols.values())}}
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, ssl=SSL_CTX) as ws:
                    await ws.send(json.dumps(sub))
                    self.status.connected = True
                    self.status.last_error = None
                    backoff = 1
                    log.info("Kraken stream connected (%s)", ", ".join(self.symbols.values()))
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.connected = False
                self.status.last_error = f"{type(e).__name__}: {e}"
                self.status.reconnects += 1
                log.warning("Kraken stream error: %s (retry in %ss)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle(self, msg: dict) -> None:
        self.status.last_message = datetime.now(timezone.utc)   # heartbeats count as alive
        if msg.get("channel") != "ticker":
            return
        for d in msg.get("data") or []:
            sym = self.by_feed.get(d.get("symbol", ""))
            if sym and d.get("bid") and d.get("ask"):
                self.cache.update(sym, float(d["bid"]), float(d["ask"]))

    async def fetch_candles(self, symbol: str, count: int = 720) -> list[dict]:
        pair = self.symbols[symbol].replace("/", "")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{REST}/OHLC", params={"pair": pair, "interval": 15})
            r.raise_for_status()
            data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken: {data['error']}")
        res = data["result"]
        rows = res[next(k for k in res if k != "last")]
        now = datetime.now(timezone.utc).timestamp()
        out = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                "v": float(k[6])} for k in rows if int(k[0]) + CANDLE_SECONDS <= now]
        return out[-count:]
