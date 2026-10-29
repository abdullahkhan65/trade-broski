"""Hypothesis tester: does one extra rule make a setup better on data it was NOT chosen from?

    python -m app.hypothesis profile --setups "XAU_USD:session_breakout,US30:ny_orb@liq+trail1"
    python -m app.hypothesis test --setup XAU_USD:session_breakout --rule "skip_hours=13,14"

Walk-forward, so the AI can't just mine the history:
* PROFILE shows the AI only YEAR 1 of the 2-year replay, broken down by hour, day, regime, side,
  volatility and ADX bands (plus live loss causes, in ai/hypothesis.py). It proposes a rule from a fixed
  menu (signals.RULE_KNOBS).
* TEST replays the setup with and without the rule in all 3 account styles. It PASSES only if, in the
  Aggressive style and every style whose allowlist has the setup:
    - YEAR 2 (never shown to the AI) improves by at least MIN_OOS_GAIN_R,
    - year 1 doesn't get worse,
    - at least MIN_KEEP of the trades remain,
    - the setup still clears its bar with the rule (both years positive for proven setups; the Aggressive
      bar from app/study.py otherwise).
A pass becomes a proposal in the Learning tab - nothing changes until you approve it.
Both commands print one JSON object on the last line (main.py runs them in a subprocess).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

from .backtest import WINDOW, load_settings, simulate
from .signals import parse_rules
from .study import STYLES, aggressive_ok, instrument, load_scan, replay, setup_signals, style_cfg

MIN_OOS_GAIN_R = 1.0
MIN_KEEP = 0.5


def _parse_setup(setup: str) -> tuple[str, str, str]:
    pair, _, profile = setup.partition("@")
    symbol, _, strategy = pair.partition(":")
    return symbol, strategy, profile or "base"


def _bands(f: dict) -> dict[str, str]:
    atr = f.get("atr_pct")
    adx = f.get("adx")
    return {
        "hour": f"{f.get('hour', 0):02d}h",
        "day": f.get("day", "?"),
        "regime": f.get("regime", "?"),
        "side": f.get("side", "?"),
        "volatility": "?" if atr is None else ("p0-30" if atr < 30 else "p30-60" if atr < 60 else
                                               "p60-90" if atr < 90 else "p90+"),
        "adx": "?" if adx is None else ("<20" if adx < 20 else "20-25" if adx < 25 else "25-35" if adx < 35 else "35+"),
    }


def profile(setups: list[str]) -> dict:
    """Year-1 breakdown of each setup's trades (Aggressive style), for the AI to form hypotheses."""
    settings = load_settings()
    out = {}
    cache: dict[str, tuple] = {}
    for setup in setups:
        symbol, strategy, prof = _parse_setup(setup)
        try:
            if symbol not in cache:
                cache[symbol] = load_scan(symbol, settings, None)
            candles, sc = cache[symbol]
        except Exception as e:  # noqa: BLE001
            out[setup] = {"error": str(e)}
            continue
        res = simulate(instrument(symbol), candles, setup_signals(sc, strategy, prof),
                       style_cfg(settings, "aggressive", symbol), 250.0)
        cut = candles[WINDOW]["t"] + (candles[-1]["t"] - candles[WINDOW]["t"]) / 2
        y1 = [t for t in res["trades"] if datetime.fromisoformat(t["opened"]).timestamp() < cut]
        buckets: dict[str, dict[str, list]] = {}
        for t in y1:
            for dim, band in _bands(t.get("f") or {}).items():
                row = buckets.setdefault(dim, {}).setdefault(band, [0, 0.0])
                row[0] += 1
                row[1] += t["r"]
        out[setup] = {"year1": {"n": len(y1), "r": round(sum(t["r"] for t in y1), 1)},
                      "breakdown": {dim: {band: {"n": v[0], "r": round(v[1], 1)}
                                          for band, v in sorted(rows.items()) if v[0] >= 3}
                                    for dim, rows in buckets.items()}}
    return out


def test(setup: str, rule_line: str, allowlists: dict[str, list[str]] | None = None) -> dict:
    """Baseline vs with-rule, per style and year. allowlists: {style: allowlist} to know each style's bar."""
    settings = load_settings()
    symbol, strategy, prof = _parse_setup(setup)
    rule = parse_rules([f"{symbol}:{strategy} {rule_line}"]).get(f"{symbol}:{strategy}")
    if not rule:
        return {"setup": setup, "rule": rule_line, "passed": False, "reason": "rule not understood"}
    candles, sc = load_scan(symbol, settings, None)
    base_sigs = setup_signals(sc, strategy, prof)
    rule_sigs = setup_signals(sc, strategy, prof, rule=rule)
    allowlists = allowlists or {}
    styles = {"aggressive"} | {m for m, allow in allowlists.items() if any(a.split("@")[0] == f"{symbol}:{strategy}"
                                                                            for a in allow)}
    detail, fails = {}, []
    for m in STYLES:
        b = replay(symbol, candles, base_sigs, settings, m)
        w = replay(symbol, candles, rule_sigs, settings, m)
        detail[m] = {"baseline": b, "with_rule": w}
        if m not in styles:
            continue
        gain2 = w["y2_r"] - b["y2_r"]
        if gain2 < MIN_OOS_GAIN_R:
            fails.append(f"{m}: year 2 (unseen) {b['y2_r']:+.1f}R -> {w['y2_r']:+.1f}R")
        if w["y1_r"] < b["y1_r"]:
            fails.append(f"{m}: year 1 got worse ({b['y1_r']:+.1f}R -> {w['y1_r']:+.1f}R)")
        if b["n"] and w["n"] < MIN_KEEP * b["n"]:
            fails.append(f"{m}: keeps only {w['n']} of {b['n']} trades")
        if m == "aggressive" and not (aggressive_ok(w) or (w["y1_r"] > 0 and w["y2_r"] > 0)):
            fails.append("aggressive: still fails the Aggressive bar with the rule")
        if m != "aggressive" and not (w["y1_r"] > 0 and w["y2_r"] > 0):
            fails.append(f"{m}: not positive in both years with the rule")
    ag = detail["aggressive"]
    passed = not fails
    reason = (f"year 2 (unseen) {ag['baseline']['y2_r']:+.1f}R -> {ag['with_rule']['y2_r']:+.1f}R, year 1 "
              f"{ag['baseline']['y1_r']:+.1f}R -> {ag['with_rule']['y1_r']:+.1f}R, trades {ag['baseline']['n']} -> "
              f"{ag['with_rule']['n']} (Aggressive)") if passed else "; ".join(fails[:3])
    return {"setup": setup, "rule": rule_line, "passed": passed, "reason": reason, "styles": sorted(styles),
            "detail": detail}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("profile")
    pr.add_argument("--setups", required=True)
    t = sub.add_parser("test")
    t.add_argument("--setup", required=True)
    t.add_argument("--rule", required=True)
    t.add_argument("--allowlists", default="{}", help="JSON {style: [items]}")
    a = p.parse_args()
    if a.cmd == "profile":
        print(json.dumps(profile([s for s in a.setups.split(",") if s])))
    else:
        print(json.dumps(test(a.setup, a.rule, json.loads(a.allowlists))))


if __name__ == "__main__":
    main()
