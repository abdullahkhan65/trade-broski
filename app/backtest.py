"""Backtest the coded signals on history: `python -m app.backtest --symbol EUR_USD --days 730`

What it tests: the free signal engine + risk engine + realistic costs, with the
CURRENT settings (from the DB if it exists, else defaults). It assumes the AI
takes every signal - the AI can only filter trades, so if the raw signals have
no edge, the AI is unlikely to create one.

Realism:
* entry at the NEXT candle's open plus half the min spread from settings (+ slippage/fee for BTC)
* if stop and target are both inside one candle, the STOP is assumed hit first
* gaps through the stop fill at the candle open, not the stop
* swap / funding approximated per day held from settings

History is downloaded once to data/history/<SYMBOL>.csv and reused:
gold and forex from Dukascopy's free datafeed (2+ years), BTC from Binance.
Every new rule should pass here on 2 years of data before going live.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import indicators as ind
from . import risk
from .config import ENV, ROOT
from .feeds import build_feeds
from .feeds import dukascopy
from .feeds.prices import CANDLE_SECONDS, PriceCache
from .instruments import INSTRUMENTS, Instrument, notional_usd, pnl_usd
from .settings_store import DEFAULTS, validate
from .signals import TWENTY_DAYS_OF_15M, evaluate

HIST = ROOT / "data" / "history"
WINDOW = 400                      # candles passed to the signal engine per step
BTC_SPREAD = 0.1


# ------------------------------------------------------------------ data
async def download(symbol: str, days: int) -> list[dict]:
    inst = INSTRUMENTS[symbol]
    if inst.history_symbol:
        return await dukascopy.download(inst.history_symbol, inst.history_point, days)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[int, dict] = {}
    cur = start
    feed = build_feeds(PriceCache())["binance"]
    while cur < datetime.now(timezone.utc) - timedelta(minutes=15):
        batch = await feed._binance_klines(inst.feed_symbol, 1500, cur)
        if not batch:
            break
        out.update({c["t"]: c for c in batch})
        cur = datetime.fromtimestamp(batch[-1]["t"] + CANDLE_SECONDS, tz=timezone.utc)
        print(f"  {symbol}: {len(out)} candles (to {cur:%Y-%m-%d})", end="\r")
    print()
    return [out[t] for t in sorted(out)]


def load_history(symbol: str, days: int, refresh: bool) -> list[dict]:
    HIST.mkdir(parents=True, exist_ok=True)
    path = HIST / f"{symbol}.csv"
    if path.exists() and not refresh:
        with path.open() as fh:
            rows = [{k: (int(v) if k == "t" else float(v)) for k, v in r.items()} for r in csv.DictReader(fh)]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        rows = [r for r in rows if r["t"] >= cutoff]
        if rows:
            return rows
    rows = asyncio.run(download(symbol, days))
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["t", "o", "h", "l", "c", "v"])
        w.writeheader()
        w.writerows(rows)
    return rows


def load_settings() -> dict:
    if ENV.db_path.exists():
        from .db import DB
        row = DB(ENV.db_path).one("SELECT data FROM settings_versions ORDER BY id DESC LIMIT 1")
        if row:
            return validate(json.loads(row["data"]))
    return validate(DEFAULTS)


# ------------------------------------------------------------- simulation
def daily_atr_floors(candles: list[dict], cfg: dict) -> dict[int, float]:
    """Dead-market floor per day: percentile of ATR over the previous 20 days."""
    s = cfg["signals"]
    a = ind.atr([c["h"] for c in candles], [c["l"] for c in candles], [c["c"] for c in candles], s["atr_period"])
    floors: dict[int, float] = {}
    for i in range(0, len(candles), 96):
        win = [x for x in a[max(0, i - TWENTY_DAYS_OF_15M):i] if x]
        floors[candles[i]["t"] // 86400] = ind.percentile(win, s["dead_market_percentile"]) if win else 0.0
    return floors


def run(inst: Instrument, candles: list[dict], cfg: dict, start_balance: float) -> dict:
    cfg = json.loads(json.dumps(cfg))
    cfg["signals"]["news_momentum"] = False      # no historical news digest
    floors = daily_atr_floors(candles, cfg)
    half_spread = cfg["costs"].get(f"min_spread_{inst.symbol}", BTC_SPREAD) / 2
    slip = cfg["costs"]["btc_slippage_pct"] / 100 if inst.asset_class == "crypto" else 0
    fee_pct = cfg["costs"]["btc_taker_fee_pct"] / 100 if inst.asset_class == "crypto" else 0

    equity = peak = start_balance
    max_dd = 0.0
    trades: list[dict] = []
    pos: dict | None = None
    pending = None
    curve = []

    for i in range(WINDOW, len(candles)):
        c = candles[i]
        # 1) fill a pending entry at this candle's open
        if pending and pos is None:
            sig = pending
            pending = None
            entry = c["o"] + half_spread if sig.side == "LONG" else c["o"] - half_spread
            entry *= (1 + slip) if sig.side == "LONG" else (1 - slip)
            o = risk.size_order(inst, cfg, sig.side, entry, sig.atr, equity, 0.0)
            if isinstance(o, risk.Order):
                fee = notional_usd(inst, o.units, entry) * fee_pct
                equity -= fee
                pos = {"o": o, "sig": sig, "t": c["t"], "fees": fee}
        # 2) manage open position on this candle
        if pos:
            o = pos["o"]
            exit_px = reason = None
            if o.side == "LONG":
                if c["o"] <= o.stop:
                    exit_px, reason = c["o"] - half_spread, "stop (gap)"
                elif c["l"] - half_spread <= o.stop:
                    exit_px, reason = o.stop, "stop"
                elif c["h"] - half_spread >= o.target:
                    exit_px, reason = o.target, "target"
            else:
                if c["o"] >= o.stop:
                    exit_px, reason = c["o"] + half_spread, "stop (gap)"
                elif c["h"] + half_spread >= o.stop:
                    exit_px, reason = o.stop, "stop"
                elif c["l"] + half_spread <= o.target:
                    exit_px, reason = o.target, "target"
            if exit_px is not None:
                gross = pnl_usd(inst, o.side, o.units, o.entry, exit_px)
                fee = notional_usd(inst, o.units, exit_px) * fee_pct
                days = (c["t"] - pos["t"]) / 86400
                if inst.asset_class == "crypto":
                    carry = -notional_usd(inst, o.units, exit_px) * 0.0001 * 3 * days  # ~0.01%/8h typical funding
                    carry *= 1 if o.side == "LONG" else -1
                else:
                    rate = cfg["costs"].get(f"swap_{inst.symbol}_{o.side.lower()}", 0)
                    carry = notional_usd(inst, o.units, exit_px) * rate / 100 / 365 * days
                net = gross - fee + carry
                equity += gross - fee + carry
                total_fees = pos["fees"] + fee
                trades.append({"signal": pos["sig"].type, "side": o.side, "entry": o.entry, "exit": exit_px,
                               "pnl": net - pos["fees"], "r": (net - pos["fees"]) / o.risk_usd,
                               "fees": total_fees, "reason": reason,
                               "opened": datetime.fromtimestamp(pos["t"], tz=timezone.utc).isoformat(),
                               "closed": datetime.fromtimestamp(c["t"], tz=timezone.utc).isoformat()})
                pos = None
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        if i % 96 == 0:
            curve.append((c["t"], equity))
        # 3) evaluate signals on this CLOSED candle; act on the next candle's open
        if pos is None and pending is None:
            now = datetime.fromtimestamp(c["t"] + CANDLE_SECONDS, tz=timezone.utc)
            sigs, _ = evaluate(inst, candles[i - WINDOW + 1:i + 1], cfg, now,
                               atr_floor=floors.get(c["t"] // 86400, 0.0))
            if sigs:
                pending = sigs[0]

    buy_hold = start_balance * candles[-1]["c"] / candles[WINDOW]["c"]
    by_sig: dict[str, list] = {}
    for t in trades:
        by_sig.setdefault(t["signal"], []).append(t)
    return {"equity": equity, "max_dd": max_dd, "trades": trades, "by_signal": by_sig,
            "buy_hold": buy_hold, "curve": curve}


def report(symbol: str, res: dict, start_balance: float, days: int) -> None:
    t = res["trades"]
    print(f"\n=== {symbol} · {days} days · start ${start_balance:.2f} ===")
    if not t:
        print("No trades.")
        return
    wins = [x for x in t if x["pnl"] > 0]
    print(f"Trades: {len(t)} | win rate {100 * len(wins) / len(t):.1f}% | avg R {sum(x['r'] for x in t) / len(t):+.3f}")
    print(f"Final equity ${res['equity']:.2f} ({100 * (res['equity'] / start_balance - 1):+.1f}%) | "
          f"max drawdown {100 * res['max_dd']:.1f}% | fees ${sum(x['fees'] for x in t):.2f}")
    print(f"Buy & hold (unleveraged): ${res['buy_hold']:.2f} ({100 * (res['buy_hold'] / start_balance - 1):+.1f}%)")
    print("By signal:")
    for sig, rows in res["by_signal"].items():
        w = sum(1 for x in rows if x["pnl"] > 0)
        print(f"  {sig:18s} {len(rows):4d} trades  win {100 * w / len(rows):5.1f}%  "
              f"avg {sum(x['r'] for x in rows) / len(rows):+.3f}R  ${sum(x['pnl'] for x in rows):+.2f}")
    verdict = "HAS an edge so far" if sum(x["r"] for x in t) > 0 and res["equity"] > start_balance else "NO edge - don't go live with this"
    print(f"Verdict: {verdict} (needs 30+ trades per signal to mean anything)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="all", help="XAU_USD | EUR_USD | GBP_USD | USD_JPY | BTC_USDT | all")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--balance", type=float, default=ENV.starting_balance)
    p.add_argument("--refresh", action="store_true", help="re-download history")
    p.add_argument("--csv", help="write all trades to this CSV file")
    args = p.parse_args()

    cfg = load_settings()
    print(f"Settings profile: {cfg['profile']} | risk {cfg['risk']['risk_per_trade_pct']}% | "
          f"signals: {[k for k in ('trend_pullback', 'session_breakout') if cfg['signals'][k]]}")
    symbols = list(INSTRUMENTS) if args.symbol == "all" else [args.symbol]
    all_rows = []
    for sym in symbols:
        try:
            candles = load_history(sym, args.days, args.refresh)
        except Exception as e:  # noqa: BLE001
            print(f"{sym}: history download failed: {e}")
            continue
        if len(candles) <= WINDOW + 10:
            print(f"{sym}: not enough history ({len(candles)} candles)")
            continue
        res = run(INSTRUMENTS[sym], candles, cfg, args.balance)
        report(sym, res, args.balance, args.days)
        all_rows += [{"symbol": sym, **t} for t in res["trades"]]
    if args.csv and all_rows:
        with Path(args.csv).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} trades to {args.csv}")


if __name__ == "__main__":
    main()
