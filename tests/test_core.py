import sqlite3
from datetime import datetime, timezone

import pytest

from app import indicators as ind
from app import risk
from app.broker import PaperBroker
from app.db import DB
from app.feeds.dukascopy import RECORD, parse_day
from app.feeds.prices import PriceCache, load_candles, store_candles
from app.instruments import INSTRUMENTS, notional_usd, pnl_usd, usd_per_lot
from app.settings_store import DEFAULTS, FIELD_BY_KEY, MODES, SettingsStore, cfg_for, validate
from app.signals import evaluate, session_breakout

TUESDAY_NOON = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
SETTINGS = validate(DEFAULTS)


def cfg(mode="standard"):
    return cfg_for(validate(DEFAULTS), mode)


@pytest.fixture
def setup():
    db = DB(":memory:")
    db.ensure_accounts(MODES, 250.0)
    prices = PriceCache()
    return db, prices, PaperBroker(db, prices), SettingsStore(db)


# ---------------------------------------------------------------- indicators
def test_ema_and_rsi_basic():
    vals = [float(i) for i in range(1, 31)]
    e = ind.ema(vals, 10)
    assert e[8] is None and e[9] == pytest.approx(5.5)
    assert ind.rsi(vals, 14)[-1] == 100.0
    assert ind.rsi(list(reversed(vals)), 14)[-1] == pytest.approx(0.0)


def test_atr_constant_range():
    assert ind.atr([11.0] * 30, [9.0] * 30, [10.0] * 30, 14)[-1] == pytest.approx(2.0)


# ------------------------------------------------------------- instruments
def test_xm_contract_sizes_and_pip_values():
    eur, gold, jpy, btc = (INSTRUMENTS[s] for s in ("EUR_USD", "XAU_USD", "USD_JPY", "BTC_USDT"))
    assert usd_per_lot(eur, 0.0001, 1.10) == pytest.approx(10.0)       # $10 per pip per lot
    assert usd_per_lot(gold, 1.0, 4000) == pytest.approx(100.0)        # $100 per $1 per lot (100 oz)
    assert usd_per_lot(jpy, 0.01, 150.0) == pytest.approx(1000 / 150)  # 1000 JPY per pip per lot
    assert usd_per_lot(btc, 100, 80000) == pytest.approx(100.0)        # 1 BTC per lot
    assert eur.point == pytest.approx(0.00001) and gold.point == pytest.approx(0.01)
    assert notional_usd(jpy, 100_000, 150.0) == 100_000


def test_short_pnl_sign():
    assert pnl_usd(INSTRUMENTS["EUR_USD"], "SHORT", 1000, 1.10, 1.09) == pytest.approx(10.0)


# --------------------------------------------------------------- lot sizing
def test_lots_floor_to_step_and_risk_within_budget():
    o = risk.size_order(INSTRUMENTS["EUR_USD"], cfg("standard"), "LONG", 1.1000, 0.0008, 250.0, 0.0)
    assert isinstance(o, risk.Order)
    assert o.lots == 0.02                     # $2.50 budget / $120 per lot-stop = 0.0208 -> 0.02
    assert o.risk_usd <= 2.5 + 1e-9
    assert o.units == pytest.approx(2000)
    assert o.margin == pytest.approx(notional_usd(INSTRUMENTS["EUR_USD"], 2000, 1.1) / 1000)


def test_min_lot_rules_per_mode():
    gold = INSTRUMENTS["XAU_USD"]
    # 15m gold ATR $8 -> 1.5 ATR stop = $12 per 0.01 lot = 4.8% of $250
    std = risk.size_order(gold, cfg("standard"), "LONG", 4000.0, 8.0, 250.0, 0.0)
    assert isinstance(std, risk.Rejection) and "0.01 lot would risk 4.8%" in std.reason
    assert isinstance(risk.size_order(gold, cfg("moderate"), "LONG", 4000.0, 8.0, 250.0, 0.0), risk.Rejection)
    agg = risk.size_order(gold, cfg("aggressive"), "LONG", 4000.0, 8.0, 250.0, 0.0)
    assert isinstance(agg, risk.Order) and agg.lots == 0.01 and "allowed up to" in agg.note
    assert agg.risk_pct == pytest.approx(3.84, abs=0.01)    # aggressive default stop = 1.2 ATR


def test_ai_cannot_exceed_caps():
    c = cfg("standard")
    o = risk.size_order(INSTRUMENTS["EUR_USD"], c, "LONG", 1.1, 0.002, 250.0, 0.0, stop_atr=0.1, target_atr=0.1)
    assert isinstance(o, risk.Order)
    assert abs(o.entry - o.stop) == pytest.approx(c["exits"]["min_stop_atr"] * 0.002)
    assert abs(o.target - o.entry) >= abs(o.entry - o.stop) * c["exits"]["min_reward_risk"] - 1e-12
    assert o.leverage <= c["risk"]["max_leverage_forex"]


