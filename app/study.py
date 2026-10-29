"""2-year setup study and the signal cache behind it (market study, hypothesis tester).

    python -m app.study grid --symbols all --refresh      # every setup x exit x account, both-years rule
    python -m app.study grid --symbols XAG_USD,US500

How it works:
1. SCAN each market once over ~2 years of M15 history with EVERY strategy on and no allowlist, H1 or SMC
   step (signals.raw_signals). Each signal keeps its features (hour, day, regime, ADX, volatility
   percentile), its H1 trend, its SMC verdict and the liquidity target it would get. The scan is cached in
   data/history/scan-<SYMBOL>.pkl and only the new candles are scanned next time.
2. For a setup (symbol, strategy, exit profile, optional per-setup rule) pick its signals from the scan with
   the live filters (with the H1 trend, SMC path clear) and replay them with backtest.simulate under each
   account's rules at $250 - the same sizing, costs and exits as live.
3. Split the trades at the middle of the period: year 1 / year 2.

Bars:
* PROVEN (all accounts): made money in BOTH years in Standard, Moderate and Aggressive, 15+ trades a year.
* AGGRESSIVE: in the Aggressive account's own rules: positive over 2 years (R and $), profit factor >= 1.1,
  10+ trades a year, and the weaker year no worse than -3R. A looser bar - more trades, more variance -
  used only by the Aggressive account, and watched live by the audit's drift check.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

from . import smc
from .backtest import HIST, WINDOW, load_history, simulate
from .exits import PROFILES
from .feeds.prices import tf_seconds
from .instruments import EXTRA_INSTRUMENTS, EXTRA_SPREADS, INSTRUMENTS, Instrument
from .settings_store import DEFAULTS, cfg_for, validate
from .signals import htf_trend, raw_signals, rule_block, set_liquidity_targets, smc_verdict

STRATEGIES = ["trend_pullback", "session_breakout", "range_reversion", "momentum_breakout", "london_orb",
              "ny_orb", "pdh_breakout", "zone_retest", "htf_pullback", "liquidity_sweep"]
GRID_PROFILES = ["base", "trail1.5", "liq+trail1"]
STYLES = ["standard", "moderate", "aggressive"]
DAYS = 730
SCAN_VERSION = 1
MIN_YEAR_TRADES = 15                 # proven bar (same as promoter.verify)
AGG_MIN_YEAR_TRADES = 10
AGG_WORST_YEAR_R = -3.0
AGG_MIN_PF = 1.1


def instrument(symbol: str) -> Instrument:
    return INSTRUMENTS.get(symbol) or EXTRA_INSTRUMENTS[symbol]


def style_cfg(settings: dict, mode: str, symbol: str) -> dict:
    """An account's cfg for replaying one market (research markets get their estimated spread)."""
    cfg = copy.deepcopy(cfg_for(settings, mode))
    if f"spread_{symbol}" not in cfg["broker"]:
        cfg["broker"][f"spread_{symbol}"] = EXTRA_SPREADS.get(symbol, 0)
    cfg["risk"].setdefault("max_leverage_energy", cfg["risk"]["max_leverage_metal"])     # oil: no setting of its own
    cfg["broker"].setdefault("leverage_energy", 100.0)
    return cfg


def scan_cfg(settings: dict) -> dict:
    cfg = copy.deepcopy(cfg_for(settings, "aggressive"))
    s = cfg["signals"]
    for k in STRATEGIES:
        s[k] = True
    s.update(news_momentum=False, allowlist=[], setup_rules=[], liquidity_targets=False)
    return cfg


def _fingerprint(cfg: dict) -> str:
    keep = {k: v for k, v in cfg["signals"].items() if k not in ("allowlist", "setup_rules")}
    return hashlib.sha1(json.dumps([SCAN_VERSION, keep, cfg["exits"]["default_stop_atr"]], sort_keys=True)
                        .encode()).hexdigest()[:12]


