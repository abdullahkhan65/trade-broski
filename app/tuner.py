"""Weekly self-tuning with a walk-forward test: `python -m app.tuner --mode standard`

For one account (mode), on the last N days of history for each enabled symbol:
1. Run the signal engine once (signals don't depend on stops/targets).
2. TRAIN on the older 70%: try a small grid around the current settings -
   default stop (x ATR), default target (x ATR), breakeven-at-R - and pick the
   one with the highest total R (needs enough trades).
3. TEST on the newest 30%, which the search never saw: compare the winner with
   the CURRENT settings.
4. Recommend applying it only if it beats the current settings on the test
   period by at least 1R in total AND is profitable there. Otherwise keep what
   we have. This is what stops the bot "learning" patterns that were luck.

The tuner never touches risk %, leverage or the hard limits - only how stops and
targets are placed, within the mode's own allowed range.

Prints one JSON object on the last line (main.py reads it, applies it if
recommended, and logs it to the Learning tab).
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import time
from typing import Any

from .backtest import HIST, WINDOW, collect_signals, load_history, load_settings, simulate
from .instruments import INSTRUMENTS
from .settings_store import MODES, cfg_for

DAYS = {"15m": 60, "5m": 30}
TRAIN_SHARE = 0.7
MIN_TRAIN_TRADES = 15
MIN_TEST_TRADES = 8
MIN_IMPROVEMENT_R = 1.0
BALANCE = 250.0


def _round(x: float) -> float:
    return round(x * 10) / 10


def grid(exits: dict) -> list[dict]:
    s0, t0, b0 = exits["default_stop_atr"], exits["default_target_atr"], exits["breakeven_at_r"]
    lo, hi = exits["min_stop_atr"], exits["max_stop_atr"]
    stops = sorted({_round(min(max(s0 + d, lo), hi)) for d in (-0.3, 0.0, 0.3)})
    targets = sorted({_round(max(t0 * k, 0.5)) for k in (0.75, 1.0, 1.25)})
    bes = sorted({0.0, 1.0, 1.5, b0})
    out = []
    for s, t, b in itertools.product(stops, targets, bes):
        t = max(t, _round(s * exits["min_reward_risk"] + 0.04))
        p = {"default_stop_atr": s, "default_target_atr": t, "breakeven_at_r": b}
        if p not in out:
            out.append(p)
    return out


def _history(sym: str, days: int, tf: str) -> list[dict]:
    path = HIST / f"{sym}-{tf}.csv"
    stale = not path.exists() or time.time() - os.path.getmtime(path) > 6 * 86400
    return load_history(sym, days, tf, refresh=stale)


def _score(datasets: list[tuple], params: dict, base_cfg: dict, part: str) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg["exits"].update(params)
    total_r, n, net = 0.0, 0, 0.0
    for inst, candles, sigs, split in datasets:
        start, end = (WINDOW, split) if part == "train" else (split, len(candles))
        res = simulate(inst, candles, sigs, cfg, BALANCE, start, end)
        total_r += res["total_r"]
        n += len(res["trades"])
        net += res["equity"] - BALANCE
    return {"total_r": round(total_r, 2), "trades": n, "avg_r": round(total_r / n, 3) if n else None,
            "net_usd": round(net, 2)}


def tune(mode: str, days: int | None = None) -> dict[str, Any]:
    settings = load_settings()
    cfg = cfg_for(settings, mode)
    tf = cfg["timeframe"]
    days = days or DAYS[tf]
    current = {k: cfg["exits"][k] for k in ("default_stop_atr", "default_target_atr", "breakeven_at_r")}
    datasets = []
    for sym, inst in INSTRUMENTS.items():
        if not cfg["instruments"].get(sym):
            continue
        try:
            candles = _history(sym, days, tf)
        except Exception as e:  # noqa: BLE001
            print(f"{sym}: history failed: {e}")
            continue
        if len(candles) < WINDOW + 200:
            continue
        sigs = collect_signals(inst, candles, cfg)
        split = WINDOW + int((len(candles) - WINDOW) * TRAIN_SHARE)
        datasets.append((inst, candles, sigs, split))
        print(f"{sym}: {len(candles)} candles, {len(sigs)} signals")
    result: dict[str, Any] = {"mode": mode, "timeframe": tf, "days": days, "current": current,
                              "best": None, "apply": False}
    if not datasets:
        result["reason"] = "no history available"
        return result

    train = [(p, _score(datasets, p, cfg, "train")) for p in grid(cfg["exits"])]
    ok = [x for x in train if x[1]["trades"] >= MIN_TRAIN_TRADES]
    if not ok:
        result["reason"] = f"too few trades to judge (need {MIN_TRAIN_TRADES} in the training period)"
        result["train_current"] = next((m for p, m in train if p == current), None)
        return result
    best, best_train = max(ok, key=lambda x: x[1]["total_r"])
    cur_train = next((m for p, m in train if p == current), _score(datasets, current, cfg, "train"))
    best_test = _score(datasets, best, cfg, "test")
    cur_test = _score(datasets, current, cfg, "test")
    result.update({"best": best, "train_best": best_train, "train_current": cur_train,
                   "test_best": best_test, "test_current": cur_test, "combos_tried": len(train)})

    if best == current:
        result["reason"] = "current settings are already the best on training data"
    elif best_test["trades"] < MIN_TEST_TRADES:
        result["reason"] = f"only {best_test['trades']} trades in the test period - not enough evidence"
    elif best_test["total_r"] <= 0:
        result["reason"] = f"best setting lost money on unseen data ({best_test['total_r']:+.1f}R) - keeping current"
    elif best_test["total_r"] < cur_test["total_r"] + MIN_IMPROVEMENT_R:
        result["reason"] = (f"not clearly better on unseen data ({best_test['total_r']:+.1f}R vs "
                            f"{cur_test['total_r']:+.1f}R now) - keeping current")
    else:
        result["apply"] = True
        result["reason"] = (f"better on unseen data: {best_test['total_r']:+.1f}R vs {cur_test['total_r']:+.1f}R "
                            f"over {best_test['trades']} trades")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--days", type=int)
    args = p.parse_args()
    res = tune(args.mode, args.days)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
