"""Free historical gold/forex candles from Dukascopy's public datafeed (for backtests).

One file per instrument per day: 1-minute BID candles, LZMA-compressed, 24-byte
big-endian records: (seconds from 00:00 UTC, open, close, low, high as int32
scaled by the instrument's point, volume as float32). Month in the URL is 0-based.
Weekend days are empty files. Minute candles are aggregated into 15m candles.
"""
from __future__ import annotations

import asyncio
import lzma
import struct
from datetime import date, datetime, timedelta, timezone

import httpx

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
RECORD = struct.Struct(">iiiiif")
BUCKET = 15 * 60


def parse_day(raw: bytes, day: date, point: float) -> list[dict]:
    """Decompressed or compressed day file -> list of 15m candles."""
    if not raw:
        return []
    try:
        raw = lzma.decompress(raw)
    except lzma.LZMAError:
        pass
    base = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    buckets: dict[int, dict] = {}
    for off in range(0, len(raw) - RECORD.size + 1, RECORD.size):
        secs, o, c, lo, hi, vol = RECORD.unpack_from(raw, off)
        if vol <= 0:            # Dukascopy writes flat zero-volume minutes when the market is shut
            continue
        t = base + secs - (base + secs) % BUCKET
        o, c, lo, hi = o / point, c / point, lo / point, hi / point
        b = buckets.get(t)
        if b is None:
            buckets[t] = {"t": t, "o": o, "h": hi, "l": lo, "c": c, "v": vol}
        else:
            b["h"] = max(b["h"], hi)
            b["l"] = min(b["l"], lo)
            b["c"] = c
            b["v"] += vol
    return [buckets[t] for t in sorted(buckets)]


async def download(symbol: str, point: float, days: int, concurrency: int = 12,
                   progress: bool = True) -> list[dict]:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days)
    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    all_days = [d for d in all_days if d.weekday() != 5]          # Saturdays are always empty
    sem = asyncio.Semaphore(concurrency)
    results: dict[date, list[dict]] = {}

    async def one(client: httpx.AsyncClient, d: date) -> None:
        url = URL.format(sym=symbol, y=d.year, m=d.month - 1, d=d.day)
        async with sem:
            for attempt in range(4):
                try:
                    r = await client.get(url)
                    if r.status_code == 404:
                        results[d] = []
                        return
                    r.raise_for_status()
                    results[d] = parse_day(r.content, d, point)
                    return
                except (httpx.HTTPError, lzma.LZMAError):
                    await asyncio.sleep(1 + attempt * 2)
            results[d] = []

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        tasks = [asyncio.create_task(one(client, d)) for d in all_days]
        for i, t in enumerate(asyncio.as_completed(tasks), 1):
            await t
            if progress and i % 25 == 0:
                print(f"  {symbol}: {i}/{len(tasks)} days", end="\r")
    if progress:
        print()
    return [c for d in sorted(results) for c in results[d]]
