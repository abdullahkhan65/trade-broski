"""The five instruments, modelled on an XM Standard (MT5) account.

Each instrument has an XM symbol name, contract size (units per 1.00 lot),
digits/point and lot limits. Spreads, swaps and margin leverage are on the
settings page (Broker group) so you can copy them from MT5:
Market Watch -> right-click symbol -> Specification.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str            # internal id
    xm: str                # XM / MT5 symbol name
    display: str
    source: str            # live price source: "binance" | "kraken" | "yahoo"
    feed_symbol: str       # symbol on that source
    asset_class: str       # "forex" | "metal" | "crypto"
    base: str
    quote: str
    digits: int            # MT5 digits; point = 10 ** -digits
    contract_size: float   # units per 1.00 lot
    currencies: tuple[str, ...]   # calendar currencies that move this instrument
    keywords: tuple[str, ...]     # headline keywords (lower-case) that relate to it
    history_symbol: str = ""      # Dukascopy symbol for backtests ("" = use the live source)
    history_point: float = 0.0    # Dukascopy price scale
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_lot: float = 50.0

    @property
    def point(self) -> float:
        return 10.0 ** -self.digits

    @property
    def decimals(self) -> int:
        return self.digits

    @property
    def pip(self) -> float:
        """Classic pip: 10 points on 5/3-digit FX, 1 point otherwise."""
        return self.point * 10 if self.asset_class == "forex" else self.point


INSTRUMENTS: dict[str, Instrument] = {
    "XAU_USD": Instrument(
        "XAU_USD", "GOLD", "GOLD (XAU/USD)", "binance", "XAUUSDT", "metal", "XAU", "USD", 2, 100,
        ("USD",),
        ("gold", "xau", "bullion", "fed", "fomc", "powell", "inflation", "cpi",
         "treasury", "yields", "safe haven", "safe-haven", "geopolit", "war",
         "central bank", "dollar", "rate cut", "rate hike", "tariff"),
        "XAUUSD", 1e3,
    ),
    "EUR_USD": Instrument(
        "EUR_USD", "EURUSD", "EURUSD", "kraken", "EUR/USD", "forex", "EUR", "USD", 5, 100_000,
        ("EUR", "USD"),
        ("eur/usd", "eurusd", "euro", "ecb", "lagarde", "eurozone", "germany",
         "fed", "fomc", "powell", "dollar", "nonfarm", "payrolls", "cpi"),
        "EURUSD", 1e5,
    ),
    "GBP_USD": Instrument(
        "GBP_USD", "GBPUSD", "GBPUSD", "kraken", "GBP/USD", "forex", "GBP", "USD", 5, 100_000,
        ("GBP", "USD"),
        ("gbp/usd", "gbpusd", "pound", "sterling", "cable", "boe",
         "bank of england", "bailey", "uk ", "britain", "fed", "fomc", "dollar"),
        "GBPUSD", 1e5,
    ),
    "USD_JPY": Instrument(
        "USD_JPY", "USDJPY", "USDJPY", "yahoo", "JPY=X", "forex", "USD", "JPY", 3, 100_000,
        ("USD", "JPY"),
        ("usd/jpy", "usdjpy", "yen", "boj", "bank of japan", "ueda", "japan",
         "intervention", "fed", "fomc", "dollar", "treasury", "yields"),
        "USDJPY", 1e3,
    ),
    "BTC_USDT": Instrument(
        "BTC_USDT", "BTCUSD", "BTCUSD", "binance", "BTCUSDT", "crypto", "BTC", "USD", 2, 1,
        ("USD",),
        ("bitcoin", "btc", "crypto", "etf", "sec ", "stablecoin", "binance",
         "coinbase", "fed", "fomc", "liquidation", "halving", "risk-on", "risk-off"),
    ),
}

# Gold/forex follow real market hours (Mon-Fri) even though Binance/Kraken quote
# them 24/7. Weekend quotes and candles are ignored for these.
FOREX_LIKE = {"forex", "metal"}


def get(symbol: str) -> Instrument:
    return INSTRUMENTS[symbol]


def quote_to_usd(inst: Instrument, amount_in_quote: float, price: float) -> float:
    """USD_JPY is quoted in JPY, so divide by the price; the rest are quoted in USD."""
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


def usd_per_lot(inst: Instrument, price_move: float, price: float) -> float:
    """USD gained/lost by 1.00 lot for a price move (e.g. the stop distance)."""
    return quote_to_usd(inst, price_move * inst.contract_size, price)