def scan(inst: Instrument, candles: list[dict], cfg: dict, start: int = WINDOW) -> dict[int, list]:
    """{candle index: [signals]} for every strategy, with features, H1 trend, SMC verdict, liquidity target."""
    from .backtest import daily_atr_floors
    floors = daily_atr_floors(candles, cfg, cfg["timeframe"])
    out: dict[int, list] = {}
    step = tf_seconds(cfg["timeframe"])
    for i in range(max(start, WINDOW), len(candles)):
        c = candles[i]
        win = candles[i - WINDOW + 1:i + 1]
        now = datetime.fromtimestamp(c["t"] + step, tz=timezone.utc)
        sigs, _, atr, levels = raw_signals(inst, win, cfg, now, atr_floor=floors.get(c["t"] // 86400, 0.0))
        if not sigs:
            continue
        trend = htf_trend(win, cfg) or "n/a"
        levels = levels or smc.key_levels(win, atr)
        zs = smc.zones(win, atr)
        for x in sigs:
            x.context["f"]["h1"] = trend
            x.context["f"]["smc"] = smc_verdict(x, cfg, atr, levels, zs, inst.decimals)
            if not x.target_atr:
                y = copy.copy(x)
                y.note = ""
                set_liquidity_targets([y], cfg, atr, levels)
                x.context["liq_target_atr"] = y.target_atr
        out[i] = sigs
    return out


def load_scan(symbol: str, settings: dict | None = None, refresh: bool | None = False) -> tuple[list[dict], dict]:
    """(candles, scan) for ~2 years, from the cache when it still matches the history and settings.
    refresh=None: download new history only if the file is more than 6 days old."""
    settings = settings or validate(DEFAULTS)
    cfg = scan_cfg(settings)
    if refresh is None:
        hist = HIST / f"{symbol}-{cfg['timeframe']}.csv"
        refresh = not hist.exists() or time.time() - os.path.getmtime(hist) > 6 * 86400
    candles = load_history(symbol, DAYS, cfg["timeframe"], refresh)
    path = HIST / f"scan-{symbol}.pkl"
    fp = _fingerprint(cfg)
    cached = None
    if path.exists():
        try:
            with path.open("rb") as fh:
                cached = pickle.load(fh)
        except Exception:  # noqa: BLE001
            cached = None
    by_t: dict[int, list] = {}
    start = WINDOW
    if cached and cached.get("fp") == fp:
        by_t = cached["by_t"]
        index = {c["t"]: i for i, c in enumerate(candles)}
        last = cached.get("last_t", 0)
        start = next((i + 1 for i in range(len(candles) - 1, -1, -1) if candles[i]["t"] <= last), WINDOW)
        by_t = {t: v for t, v in by_t.items() if t in index}
    fresh = scan(instrument(symbol), candles, cfg, start)
    by_t.update({candles[i]["t"]: v for i, v in fresh.items()})
    with path.open("wb") as fh:
        pickle.dump({"fp": fp, "last_t": candles[-1]["t"], "by_t": by_t}, fh)
    index = {c["t"]: i for i, c in enumerate(candles)}
    return candles, {index[t]: v for t, v in by_t.items() if t in index and index[t] >= WINDOW}


def setup_signals(sc: dict[int, list], strategy: str, profile: str = "base", h1: bool = True,
                  smc_filter: bool = True, rule: dict | None = None) -> dict[int, object]:
    """One setup's signals from the scan, through the live filters and (optionally) a per-setup rule."""
    liq = PROFILES.get(profile, {}).get("liquidity_target")
    out = {}
    for i, sigs in sc.items():
        for x in sigs:
            if x.type != strategy:
                continue
            f = x.context["f"]
            if h1 and f["h1"] != "n/a" and {"up": "LONG", "down": "SHORT"}.get(f["h1"]) != x.side:
                break
            if smc_filter and f["smc"]:
                break
            if rule_block(rule, f):
                break
            y = copy.copy(x)
            y.context = {**x.context, "exit_profile": profile}
            if liq and not x.target_atr:
                y.target_atr = x.context.get("liq_target_atr")
            out[i] = y
            break
    return out


def split(trades: list[dict], candles: list[dict]) -> dict:
    mid = candles[WINDOW]["t"] + (candles[-1]["t"] - candles[WINDOW]["t"]) / 2
    y1 = [t for t in trades if datetime.fromisoformat(t["opened"]).timestamp() < mid]
    y2 = [t for t in trades if datetime.fromisoformat(t["opened"]).timestamp() >= mid]
    win = sum(t["r"] for t in trades if t["r"] > 0)
    loss = -sum(t["r"] for t in trades if t["r"] < 0)
    return {"n": len(trades), "r": round(sum(t["r"] for t in trades), 2),
            "y1_n": len(y1), "y1_r": round(sum(t["r"] for t in y1), 2),
            "y2_n": len(y2), "y2_r": round(sum(t["r"] for t in y2), 2),
            "pf": round(win / loss, 2) if loss else (99.0 if win else 0.0),
            "wins": sum(1 for t in trades if t["r"] > 0)}


def replay(symbol: str, candles: list[dict], sigs: dict, settings: dict, mode: str) -> dict:
    res = simulate(instrument(symbol), candles, sigs, style_cfg(settings, mode, symbol), 250.0)
    return {**split(res["trades"], candles), "equity": round(res["equity"], 2),
            "max_dd": round(100 * res["max_dd"], 1), "weeks": round(res["days"] / 7, 1)}


def proven(per_style: dict) -> bool:
    return all(r["y1_r"] > 0 and r["y2_r"] > 0 and r["y1_n"] >= MIN_YEAR_TRADES and r["y2_n"] >= MIN_YEAR_TRADES
               for r in (per_style[m] for m in STYLES))


def aggressive_ok(r: dict) -> bool:
    return (r["r"] > 0 and r["equity"] > 250 and r["pf"] >= AGG_MIN_PF and r["y1_n"] >= AGG_MIN_YEAR_TRADES
            and r["y2_n"] >= AGG_MIN_YEAR_TRADES and min(r["y1_r"], r["y2_r"]) >= AGG_WORST_YEAR_R)


def grid_symbol(symbol: str, refresh: bool = False) -> list[dict]:
    settings = validate(DEFAULTS)
    candles, sc = load_scan(symbol, settings, refresh)
    rows = []
    for strat in STRATEGIES:
        for prof in GRID_PROFILES:
            sigs = setup_signals(sc, strat, prof)
            if len(sigs) < 2 * AGG_MIN_YEAR_TRADES:
                continue
            per = {m: replay(symbol, candles, sigs, settings, m) for m in STYLES}
            rows.append({"symbol": symbol, "strategy": strat, "profile": prof, "signals": len(sigs),
                         "pass_proven": proven(per), "pass_aggressive": aggressive_ok(per["aggressive"]),
                         **{m: per[m] for m in STYLES}})
    return rows


def _grid_worker(args: tuple[str, bool]) -> list[dict]:
    os.nice(5)
    try:
        return grid_symbol(*args)
    except Exception as e:  # noqa: BLE001
        print(f"{args[0]}: failed: {e}", file=sys.stderr)
        return []


def best_profiles(rows: list[dict], key: str) -> list[dict]:
    """Per (symbol, strategy): the passing exit profile with the best weaker year (one entry per setup)."""
    best: dict[tuple, dict] = {}
    for r in rows:
        if not r[key]:
            continue
        score = min(r["aggressive"]["y1_r"], r["aggressive"]["y2_r"])
        k = (r["symbol"], r["strategy"])
        if k not in best or score > best[k]["_score"]:
            best[k] = {**r, "_score": score}
    return sorted(best.values(), key=lambda r: -r["_score"])


def item(r: dict) -> str:
    return f"{r['symbol']}:{r['strategy']}" + ("" if r["profile"] == "base" else f"@{r['profile']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid")
    g.add_argument("--symbols", default="all")
    g.add_argument("--refresh", action="store_true", help="download the newest history first")
    g.add_argument("--out", default=str(HIST.parent / "study.json"))
    g.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = p.parse_args()
    syms = (list(INSTRUMENTS) + list(EXTRA_INSTRUMENTS)) if a.symbols == "all" else a.symbols.split(",")
    syms = [s for s in syms if s != "USD_JPY"]            # no 2-year history file
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        rows = [r for part in ex.map(_grid_worker, [(s, a.refresh) for s in syms]) for r in part]
    with open(a.out, "w") as fh:
        json.dump(rows, fh)
    for key in ("pass_proven", "pass_aggressive"):
        best = best_profiles(rows, key)
        print(f"\n== {key[5:].upper()}: {len(best)} setups ==")
        for r in best:
            ag = r["aggressive"]
            print(f"{item(r):38s} agg {ag['n']:3d} tr ({ag['n'] / ag['weeks']:.2f}/wk) {ag['y1_r']:+6.1f}R / {ag['y2_r']:+6.1f}R"
                  f"  pf {ag['pf']:.2f}  ${ag['equity']:.0f}  dd {ag['max_dd']:.0f}%")
    print(f"\nWrote {len(rows)} rows to {a.out}")


if __name__ == "__main__":
    main()