def test_leverage_cap_reduces_lots():
    c = cfg("standard")
    c["risk"]["risk_per_trade_pct"] = 3.0
    o = risk.size_order(INSTRUMENTS["EUR_USD"], c, "LONG", 1.1, 0.0003, 250.0, 0.0)
    assert isinstance(o, risk.Order) and o.leverage <= 10 and "leverage cap" in o.note


def test_margin_cap():
    r = risk.size_order(INSTRUMENTS["EUR_USD"], cfg("standard"), "LONG", 1.1, 0.0008, 250.0, margin_used=124.0)
    assert isinstance(r, risk.Rejection) and "margin" in r.reason


def test_portfolio_rules():
    c = cfg("moderate")
    pos = [{"symbol": "EUR_USD", "asset_class": "forex"}, {"symbol": "GBP_USD", "asset_class": "forex"}]
    assert "USD forex" in risk.portfolio_check(INSTRUMENTS["USD_JPY"], c, pos)
    assert "already" in risk.portfolio_check(INSTRUMENTS["EUR_USD"], c, pos)
    assert risk.portfolio_check(INSTRUMENTS["BTC_USDT"], c, pos) is None


def test_loss_limits_and_daily_target():
    s, a = cfg("standard"), cfg("aggressive")
    assert risk.account_limit_status(s, 200, 250, 250, 250)[0] == "halted"
    assert risk.account_limit_status(s, 234, 250, 240, 250)[0] == "paused_weekly"
    assert risk.account_limit_status(s, 242, 250, 250, 248)[0] == "paused_daily"
    assert risk.account_limit_status(s, 260, 260, 250, 250) is None            # standard has no profit target
    assert risk.account_limit_status(a, 258, 258, 250, 250)[0] == "target_hit"  # +3.2% >= 3%


# ------------------------------------------------------------------- broker
def _open(broker, prices, account, symbol, side, bid, ask, atr):
    c = cfg(account)
    inst = INSTRUMENTS[symbol]
    prices.update(symbol, bid, ask, TUESDAY_NOON)
    entry = broker.entry_price(c, inst, side, prices.get(symbol))
    o = risk.size_order(inst, c, side, entry, atr, 250.0, 0.0)
    assert isinstance(o, risk.Order), o
    broker.open(c, account, o, "trend_pullback", "test", None)
    return o


def test_xm_spread_applied(setup):
    _, prices, broker, _ = setup
    c, inst = cfg(), INSTRUMENTS["EUR_USD"]
    prices.update("EUR_USD", 1.10000, 1.10002, TUESDAY_NOON)     # exchange spread 0.2 pip
    bid, ask = broker.broker_quote(c, inst, prices.get("EUR_USD"))
    assert (ask - bid) / inst.point == pytest.approx(16)          # widened to XM's 1.6 pips
    prices.update("EUR_USD", 1.1000, 1.1003, TUESDAY_NOON)       # wider live spread is kept
    assert broker.broker_quote(c, inst, prices.get("EUR_USD")) == (1.1000, 1.1003)


def test_accounts_are_separate(setup):
    _, prices, broker, _ = setup
    _open(broker, prices, "standard", "EUR_USD", "LONG", 1.1, 1.10016, 0.0008)
    _open(broker, prices, "aggressive", "EUR_USD", "SHORT", 1.1, 1.10016, 0.0008)
    assert len(broker.positions("standard")) == 1 and len(broker.positions("aggressive")) == 1
    assert broker.positions("moderate") == []


def test_stop_gap_and_cash(setup):
    _, prices, broker, _ = setup
    o = _open(broker, prices, "standard", "EUR_USD", "LONG", 1.10000, 1.10016, 0.0008)
    prices.update("EUR_USD", o.stop - 0.0010, o.stop - 0.0009, TUESDAY_NOON)
    closed = broker.check_exits(lambda a: cfg(a), TUESDAY_NOON)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop loss (gap)"
    # feed spread (1 pip) is tighter than XM's 1.6 pips, so the bid is mid - 0.8 pip
    assert closed[0]["exit"] == pytest.approx(o.stop - 0.00095 - 0.00008)
    assert broker.state(cfg(), "standard").cash == pytest.approx(250 + closed[0]["pnl_usd"])


