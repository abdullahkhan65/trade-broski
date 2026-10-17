from datetime import datetime, timezone

import pytest

from app import indicators as ind
from app import risk
from app.broker import PaperBroker
from app.db import DB
from app.feeds.prices import PriceCache
from app.instruments import INSTRUMENTS, notional_usd, pnl_usd
from app.settings_store import DEFAULTS, FIELD_BY_KEY, SettingsStore, preset, validate
from app.feeds.dukascopy import RECORD, parse_day
from app.signals import evaluate, session_breakout

TUESDAY_NOON = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return validate(DEFAULTS)


@pytest.fixture
def setup():
    db = DB(":memory:")
    db.ensure_account(250.0)
    prices = PriceCache()
    return db, prices, PaperBroker(db, prices), SettingsStore(db)


# ---------------------------------------------------------------- indicators
def test_ema_and_rsi_basic():
    vals = [float(i) for i in range(1, 31)]
    e = ind.ema(vals, 10)
    assert e[8] is None and e[9] == pytest.approx(5.5)
    assert e[-1] > e[-2]
    r = ind.rsi(vals, 14)
    assert r[-1] == 100.0
    falling = list(reversed(vals))
    assert ind.rsi(falling, 14)[-1] == pytest.approx(0.0)


def test_atr_constant_range():
    h = [11.0] * 30
    l = [9.0] * 30
    c = [10.0] * 30
    assert ind.atr(h, l, c, 14)[-1] == pytest.approx(2.0)


# ------------------------------------------------------------- instruments
def test_usdjpy_pnl_converted_to_usd():
    inst = INSTRUMENTS["USD_JPY"]
    # long 10,000 USD at 150.00, exit 151.00 -> 10,000 JPY -> ~66.2 USD
    assert pnl_usd(inst, "LONG", 10_000, 150.0, 151.0) == pytest.approx(10_000 / 151.0)
    assert notional_usd(inst, 10_000, 150.0) == 10_000


def test_short_pnl_sign():
    inst = INSTRUMENTS["EUR_USD"]
    assert pnl_usd(inst, "SHORT", 1000, 1.10, 1.09) == pytest.approx(10.0)


# --------------------------------------------------------------------- risk
def test_size_risks_one_percent(cfg):
    inst = INSTRUMENTS["EUR_USD"]
    o = risk.size_order(inst, cfg, "LONG", 1.1000, 0.0008, 250.0, 0.0)
    assert isinstance(o, risk.Order)
    # stop 1.5 ATR away -> loss at stop = 1% of 250
    assert pnl_usd(inst, "LONG", o.units, o.entry, o.stop) == pytest.approx(-2.5)
    assert o.target - o.entry == pytest.approx(3.0 * 0.0008)
    assert o.effective_leverage <= 10


def test_oversized_trade_skipped_or_shrunk(cfg):
    inst = INSTRUMENTS["XAU_USD"]
    # gold at 4000 with a tiny ATR needs huge leverage
    r = risk.size_order(inst, cfg, "LONG", 4000.0, 0.5, 250.0, 0.0)
    assert isinstance(r, risk.Rejection) and "leverage" in r.reason
    cfg["risk"]["oversize_action"] = "shrink"
    o = risk.size_order(inst, cfg, "LONG", 4000.0, 0.5, 250.0, 0.0)
    assert isinstance(o, risk.Order) and o.shrunk
    assert o.effective_leverage == pytest.approx(5.0)
    assert o.risk_usd < 2.5


def test_tight_stop_needing_too_much_leverage_is_skipped(cfg):
    r = risk.size_order(INSTRUMENTS["EUR_USD"], cfg, "LONG", 1.1, 0.0008, 250.0, 0.0, stop_atr=0.1)
    assert isinstance(r, risk.Rejection) and "leverage" in r.reason


def test_ai_cannot_exceed_caps(cfg):
    inst = INSTRUMENTS["EUR_USD"]
    # AI asks for a 0.1 ATR stop + 0.1 ATR target: clamped to the minimums
    o = risk.size_order(inst, cfg, "LONG", 1.1, 0.002, 250.0, 0.0, stop_atr=0.1, target_atr=0.1)
    assert isinstance(o, risk.Order)
    assert o.leverage <= 10
    assert abs(o.entry - o.stop) == pytest.approx(cfg["exits"]["min_stop_atr"] * 0.002)
    assert abs(o.target - o.entry) >= abs(o.entry - o.stop) * cfg["exits"]["min_reward_risk"] - 1e-12


def test_margin_cap(cfg):
    inst = INSTRUMENTS["EUR_USD"]
    r = risk.size_order(inst, cfg, "LONG", 1.1, 0.0008, 250.0, margin_used=60.0)
    assert isinstance(r, risk.Rejection) and "margin" in r.reason


