"""Backtest the coded signals on history, per mode, sized like an XM Standard account.

    python -m app.backtest --mode all --symbol all --days 365
    python -m app.backtest --mode aggressive --symbol XAU_USD --days 90 --csv gold.csv

What it tests: the free signal engine + lot sizing + XM spread/swap, with the
CURRENT settings of each mode (from the DB if it exists, else defaults). It
assumes the AI takes every signal - the AI can only filter trades, so if the
raw signals have no edge, the AI is unlikely to create one.

Realism:
* entry at the NEXT candle's open plus half the XM spread; exits pay the other half
* if stop and target are both inside one candle, the STOP is assumed hit first
* gaps through the stop fill at the candle open, not the stop
* breakeven move (per mode) is applied on the candle after price reaches +X R
* swap: XM points per lot per night held (x3 Wednesdays for FX/gold)
* daily loss limit and daily profit target per mode (no new trades that day)

History is downloaded once to data/history/<SYMBOL>-<tf>.csv and reused:
gold/forex from Dukascopy's free datafeed (2+ years), BTC from Binance.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import indicators as ind
from . import risk
from .config import ENV, ROOT
from .feeds import build_feeds, dukascopy
from .feeds.prices import PriceCache, tf_seconds
from .instruments import INSTRUMENTS, Instrument, pnl_usd, quote_to_usd
from .settings_store import DEFAULTS, MODE_LABELS, MODES, cfg_for, validate
from .signals import evaluate

HIST = ROOT / "data" / "history"
WINDOW = 400                      # candles passed to the signal engine per step


# ------------------------------------------------------------------ data
async def download(symbol: str, days: int, tf: str) -> list[dict]:
    inst = INSTRUMENTS[symbol]
    if inst.history_symbol:
        return await dukascopy.download(inst.history_symbol, inst.history_point, days, bucket=tf_seconds(tf))
    start = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[int, dict] = {}
    cur = start
    feed = build_feeds(PriceCache())["binance"]
    while cur < datetime.now(timezone.utc) - timedelta(seconds=tf_seconds(tf)):
        batch = await feed._binance_klines(inst.feed_symbol, 1500, cur, tf)
        if not batch:
            break
        out.update({c["t"]: c for c in batch})
        cur = datetime.fromtimestamp(batch[-1]["t"] + tf_seconds(tf), tz=timezone.utc)
        print(f"  {symbol} {tf}: {len(out)} candles (to {cur:%Y-%m-%d})", end="\r")
    print()
    return [out[t] for t in sorted(out)]


def load_history(symbol: str, days: int, tf: str, refresh: bool) -> list[dict]:
    HIST.mkdir(parents=True, exist_ok=True)
    path = HIST / f"{symbol}-{tf}.csv"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    if path.exists() and not refresh:
        with path.open() as fh:
            rows = [{k: (int(v) if k == "t" else float(v)) for k, v in r.items()} for r in csv.DictReader(fh)]
        if rows and rows[0]["t"] <= cutoff + 7 * 86400:
            return [r for r in rows if r["t"] >= cutoff]
    rows = asyncio.run(download(symbol, days, tf))
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
def daily_atr_floors(candles: list[dict], cfg: dict, tf: str) -> dict[int, float]:
    """Dead-market floor per day: percentile of ATR over the previous 20 days."""
    s = cfg["signals"]
    a = ind.atr([c["h"] for c in candles], [c["l"] for c in candles], [c["c"] for c in candles], s["atr_period"])
    per_day = 86400 // tf_seconds(tf)
    floors: dict[int, float] = {}
    for i in range(0, len(candles), per_day):
        win = [x for x in a[max(0, i - 20 * per_day):i] if x]
        floors[candles[i]["t"] // 86400] = ind.percentile(win, s["dead_market_percentile"]) if win else 0.0
    return floors


def run(inst: Instrument, candles: list[dict], cfg: dict, start_balance: float) -> dict:
    cfg = json.loads(json.dumps(cfg))
    cfg["signals"]["news_momentum"] = False      # no historical news digest
    tf = cfg["timeframe"]
    floors = daily_atr_floors(candles, cfg, tf)
    half = cfg["broker"].get(f"spread_{inst.symbol}", 0) * inst.point / 2
    swap_pts = {s: cfg["broker"].get(f"swap_{inst.symbol}_{s.lower()}", 0.0) for s in ("LONG", "SHORT")}
    r = cfg["risk"]

    equity = peak = start_balance
    max_dd = 0.0
    trades: list[dict] = []
    skipped: dict[str, int] = {}
    pos: dict | None = None
    pending = None
    day, day_start, day_blocked = None, equity, False

    for i in range(WINDOW, len(candles)):
        c = candles[i]
        d = c["t"] // 86400
        if d != day:
            day, day_start, day_blocked = d, equity, False
        # 1) fill a pending entry at this candle's open
        if pending and pos is None:
            sig, pending = pending, None
            entry = c["o"] + half if sig.side == "LONG" else c["o"] - half
            o = risk.size_order(inst, cfg, sig.side, entry, sig.atr, equity, 0.0)
            if isinstance(o, risk.Order):
                pos = {"o": o, "sig": sig, "t": c["t"], "stop": o.stop}
            else:
                key = re.sub(r"[\d.]+", "N", o.reason.split(" ($")[0].split(";")[0])[:50]
                skipped[key] = skipped.get(key, 0) + 1
        # 2) manage the open position on this candle (bid-based highs/lows; exits pay half the spread)
        if pos:
            o, long = pos["o"], pos["o"].side == "LONG"
            exit_px = reason = None
            bid_o, bid_h, bid_l = (c["o"] - half, c["h"] - half, c["l"] - half) if long else (c["o"] + half, c["h"] + half, c["l"] + half)
            if long:
                if bid_o <= pos["stop"]:
                    exit_px, reason = bid_o, "stop (gap)"
                elif bid_l <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif bid_h >= o.target:
                    exit_px, reason = o.target, "target"
            else:
                if bid_o >= pos["stop"]:
                    exit_px, reason = bid_o, "stop (gap)"
                elif bid_h >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif bid_l <= o.target:
                    exit_px, reason = o.target, "target"
            if exit_px is not None:
                gross = pnl_usd(inst, o.side, o.units, o.entry, exit_px)
                nights = max(0, int((c["t"] - pos["t"]) // 86400))
                swap = quote_to_usd(inst, swap_pts[o.side] * inst.point * inst.contract_size * o.lots, exit_px) * nights
                net = gross + swap
                equity += net
                if reason == "stop" and abs(pos["stop"] - o.entry) < inst.point:
                    reason = "breakeven"
                trades.append({"signal": pos["sig"].type, "side": o.side, "lots": o.lots, "entry": o.entry,
                               "exit": exit_px, "pnl": net, "r": net / o.risk_usd, "reason": reason,
                               "opened": datetime.fromtimestamp(pos["t"], tz=timezone.utc).isoformat(),
                               "closed": datetime.fromtimestamp(c["t"], tz=timezone.utc).isoformat()})
                pos = None
            else:
                be = cfg["exits"]["breakeven_at_r"]
                init = abs(o.entry - o.stop)
                best = (bid_h - o.entry) if long else (o.entry - bid_l)
                if be and init > 0 and best >= be * init:
                    pos["stop"] = o.entry
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0)
        if day_start > 0:
            change = 100 * (equity - day_start) / day_start
            if change <= -r["daily_loss_limit_pct"] or (r["daily_profit_target_pct"] and change >= r["daily_profit_target_pct"]):
                day_blocked = True
        # 3) evaluate signals on this CLOSED candle; act on the next candle's open
        if pos is None and pending is None and not day_blocked and equity > 0:
            now = datetime.fromtimestamp(c["t"] + tf_seconds(tf), tz=timezone.utc)
            sigs, _ = evaluate(inst, candles[i - WINDOW + 1:i + 1], cfg, now,
                               atr_floor=floors.get(c["t"] // 86400, 0.0))
            if sigs:
                pending = sigs[0]

    buy_hold = start_balance * candles[-1]["c"] / candles[WINDOW]["c"]
    by_sig: dict[str, list] = {}
    for t in trades:
        by_sig.setdefault(t["signal"], []).append(t)
    days = max(1.0, (candles[-1]["t"] - candles[WINDOW]["t"]) / 86400)
    return {"equity": equity, "max_dd": max_dd, "trades": trades, "by_signal": by_sig,
            "buy_hold": buy_hold, "skipped": skipped, "days": days}


def report(mode: str, symbol: str, res: dict, start_balance: float) -> None:
    t = res["trades"]
    print(f"\n=== {MODE_LABELS[mode]} · {INSTRUMENTS[symbol].xm} · {res['days']:.0f} days · start ${start_balance:.2f} ===")
    if res["skipped"]:
        print("Skipped by sizing rules: " + ", ".join(f"{k} x{v}" for k, v in res["skipped"].items()))
    if not t:
        print("No trades.")
        return
    wins = [x for x in t if x["pnl"] > 0]
    daily = 100 * ((max(res["equity"], 0.01) / start_balance) ** (1 / res["days"]) - 1)
    print(f"Trades: {len(t)} | win rate {100 * len(wins) / len(t):.1f}% | avg R {sum(x['r'] for x in t) / len(t):+.3f}")
    print(f"Final equity ${res['equity']:.2f} ({100 * (res['equity'] / start_balance - 1):+.1f}%, "
          f"{daily:+.2f}%/day) | max drawdown {100 * res['max_dd']:.1f}%")
    print(f"Buy & hold (unleveraged): ${res['buy_hold']:.2f} ({100 * (res['buy_hold'] / start_balance - 1):+.1f}%)")
    for sig, rows in res["by_signal"].items():
        w = sum(1 for x in rows if x["pnl"] > 0)
        print(f"  {sig:18s} {len(rows):4d} trades  win {100 * w / len(rows):5.1f}%  "
              f"avg {sum(x['r'] for x in rows) / len(rows):+.3f}R  ${sum(x['pnl'] for x in rows):+.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="all", help="standard | moderate | aggressive | all")
    p.add_argument("--symbol", default="all", help="XAU_USD | EUR_USD | GBP_USD | USD_JPY | BTC_USDT | all")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--balance", type=float, default=ENV.starting_balance)
    p.add_argument("--refresh", action="store_true", help="re-download history")
    p.add_argument("--csv", help="write all trades to this CSV file")
    args = p.parse_args()

    settings = load_settings()
    modes = MODES if args.mode == "all" else [args.mode]
    symbols = list(INSTRUMENTS) if args.symbol == "all" else [args.symbol]
    all_rows = []
    for mode in modes:
        cfg = cfg_for(settings, mode)
        for sym in symbols:
            try:
                candles = load_history(sym, args.days, cfg["timeframe"], args.refresh)
            except Exception as e:  # noqa: BLE001
                print(f"{sym}: history download failed: {e}")
                continue
            if len(candles) <= WINDOW + 10:
                print(f"{sym}: not enough history ({len(candles)} candles)")
                continue
            res = run(INSTRUMENTS[sym], candles, cfg, args.balance)
            report(mode, sym, res, args.balance)
            all_rows += [{"mode": mode, "symbol": sym, **t} for t in res["trades"]]
    if args.csv and all_rows:
        with Path(args.csv).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} trades to {args.csv}")


if __name__ == "__main__":
    main()
