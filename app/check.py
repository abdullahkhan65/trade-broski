"""Pre-flight check: `python -m app.check`

Confirms each external service works from THIS machine (run it on the server):
Binance (BTC + gold, or the Bybit fallback), Kraken (EUR/USD, GBP/USD), Yahoo
(USD/JPY), Dukascopy (backtest history), Anthropic key + model, Telegram,
economic calendar and each RSS feed. Costs nothing - no AI tokens are used.
"""
from __future__ import annotations

import asyncio
from datetime import date

import anthropic
import httpx

from .config import ENV
from .feeds import build_feeds
from .feeds.dukascopy import URL as DUKA_URL
from .feeds.dukascopy import parse_day
from .feeds.prices import PriceCache
from .news.sources import CALENDAR_URL, UA
from .settings_store import DEFAULT_FEEDS


async def _try(name: str, coro) -> bool:
    try:
        msg = await coro
        print(f"  ✅ {name}: {msg}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False


async def binance() -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        out = []
        for sym in ("BTCUSDT", "XAUUSDT"):
            r = await c.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": sym})
            if r.status_code == 451:
                raise RuntimeError("HTTP 451 - Binance blocks this server's region. Bybit fallback will be used.")
            r.raise_for_status()
            out.append(f"{sym} {float(r.json()['markPrice']):.2f}")
        return ", ".join(out)


async def bybit() -> str:
    feed = build_feeds(PriceCache())["binance"]
    btc = await feed._bybit_klines("BTCUSDT", 3)
    xau = await feed._bybit_klines("XAUUSDT", 3)
    return f"fallback OK, BTC {btc[-1]['c'] if btc else 'n/a'}, gold {xau[-1]['c'] if xau else 'n/a'}"


async def kraken() -> str:
    feed = build_feeds(PriceCache())["kraken"]
    eur = await feed.fetch_candles("EUR_USD", 3)
    gbp = await feed.fetch_candles("GBP_USD", 3)
    return f"EUR/USD {eur[-1]['c']}, GBP/USD {gbp[-1]['c']} (last 15m close)"


async def yahoo() -> str:
    c = await build_feeds(PriceCache())["yahoo"].fetch_candles("USD_JPY", 3)
    return f"USD/JPY {c[-1]['c']:.3f} (last 15m close)"


async def dukascopy() -> str:
    d = date(2025, 3, 12)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = await c.get(DUKA_URL.format(sym="EURUSD", y=d.year, m=d.month - 1, d=d.day))
        r.raise_for_status()
    return f"history OK ({len(parse_day(r.content, d, 1e5))} x 15m candles for a sample day)"


async def claude() -> str:
    if not ENV.has_ai:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.AsyncAnthropic(api_key=ENV.anthropic_api_key)
    m = await client.models.retrieve(ENV.ai_model)
    m2 = await client.models.retrieve(ENV.ai_strong_model)
    return f"key works, models {m.id} + {m2.id} available"


async def telegram() -> str:
    if not ENV.has_telegram:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://api.telegram.org/bot{ENV.telegram_bot_token}/sendMessage",
                         json={"chat_id": ENV.telegram_chat_id, "text": "✅ Paper trader check: Telegram works"})
        r.raise_for_status()
    return "test message sent"


async def calendar() -> str:
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        r = await c.get(CALENDAR_URL)
        r.raise_for_status()
        ev = r.json()
    return f"{len(ev)} events this week, {sum(1 for e in ev if e.get('impact') == 'High')} high impact"


async def feed(url: str) -> str:
    async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
    return f"{len(r.text) // 1024} KB"


async def main() -> None:
    print("Pre-flight check\n")
    print("Prices")
    if not await _try("Binance futures (BTC + gold)", binance()):
        await _try("Bybit (fallback)", bybit())
    await _try("Kraken (EUR/USD, GBP/USD)", kraken())
    await _try("Yahoo (USD/JPY, off by default)", yahoo())
    await _try("Dukascopy (backtest history)", dukascopy())
    print("AI")
    await _try("Anthropic", claude())
    print("Alerts")
    await _try("Telegram", telegram())
    print("News")
    await _try("Economic calendar", calendar())
    for url in DEFAULT_FEEDS:
        await _try(url, feed(url))
    print(f"\nPAPER_ONLY={ENV.paper_only}  DB={ENV.db_path}  dashboard password set: {bool(ENV.dashboard_password)}")


if __name__ == "__main__":
    asyncio.run(main())