def test_portfolio_rules(cfg):
    pos = [{"symbol": "EUR_USD", "asset_class": "forex"}, {"symbol": "GBP_USD", "asset_class": "forex"}]
    assert "USD forex" in risk.portfolio_check(INSTRUMENTS["USD_JPY"], cfg, pos)
    assert "already" in risk.portfolio_check(INSTRUMENTS["EUR_USD"], cfg, pos)
    assert risk.portfolio_check(INSTRUMENTS["BTC_USDT"], cfg, pos) is None


def test_loss_limits(cfg):
    assert risk.account_limit_status(cfg, 200, 250, 250, 250)[0] == "halted"
    assert risk.account_limit_status(cfg, 234, 250, 240, 250)[0] == "paused_weekly"
    assert risk.account_limit_status(cfg, 242, 250, 250, 248)[0] == "paused_daily"
    assert risk.account_limit_status(cfg, 249, 250, 250, 250) is None


# ------------------------------------------------------------------- broker
def _open(broker, prices, cfg, symbol, side, bid, ask, atr):
    inst = INSTRUMENTS[symbol]
    prices.update(symbol, bid, ask)
    entry = broker.entry_price(cfg, inst, side, prices.get(symbol))
    o = risk.size_order(inst, cfg, side, entry, atr, 250.0, 0.0)
    assert isinstance(o, risk.Order)
    broker.open(cfg, o, "trend_pullback", "test", None)
    return o


def test_stop_fills_at_gap_price(setup, cfg):
    db, prices, broker, _ = setup
    o = _open(broker, prices, cfg, "EUR_USD", "LONG", 1.10000, 1.10010, 0.0008)
    prices.update("EUR_USD", o.stop - 0.0010, o.stop - 0.0009)   # gapped through the stop
    closed = broker.check_exits(cfg, TUESDAY_NOON)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop (gap)"
    assert closed[0]["exit"] == pytest.approx(o.stop - 0.0010)
    assert closed[0]["pnl_r"] < -1


def test_target_and_cash(setup, cfg):
    db, prices, broker, _ = setup
    o = _open(broker, prices, cfg, "EUR_USD", "SHORT", 1.10000, 1.10010, 0.0008)
    prices.update("EUR_USD", o.target - 0.0005, o.target - 0.0004)
    t = broker.check_exits(cfg, TUESDAY_NOON)[0]
    assert t["exit_reason"] == "target" and t["exit"] == o.target
    assert t["pnl_r"] == pytest.approx(2.0, rel=1e-6)
    st = broker.state(cfg)
    assert st.cash == pytest.approx(250 + t["pnl_usd"])
    assert not broker.positions()


def test_no_stops_while_market_closed(setup, cfg):
    db, prices, broker, _ = setup
    o = _open(broker, prices, cfg, "XAU_USD", "LONG", 4000.0, 4000.1, 8.0)
    prices.update("XAU_USD", o.stop - 50, o.stop - 49.9)   # weekend quote from a 24/7 venue
    assert broker.check_exits(cfg, SATURDAY) == []
    assert broker.check_exits(cfg, TUESDAY_NOON)[0]["exit_reason"] == "stop (gap)"


def test_spread_widened_to_broker_minimum(setup, cfg):
    _, prices, broker, _ = setup
    inst = INSTRUMENTS["XAU_USD"]
    prices.update("XAU_USD", 4000.00, 4000.02)            # exchange spread 0.02
    q = prices.get("XAU_USD")
    assert broker.entry_price(cfg, inst, "LONG", q) - broker.exit_price(cfg, inst, "LONG", q) == pytest.approx(0.30)
    prices.update("XAU_USD", 4000.0, 4001.0)              # wider than minimum: kept as is
    assert broker.broker_quote(cfg, inst, prices.get("XAU_USD")) == (4000.0, 4001.0)


def test_dukascopy_parse_aggregates_to_15m():
    from datetime import date
    rows = b"".join(RECORD.pack(m * 60, 109000 + m, 109001 + m, 108990 + m, 109010 + m, 1.0) for m in range(30))
    candles = parse_day(rows, date(2025, 3, 12), 1e5)
    assert len(candles) == 2
    first = candles[0]
    assert first["o"] == pytest.approx(1.09000) and first["c"] == pytest.approx(1.09015)
    assert first["h"] == pytest.approx(1.09024) and first["l"] == pytest.approx(1.08990)


