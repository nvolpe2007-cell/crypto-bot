#!/usr/bin/env python3
"""Run one proof-gated allocator tick → data/allocator_state.json.

Scores every forward arm on the REAL proof bar (proof_scorecard), detects the
market regime, steps the MetaAllocator (promote-on-proof / demote-on-breakdown /
regime-reweight-among-proven, persistence-gated), and writes its state — which
the live dashboard auto-discovers like any other arm.

    python scripts/run_allocator.py            # one tick
    python scripts/run_allocator.py --dry      # score + would-allocate, no write

Designed to run on a schedule (cron, e.g. hourly). Today it will sit in 100%
CASH because nothing has cleared the bar — that is the correct, honest output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.allocator import AllocConfig, MetaAllocator, score_arms, switch_readiness  # noqa: E402

DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE = os.path.join(DATA_DIR, "allocator_state.json")


def _detect_regime() -> str | None:
    """BTC daily regime via the bot's own RegimeDetector. Fail-safe to None
    (no tilt, equal-weight among proven) if data/network is unavailable."""
    try:
        import ccxt  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        from src.regime_detector import RegimeDetector  # noqa: PLC0415

        ex = ccxt.kraken({"enableRateLimit": True})
        o = ex.fetch_ohlcv("BTC/USD", timeframe="1d", limit=250)
        df = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "vol"])
        res = RegimeDetector().detect(df)
        return res.regime if res else None
    except Exception as exc:
        print(f"[regime] unavailable ({exc}); proceeding with no tilt", flush=True)
        return None


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="proof-gated allocator tick")
    ap.add_argument("--dry", action="store_true", help="score only, do not write state")
    ap.add_argument("--executable-only", dest="exec_only", action="store_true", default=True)
    ap.add_argument("--allow-paper-arms", dest="exec_only", action="store_false",
                    help="also allocate to perp/short paper arms (not Kraken-spot runnable)")
    args = ap.parse_args()

    cfg = AllocConfig(executable_only=args.exec_only)
    scored = score_arms(DATA_DIR, cfg)
    regime = _detect_regime()

    prev = None
    if os.path.exists(STATE):
        try:
            with open(STATE, "r", encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = None
    alloc = MetaAllocator.from_state(prev, cfg)
    decision = alloc.update(scored, regime)

    print(f"regime: {regime or 'unknown'}   proven arms: {decision['n_proven']}   "
          f"book equity: ${decision['equity']}   cash: {decision['cash_pct']}%")
    print(f"{'arm':<13}{'n':>4}{'exp/trade':>11}{'clust-t':>9}{'bar':>6}  verdict        weight")
    for a in sorted(scored, key=lambda x: (-x["proven"], -x["t_clustered"])):
        w = alloc.weights.get(a["name"], 0.0)
        verdict = "PROVEN" if a["proven"] else f"need n>={30 if a['n'] < 30 else ''} t>{a['t_family']}"
        print(f"{a['name']:<13}{a['n']:>4}{a['expectancy']:>11.4f}{a['t_clustered']:>9.2f}"
              f"{a['t_family']:>6.2f}  {verdict:<14} {w*100:>5.1f}%")
    if decision["demoted"]:
        print(f"demoted (drawdown cap): {', '.join(decision['demoted'])}")

    if args.dry:
        print("\n[dry] not written.")
        return
    state = alloc.to_state()
    state["readiness"] = switch_readiness(DATA_DIR, cfg)  # human "when do we switch" view
    _atomic_write(STATE, state)
    print(f"\nwrote {STATE}")


if __name__ == "__main__":
    main()
