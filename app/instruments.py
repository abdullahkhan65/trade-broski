"""The live instruments, modelled on an XM Standard (MT5) account.

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
    # US indices: live prices from Yahoo futures (YM=F / NQ=F, ~23h a day). Futures trade a few points
    # away from XM's cash CFDs (basis); strategies use ranges/ATR, so that offset doesn't matter. Specs are
    # ESTIMATES until copied from MT5 (contract size, min lot).
    "US30": Instrument("US30", "US30Cash", "US30 (Dow Jones)", "yahoo", "YM=F", "index", "US30", "USD", 2, 1,
                       ("USD",), ("dow", "stocks", "wall street", "fed", "earnings"), "USA30IDXUSD", 1e3,
                       min_lot=0.1, lot_step=0.1),
    "US100": Instrument("US100", "US100Cash", "US100 (Nasdaq)", "yahoo", "NQ=F", "index", "US100", "USD", 2, 1,
                        ("USD",), ("nasdaq", "tech", "stocks", "fed", "earnings"), "USATECHIDXUSD", 1e3,
                        min_lot=0.1, lot_step=0.1),
}

# RESEARCH CANDIDATES - not traded or streamed yet. Used by the 2-year research to test
# whether our edges carry over to other XM markets. Specs (contract size, lots) are
# ESTIMATES until copied from MT5; R-based research results don't depend on them.
# A candidate that passes is moved into INSTRUMENTS with a live feed.
EXTRA_INSTRUMENTS: dict[str, Instrument] = {
    "XAG_USD": Instrument("XAG_USD", "SILVER", "SILVER (XAG/USD)", "yahoo", "SI=F", "metal", "XAG", "USD", 3, 5000,
                          ("USD",), ("silver", "xag", "precious", "fed", "inflation"), "XAGUSD", 1e3),
    "US500": Instrument("US500", "US500Cash", "US500 (S&P 500)", "yahoo", "ES=F", "index", "US500", "USD", 2, 1,
                        ("USD",), ("s&p", "stocks", "wall street", "fed"), "USA500IDXUSD", 1e3,
                        min_lot=0.1, lot_step=0.1),
    "GER40": Instrument("GER40", "GER40Cash", "GER40 (DAX)", "yahoo", "^GDAXI", "index", "GER40", "EUR", 2, 1,
                        ("EUR",), ("dax", "germany", "ecb", "european stocks"), "DEUIDXEUR", 1e3,
                        min_lot=0.1, lot_step=0.1),
    "UK100": Instrument("UK100", "UK100Cash", "UK100 (FTSE)", "yahoo", "^FTSE", "index", "UK100", "GBP", 2, 1,
                        ("GBP",), ("ftse", "uk stocks", "boe"), "GBRIDXGBP", 1e3, min_lot=0.1, lot_step=0.1),
    "WTI": Instrument("WTI", "OILCash", "WTI Crude Oil", "yahoo", "CL=F", "energy", "OIL", "USD", 2, 100,
                      ("USD",), ("oil", "crude", "opec", "wti", "brent"), "LIGHTCMDUSD", 1e3),
    "EUR_JPY": Instrument("EUR_JPY", "EURJPY", "EURJPY", "yahoo", "EURJPY=X", "forex", "EUR", "JPY", 3, 100_000,
                          ("EUR", "JPY"), ("eur/jpy", "yen", "boj", "ecb"), "EURJPY", 1e3),
    "GBP_JPY": Instrument("GBP_JPY", "GBPJPY", "GBPJPY", "yahoo", "GBPJPY=X", "forex", "GBP", "JPY", 3, 100_000,
                          ("GBP", "JPY"), ("gbp/jpy", "yen", "boj", "boe"), "GBPJPY", 1e3),
    "AUD_USD": Instrument("AUD_USD", "AUDUSD", "AUDUSD", "yahoo", "AUDUSD=X", "forex", "AUD", "USD", 5, 100_000,
                          ("AUD", "USD"), ("aud/usd", "aussie", "rba", "china"), "AUDUSD", 1e5),
}
# estimated XM Standard spreads in points for the candidates (research only)
EXTRA_SPREADS = {"XAG_USD": 30, "US30": 250, "US100": 150, "US500": 50, "GER40": 150, "UK100": 150,
                 "WTI": 5, "EUR_JPY": 25, "GBP_JPY": 35, "AUD_USD": 18}

# Gold/forex/indices/oil follow real market hours (Mon-Fri) even though some feeds quote
# them 24/7. Weekend quotes and candles are ignored for these.
FOREX_LIKE = {"forex", "metal", "index", "energy"}


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