def test_target_fill(setup):
    _, prices, broker, _ = setup
    o = _open(broker, prices, "standard", "EUR_USD", "SHORT", 1.10000, 1.10016, 0.0008)
    prices.update("EUR_USD", o.target - 0.0005, o.target - 0.0004, TUESDAY_NOON)
    t = broker.check_exits(lambda a: cfg(a), TUESDAY_NOON)[0]
    assert t["exit_reason"] == "take profit" and t["exit"] == o.target
    assert t["pnl_r"] > 1.9


def test_breakeven_move_then_stop(setup):
    _, prices, broker, _ = setup
    o = _open(broker, prices, "aggressive", "EUR_USD", "LONG", 1.10000, 1.10016, 0.0008)
    risk_dist = o.entry - o.stop
    prices.update("EUR_USD", o.entry + 1.05 * risk_dist, o.entry + 1.05 * risk_dist + 0.00016, TUESDAY_NOON)
    assert broker.check_exits(lambda a: cfg(a), TUESDAY_NOON) == []
    assert broker.positions("aggressive")[0]["stop"] == pytest.approx(o.entry)
    prices.update("EUR_USD", o.entry - 0.00001, o.entry + 0.00015, TUESDAY_NOON)
    t = broker.check_exits(lambda a: cfg(a), TUESDAY_NOON)[0]
    assert t["exit_reason"].startswith("breakeven stop") and abs(t["pnl_usd"]) < 0.1


def test_no_stops_while_market_closed(setup):
    _, prices, broker, _ = setup
    o = _open(broker, prices, "aggressive", "XAU_USD", "LONG", 4000.0, 4000.35, 8.0)
    prices.quotes["XAU_USD"].bid, prices.quotes["XAU_USD"].ask = o.stop - 50, o.stop - 49.6
    assert broker.check_exits(lambda a: cfg(a), SATURDAY) == []
    assert broker.check_exits(lambda a: cfg(a), TUESDAY_NOON)[0]["exit_reason"] == "stop loss (gap)"


def test_weekend_quotes_frozen():
    prices = PriceCache()
    prices.update("EUR_USD", 1.1000, 1.1001, datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc))  # Fri 16:00 NY
    prices.update("EUR_USD", 1.2000, 1.2001, SATURDAY)
    assert prices.get("EUR_USD").bid == 1.1000
    prices.update("BTC_USDT", 80000, 80001, SATURDAY)
    prices.update("BTC_USDT", 81000, 81001, SATURDAY)
    assert prices.get("BTC_USDT").bid == 81000


def test_weekend_candles_dropped():
    db = DB(":memory:")
    sat, tue = int(SATURDAY.timestamp()), int(TUESDAY_NOON.timestamp())

    def c(t):
        return {"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    store_candles(db, "EUR_USD", [c(sat), c(tue)], "15m")
    store_candles(db, "BTC_USDT", [c(sat), c(tue)], "15m")
    assert [r["t"] for r in load_candles(db, "EUR_USD", 10, "15m")] == [tue]
    assert len(load_candles(db, "BTC_USDT", 10, "15m")) == 2


def test_swap_points_triple_wednesday(setup):
    _, prices, broker, _ = setup
    o = _open(broker, prices, "standard", "EUR_USD", "LONG", 1.1, 1.10016, 0.0008)
    tue = broker.apply_swap(SETTINGS, datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc))
    assert tue == pytest.approx(-8.0 * 0.00001 * 100_000 * o.lots)       # -8 points x $1/point/lot
    wed = broker.apply_swap(SETTINGS, datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc))
    assert wed == pytest.approx(3 * tue)
    assert broker.apply_swap(SETTINGS, datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)) == 0   # Saturday


def test_halve_and_min_lot_close(setup):
    db, prices, broker, _ = setup
    o = _open(broker, prices, "moderate", "EUR_USD", "LONG", 1.1, 1.10016, 0.0008)
    assert o.lots == 0.03
    broker.close_at_market(cfg("moderate"), broker.positions("moderate")[0], "weekend halve", fraction=0.5)
    p = broker.positions("moderate")[0]
    assert p["lots"] in (0.01, 0.02) and p["weekend_halved"] == 1          # half of 0.03, rounded to the 0.01 step
    db.execute("UPDATE positions SET lots=0.01, units=1000")
    broker.close_at_market(cfg("moderate"), broker.positions("moderate")[0], "weekend halve", fraction=0.5)
    assert broker.positions("moderate") == []                              # 0.01 can't be halved


def test_stop_out(setup):
    db, prices, broker, _ = setup
    _open(broker, prices, "standard", "EUR_USD", "LONG", 1.1, 1.10016, 0.0008)
    db.execute("UPDATE positions SET margin=2000")
    closed = broker.check_stop_out(cfg(), "standard")
    assert closed and "stop out" in closed[0]["exit_reason"]


