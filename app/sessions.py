"""Market hours and trading sessions.

Daylight saving is handled by using real time zones:
* Forex/gold week: Sunday 17:00 New York -> Friday 17:00 New York.
  (About 2-3am PKT Monday -> 2-3am PKT Saturday.)
* Daily rollover (swap is charged): 17:00 New York.
* Asian range: 00:00-07:00 UTC (Tokyo session, used for breakouts).
* London session: 08:00-17:00 London time. New York: 08:00-17:00 NY time.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .instruments import FOREX_LIKE, Instrument

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

ASIAN_START_UTC = time(0, 0)
ASIAN_END_UTC = time(7, 0)


def forex_open(now: datetime) -> bool:
    ny = now.astimezone(NY)
    wd, t = ny.weekday(), ny.time()          # Mon=0 ... Sun=6
    if wd == 5:                               # Saturday
        return False
    if wd == 4 and t >= time(17, 0):          # Friday after 5pm NY
        return False
    if wd == 6 and t < time(17, 0):           # Sunday before 5pm NY
        return False
    # retail brokers halt gold ~5pm-6pm NY daily; treat that hour as closed for gold below
    return True


def market_open(inst: Instrument, now: datetime) -> bool:
    if inst.asset_class == "crypto":
        return True
    if not forex_open(now):
        return False
    if inst.asset_class == "metal":
        t = now.astimezone(NY).time()
        if time(17, 0) <= t < time(18, 0):    # gold daily maintenance break
            return False
    return True


def london_session(now: datetime) -> bool:
    t = now.astimezone(LONDON).time()
    return time(8, 0) <= t < time(17, 0)


def ny_session(now: datetime) -> bool:
    t = now.astimezone(NY).time()
    return time(8, 0) <= t < time(17, 0)


def in_trading_session(inst: Instrument, now: datetime, session_filter: bool) -> bool:
    if inst.asset_class == "crypto" or not session_filter:
        return True
    return london_session(now) or ny_session(now)


def session_name(now: datetime) -> str:
    names = []
    t = now.astimezone(UTC).time()
    if ASIAN_START_UTC <= t < ASIAN_END_UTC:
        names.append("Asia")
    if london_session(now):
        names.append("London")
    if ny_session(now):
        names.append("New York")
    return "+".join(names) or "off-hours"


def asian_range_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Asian range for the current UTC day."""
    d = now.astimezone(UTC).date()
    return (datetime.combine(d, ASIAN_START_UTC, UTC), datetime.combine(d, ASIAN_END_UTC, UTC))


def minutes_to_weekly_close(now: datetime) -> float | None:
    """Minutes until Friday 17:00 NY, or None if that's more than 24h away."""
    ny = now.astimezone(NY)
    if ny.weekday() != 4:
        return None
    close = ny.replace(hour=17, minute=0, second=0, microsecond=0)
    mins = (close - ny).total_seconds() / 60
    return mins if mins >= 0 else None


def is_forex_like(inst: Instrument) -> bool:
    return inst.asset_class in FOREX_LIKE


def last_rollover(now: datetime) -> datetime:
    ny = now.astimezone(NY)
    roll = ny.replace(hour=17, minute=0, second=0, microsecond=0)
    if ny < roll:
        roll -= timedelta(days=1)
    return roll.astimezone(UTC)