def test_btc_fees_and_funding(setup, cfg):
    db, prices, broker, _ = setup
    _open(broker, prices, cfg, "BTC_USDT", "LONG", 100_000, 100_001, 600)
    p = broker.positions()[0]
    assert p["fees"] > 0
    paid = broker.apply_funding(0.0001)          # longs pay positive funding
    assert paid < 0
    t = broker.close_at_market(cfg, broker.positions()[0], "test")
    assert t["funding"] == pytest.approx(paid)
    assert t["pnl_usd"] < 0                       # spread + slippage + fees + funding


def test_swap_triple_wednesday(setup, cfg):
    db, prices, broker, _ = setup
    _open(broker, prices, cfg, "EUR_USD", "LONG", 1.1, 1.1001, 0.0008)
    tue = broker.apply_swap(cfg, datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc))
    wed = broker.apply_swap(cfg, datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc))
    assert tue < 0 and wed == pytest.approx(3 * tue, rel=1e-3)


def test_halve_position(setup, cfg):
    db, prices, broker, _ = setup
    o = _open(broker, prices, cfg, "XAU_USD", "LONG", 4000.0, 4000.3, 8.0)
    broker.close_at_market(cfg, broker.positions()[0], "weekend halve", fraction=0.5)
    p = broker.positions()[0]
    assert p["units"] == pytest.approx(o.units / 2) and p["weekend_halved"] == 1


def test_stop_out_closes_biggest_loser(setup, cfg):
    db, prices, broker, _ = setup
    cfg["risk"]["stop_out_margin_level_pct"] = 100
    cfg["risk"]["max_margin_used_pct"] = 80
    _open(broker, prices, cfg, "EUR_USD", "LONG", 1.1, 1.1001, 0.0008)
    db.execute("UPDATE positions SET margin=300")     # force margin level < 100%
    closed = broker.check_stop_out(cfg)
    assert closed and "stop-out" in closed[0]["exit_reason"]


# ----------------------------------------------------------------- settings
def test_hard_limits_cannot_be_exceeded():
    d = validate(DEFAULTS)
    d["risk"]["risk_per_trade_pct"] = 50
    d["risk"]["max_leverage_crypto"] = 100
    d["ai"]["monthly_budget_usd"] = 100
    out = validate(d)
    assert out["risk"]["risk_per_trade_pct"] == FIELD_BY_KEY["risk.risk_per_trade_pct"].max
    assert out["risk"]["max_leverage_crypto"] == 10
    assert out["ai"]["monthly_budget_usd"] == 5


def test_presets_and_history(setup):
    db, _, _, store = setup
    assert preset("aggressive")["risk"]["risk_per_trade_pct"] == 2.0
    assert preset("conservative")["risk"]["max_leverage_crypto"] == 2.0
    store.current["instruments"]["BTC_USDT"] = False
    store.save(store.current, "ui")
    store.apply_preset("aggressive")
    assert store.current["instruments"]["BTC_USDT"] is False   # instruments kept across presets
    first = store.history()[-1]["id"]
    store.revert(first)
    assert store.current["risk"]["risk_per_trade_pct"] == 1.0


def test_bad_choice_rejected():
    d = validate(DEFAULTS)
    d["weekend"]["mode"] = "yolo"
    with pytest.raises(ValueError):
        validate(d)


# ------------------------------------------------------------------ signals
def _candles(closes, start=1_760_000_000, spread=0.0005):
    return [{"t": start + i * 900, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1} for i, c in enumerate(closes)]


def test_session_breakout_first_close_only(cfg):
    inst = INSTRUMENTS["EUR_USD"]
    day = datetime(2026, 10, 20, tzinfo=timezone.utc)
    t0 = int(day.timestamp())
    asian = [{"t": t0 + i * 900, "o": 1.1, "h": 1.1010, "l": 1.0990, "c": 1.1, "v": 1} for i in range(28)]
    after = [{"t": t0 + (28 + i) * 900, "o": 1.1, "h": 1.1008, "l": 1.0995, "c": 1.1005, "v": 1} for i in range(4)]
    brk = {"t": t0 + 32 * 900, "o": 1.1005, "h": 1.1020, "l": 1.1, "c": 1.1015, "v": 1}
    now = datetime.fromtimestamp(brk["t"] + 900, tz=timezone.utc)
    sig = session_breakout(inst, asian + after + [brk], cfg, 0.0008, now)
    assert sig and sig.side == "LONG"
    again = dict(brk, t=brk["t"] + 900)
    assert session_breakout(inst, asian + after + [brk, again], cfg, 0.0008, now) is None


def test_filters_block_outside_session(cfg):
    inst = INSTRUMENTS["EUR_USD"]
    closes = [1.1 + 0.0001 * ((i % 7) - 3) for i in range(300)]
    sunday_night = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)  # Asia only
    sigs, why = evaluate(inst, _candles(closes), cfg, sunday_night, atr_floor=0.0)
    assert sigs == [] and "session" in why