# ----------------------------------------------------------------- settings
def test_hard_limits_cannot_be_exceeded():
    d = validate(DEFAULTS)
    d["modes"]["aggressive"]["risk"]["risk_per_trade_pct"] = 50
    d["modes"]["standard"]["risk"]["max_leverage_crypto"] = 100
    d["ai"]["monthly_budget_usd"] = 100
    out = validate(d)
    assert out["modes"]["aggressive"]["risk"]["risk_per_trade_pct"] == 5.0
    assert out["modes"]["standard"]["risk"]["max_leverage_crypto"] == 10
    assert out["ai"]["monthly_budget_usd"] == FIELD_BY_KEY["ai.monthly_budget_usd"].max


def test_modes_differ_and_reset(setup):
    _, _, _, store = setup
    assert store.cfg("aggressive")["timeframe"] == "5m" and store.cfg("standard")["timeframe"] == "15m"
    data = store.current
    data["modes"]["moderate"]["risk"]["risk_per_trade_pct"] = 2.5
    store.save(data)
    assert store.cfg("moderate")["risk"]["risk_per_trade_pct"] == 2.5
    store.reset_mode("moderate")
    assert store.cfg("moderate")["risk"]["risk_per_trade_pct"] == 1.5


def test_bad_choice_rejected():
    d = validate(DEFAULTS)
    d["modes"]["standard"]["weekend"]["mode"] = "yolo"
    with pytest.raises(ValueError):
        validate(d)


def test_old_database_is_backed_up(tmp_path):
    path = tmp_path / "trader.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE account (id INTEGER)")
    conn.commit()
    conn.close()
    db = DB(path)
    assert db.migrated_from and (tmp_path / db.migrated_from.split("/")[-1]).exists()
    db.ensure_accounts(MODES, 250)
    assert len(db.all("SELECT * FROM accounts")) == 3
    assert DB(path).migrated_from is None          # second start: no migration


# ------------------------------------------------------------------ signals
def _candles(closes, start=1_760_000_000, spread=0.0005):
    return [{"t": start + i * 900, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1} for i, c in enumerate(closes)]


def test_session_breakout_first_close_only():
    inst = INSTRUMENTS["EUR_USD"]
    t0 = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
    asian = [{"t": t0 + i * 900, "o": 1.1, "h": 1.1010, "l": 1.0990, "c": 1.1, "v": 1} for i in range(28)]
    after = [{"t": t0 + (28 + i) * 900, "o": 1.1, "h": 1.1008, "l": 1.0995, "c": 1.1005, "v": 1} for i in range(4)]
    brk = {"t": t0 + 32 * 900, "o": 1.1005, "h": 1.1020, "l": 1.1, "c": 1.1015, "v": 1}
    now = datetime.fromtimestamp(brk["t"] + 900, tz=timezone.utc)
    sig = session_breakout(inst, asian + after + [brk], cfg(), 0.0008, now)
    assert sig and sig.side == "LONG"
    again = dict(brk, t=brk["t"] + 900)
    assert session_breakout(inst, asian + after + [brk, again], cfg(), 0.0008, now) is None


def test_session_breakout_on_m5():
    inst = INSTRUMENTS["EUR_USD"]
    t0 = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
    asian = [{"t": t0 + i * 300, "o": 1.1, "h": 1.1010, "l": 1.0990, "c": 1.1, "v": 1} for i in range(84)]
    brk = {"t": t0 + 84 * 300, "o": 1.1005, "h": 1.1020, "l": 1.1, "c": 1.1015, "v": 1}
    now = datetime.fromtimestamp(brk["t"] + 300, tz=timezone.utc)
    assert session_breakout(inst, asian + [brk], cfg("aggressive"), 0.0004, now).side == "LONG"


def test_filters_block_outside_session():
    inst = INSTRUMENTS["EUR_USD"]
    closes = [1.1 + 0.0001 * ((i % 7) - 3) for i in range(300)]
    sunday_night = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)
    sigs, why = evaluate(inst, _candles(closes), cfg(), sunday_night, atr_floor=0.0)
    assert sigs == [] and "session" in why


def test_dukascopy_parse_aggregates():
    from datetime import date
    rows = b"".join(RECORD.pack(m * 60, 109000 + m, 109001 + m, 108990 + m, 109010 + m, 1.0) for m in range(30))
    c15 = parse_day(rows, date(2025, 3, 12), 1e5)
    assert len(c15) == 2 and c15[0]["o"] == pytest.approx(1.09000) and c15[0]["c"] == pytest.approx(1.09015)
    assert len(parse_day(rows, date(2025, 3, 12), 1e5, bucket=300)) == 6
