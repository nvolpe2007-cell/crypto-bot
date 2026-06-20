#!/usr/bin/env python3
"""
Walk-forward + robustness report for the ORB strategy — the honest way to choose
params (out-of-sample), not by sweeping until a backtest looks good.

  python -m stockbot.run_walkforward --synthetic
  python -m stockbot.run_walkforward --csv SPY_5m.csv --symbol SPY --train-days 40 --test-days 10
  python -m stockbot.run_walkforward --yf SPY --interval 5m --period 60d
"""
from __future__ import annotations

import argparse

from .backtest import net_returns
from .data import fetch_yfinance, load_csv, synthetic_intraday
from .metrics import render, summary, verdict
from .walkforward import default_grid, grid_robustness, walk_forward


def main() -> int:
    ap = argparse.ArgumentParser(description="ORB walk-forward + robustness (sim only)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true")
    src.add_argument("--csv")
    src.add_argument("--yf")
    ap.add_argument("--symbol", default="SYNTH")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--train-days", type=int, default=40)
    ap.add_argument("--test-days", type=int, default=10)
    ap.add_argument("--anchored", action="store_true", help="expanding train window")
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--cost-bps", type=float, default=2.0)
    args = ap.parse_args()

    if args.synthetic:
        df, sym = synthetic_intraday(days=120), args.symbol
    elif args.csv:
        df, sym = load_csv(args.csv), args.symbol
    else:
        df, sym = fetch_yfinance(args.yf, period=args.period, interval=args.interval), args.yf

    grid = default_grid(cost_bps_per_side=args.cost_bps)
    folds, oos = walk_forward(df, grid, train_days=args.train_days,
                              test_days=args.test_days, anchored=args.anchored,
                              min_trades=args.min_trades, symbol=sym)
    oos_s = summary(net_returns(oos))

    print("=" * 72)
    print(f"WALK-FORWARD — {sym}  (train={args.train_days}d test={args.test_days}d "
          f"{'anchored' if args.anchored else 'rolling'}, {len(grid)} param sets/fold)")
    print("=" * 72)
    print("OUT-OF-SAMPLE (pooled test trades — the honest record):")
    print(render(oos_s, label=f"{sym} ORB walk-forward OOS"))
    print("-" * 72)
    for f in folds:
        s = summary(net_returns(f.oos_trades))
        print(f"  {f.test_start}→{f.test_end}  {f.cfg_label():<22} "
              f"oos n={s['n']:<3} net={s['total']*100:+.2f}%")

    rob = grid_robustness(df, grid, symbol=sym)
    print("-" * 72)
    print("ROBUSTNESS (full-period grid — plateau vs knife-edge):")
    bc = rob["best_cfg"]
    print(f"  expectancy across {rob['n_params']} param sets: "
          f"best={rob['expectancy_best']*100:+.3f}%  med={rob['expectancy_median']*100:+.3f}%  "
          f"worst={rob['expectancy_worst']*100:+.3f}%  positive={rob['share_positive']*100:.0f}%")
    print(f"  best cell: OR{bc.or_minutes}/R{bc.target_r}/{bc.direction}  "
          f"deflated_sharpe={rob['best_deflated_sharpe']:.2f} "
          f"(vs expected-max-Sharpe sr0={rob['sr0_expected_max_sharpe']:.3f} across the grid)")
    print("-" * 72)
    print("READ THIS:")
    print("  • Trust the OUT-OF-SAMPLE block, not the best full-period cell — the latter")
    print("    is selection-biased (that's what the deflated Sharpe corrects for).")
    print("  • A robust edge is a PLATEAU (most cells positive) + OOS that holds up, not")
    print("    one lucky cell. Sim only; PDT + real fills still apply live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
