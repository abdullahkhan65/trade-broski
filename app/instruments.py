"""The five instruments the bot trades, and the maths to turn prices into USD."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str            # internal id
    display: str
    source: str            # live price source: "binance" | "kraken" | "yahoo"
    feed_symbol: str       # symbol on that source
    asset_class: str       # "forex" | "metal" | "crypto"
    base: str
    quote: str
    decimals: int
    currencies: tuple[str, ...]   # calendar currencies that move this instrument
    keywords: tuple[str, ...]     # headline keywords (lower-case) that relate to it
    history_symbol: str = ""      # Dukascopy symbol for 2-year backtests ("" = use the live source)
    history_point: float = 0.0    # Dukascopy price scale (prices are stored as integers)


INSTRUMENTS: dict[str, Instrument] = {
    "XAU_USD": Instrument(
        "XAU_USD", "XAU/USD (Gold)", "binance", "XAUUSDT", "metal", "XAU", "USD", 2,
        ("USD",),
        ("gold", "xau", "bullion", "fed", "fomc", "powell", "inflation", "cpi",
         "treasury", "yields", "safe haven", "safe-haven", "geopolit", "war",
         "central bank", "dollar", "rate cut", "rate hike", "tariff"),
        "XAUUSD", 1e3,
    ),
    "EUR_USD": Instrument(
        "EUR_USD", "EUR/USD", "kraken", "EUR/USD", "forex", "EUR", "USD", 5,
        ("EUR", "USD"),
        ("eur/usd", "eurusd", "euro", "ecb", "lagarde", "eurozone", "germany",
         "fed", "fomc", "powell", "dollar", "nonfarm", "payrolls", "cpi"),
        "EURUSD", 1e5,
    ),
    "GBP_USD": Instrument(
        "GBP_USD", "GBP/USD", "kraken", "GBP/USD", "forex", "GBP", "USD", 5,
        ("GBP", "USD"),
        ("gbp/usd", "gbpusd", "pound", "sterling", "cable", "boe",
         "bank of england", "bailey", "uk ", "britain", "fed", "fomc", "dollar"),
        "GBPUSD", 1e5,
    ),
    "USD_JPY": Instrument(
        "USD_JPY", "USD/JPY", "yahoo", "JPY=X", "forex", "USD", "JPY", 3,
        ("USD", "JPY"),
        ("usd/jpy", "usdjpy", "yen", "boj", "bank of japan", "ueda", "japan",
         "intervention", "fed", "fomc", "dollar", "treasury", "yields"),
        "USDJPY", 1e3,
    ),
    "BTC_USDT": Instrument(
        "BTC_USDT", "BTC/USDT perp", "binance", "BTCUSDT", "crypto", "BTC", "USDT", 1,
        ("USD",),
        ("bitcoin", "btc", "crypto", "etf", "sec ", "stablecoin", "binance",
         "coinbase", "fed", "fomc", "liquidation", "halving", "risk-on", "risk-off"),
    ),
}

# Gold/forex follow real market hours (Mon-Fri) and pay swap like a CFD broker,
# even though Binance/Kraken quote them 24/7. Weekend quotes are ignored for stops.
FOREX_LIKE = {"forex", "metal"}

# Margin the simulated broker charges (= notional / this). Typical EU/UK-style
# retail CFD limits: 30:1 majors, 20:1 gold (Binance allows far more on BTC).
# This is NOT your leverage cap - that's `max_leverage_*` on the settings page and
# limits each position's effective leverage (notional / equity).
BROKER_MARGIN_LEVERAGE = {"forex": 30.0, "metal": 20.0, "crypto": 20.0}


def get(symbol: str) -> Instrument:
    return INSTRUMENTS[symbol]


def quote_to_usd(inst: Instrument, amount_in_quote: float, price: float) -> float:
    """Convert an amount in the instrument's quote currency to USD.

    USD_JPY is quoted in JPY so we divide by the price; everything else is
    quoted in USD (USDT is treated as USD).
    """
    if inst.quote == "JPY":
        return amount_in_quote / price
    return amount_in_quote


def pnl_usd(inst: Instrument, side: str, units: float, entry: float, exit_: float) -> float:
    sign = 1 if side == "LONG" else -1
    return quote_to_usd(inst, sign * units * (exit_ - entry), exit_)


def notional_usd(inst: Instrument, units: float, price: float) -> float:
    """Position size in USD. For USD-based pairs (USD_JPY) one unit is one USD."""
    if inst.base == "USD":
        return abs(units)
    return abs(units) * price


def risk_per_unit_usd(inst: Instrument, stop_distance: float, price: float) -> float:
    return quote_to_usd(inst, stop_distance, price)
