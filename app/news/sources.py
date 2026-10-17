"""Free news sources: economic calendar, RSS headlines, crypto fear & greed.

* Economic calendar - ForexFactory's public weekly JSON (impact, forecast, previous).
  Refreshed every hour; it's rate-limited, so don't poll it faster.
* Headlines - any RSS/Atom feeds listed on the settings page. Each headline is
  tagged with the instruments it relates to by keyword (see instruments.py).
* Fear & Greed - alternative.me crypto sentiment index (0-100), daily.
"""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..db import DB, iso, parse_iso
from ..instruments import INSTRUMENTS

log = logging.getLogger("news")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FNG_URL = "https://api.alternative.me/fng/?limit=1"
UA = {"User-Agent": "Mozilla/5.0 (paper-trader news reader)"}


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def tag_symbols(text: str) -> list[str]:
    t = f" {text.lower()} "
    return [s for s, inst in INSTRUMENTS.items() if any(k in t for k in inst.keywords)]


# ---------------------------------------------------------------- calendar
async def refresh_calendar(db: DB) -> int:
    async with httpx.AsyncClient(timeout=20, headers=UA) as c:
        r = await c.get(CALENDAR_URL)
        r.raise_for_status()
        events = r.json()
    rows = []
    for e in events:
        try:
            at = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        cur = (e.get("country") or "").upper()
        rows.append((_id(e.get("title", ""), cur, e["date"]), iso(at), cur, e.get("impact", ""),
                     e.get("title", ""), e.get("forecast", ""), e.get("previous", "")))
    db.executemany(
        "INSERT INTO calendar_events(id, at, currency, impact, title, forecast, previous) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET impact=excluded.impact, forecast=excluded.forecast, previous=excluded.previous",
        rows)
    return len(rows)


def events_between(db: DB, start: datetime, end: datetime, currencies: tuple[str, ...],
                   impacts: tuple[str, ...] = ("High",)) -> list[dict]:
    if not currencies:
        return []
    qc = ",".join("?" * len(currencies))
    qi = ",".join("?" * len(impacts))
    return db.all(f"SELECT * FROM calendar_events WHERE at >= ? AND at <= ? AND currency IN ({qc}) "
                  f"AND impact IN ({qi}) ORDER BY at", (iso(start), iso(end), *currencies, *impacts))


def blackout(db: DB, cfg: dict, currencies: tuple[str, ...], now: datetime) -> dict | None:
    """The event that blocks new trades right now, if any."""
    n = cfg["news"]
    if not n["enabled"] or n["blackout_minutes"] <= 0:
        return None
    w = timedelta(minutes=n["blackout_minutes"])
    impacts = ("High", "Medium") if n["blackout_medium_impact"] else ("High",)
    ev = events_between(db, now - w, now + w, currencies, impacts)
    return ev[0] if ev else None


# --------------------------------------------------------------- headlines
def _parse_feed(xml_text: str, source: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in items[:50]:
        title = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            el = it.find("a:link", ns)
            link = el.get("href", "") if el is not None else ""
        desc = (it.findtext("description") or it.findtext("a:summary", namespaces=ns) or "")[:300]
        raw_date = it.findtext("pubDate") or it.findtext("a:updated", namespaces=ns) or it.findtext("a:published", namespaces=ns)
        try:
            at = parsedate_to_datetime(raw_date) if raw_date and "," in raw_date else datetime.fromisoformat(
                (raw_date or "").replace("Z", "+00:00"))
            at = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            at = datetime.now(timezone.utc)
        if title:
            out.append({"title": title, "link": link, "at": at, "source": source,
                        "symbols": tag_symbols(f"{title} {desc}")})
    return out


async def refresh_headlines(db: DB, feeds: list[str]) -> int:
    new = 0
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as c:
        for url in feeds:
            try:
                r = await c.get(url)
                r.raise_for_status()
                items = _parse_feed(r.text, httpx.URL(url).host)
            except Exception as e:  # noqa: BLE001 - one bad feed shouldn't stop the rest
                log.warning("feed %s failed: %s", url, e)
                continue
            for it in items:
                cur = db.execute(
                    "INSERT OR IGNORE INTO news_items(id, at, source, title, link, symbols) VALUES (?,?,?,?,?,?)",
                    (_id(it["link"] or it["title"]), iso(it["at"]), it["source"], it["title"][:400],
                     it["link"], ",".join(it["symbols"])))
                new += cur.rowcount
    db.execute("DELETE FROM news_items WHERE at < ?", (iso(datetime.now(timezone.utc) - timedelta(days=7)),))
    return new


def recent_headlines(db: DB, symbol: str | None, hours: int = 6, limit: int = 20) -> list[dict]:
    since = iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    if symbol:
        return db.all("SELECT * FROM news_items WHERE at >= ? AND (',' || symbols || ',') LIKE ? "
                      "ORDER BY at DESC LIMIT ?", (since, f"%,{symbol},%", limit))
    return db.all("SELECT * FROM news_items WHERE at >= ? ORDER BY at DESC LIMIT ?", (since, limit))


# ------------------------------------------------------------ fear & greed
async def fear_greed() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(FNG_URL)
            d = r.json()["data"][0]
            return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:  # noqa: BLE001
        return None


def is_fresh(at: str, max_age: timedelta) -> bool:
    return datetime.now(timezone.utc) - parse_iso(at) <= max_age
