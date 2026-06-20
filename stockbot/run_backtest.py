#!/usr/bin/env python3
"""
Run the ORB intraday backtest and print the honest proof scorecard.

Examples:
  python -m stockbot.run_backtest --synthetic            # offline demo
  python -m stockbot.run_backtest --csv SPY_5m.csv --symbol SPY
  python -m stockbot.run_backtest --yf SPY --interval 5m --period 60d

All sim — there is NO broker and NO live trading here. A green result is
IN-SAMPLE only; see the caveats printed at the end.
"""
from __future__ import annotations

import argparse
from datetime import time

from .backtest import run_backtest, net_returns
from .data import load_csv, fetch_yfinance, synthetic_intraday
from .metrics import render, summary
from .strategy import ORBConfig


def main() -> int:
    ap = argparse.ArgumentParser(description="ORB intraday backtest (paper/sim only)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true", help="deterministic demo data")
    src.add_argument("--csv", help="CSV of intraday bars")
    src.add_argument("--yf", help="symbol to pull via yfinance")
    ap.add_argument("--symbol", default="SYNTH")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--direction", default="long", choices=["long", "short", "both"])
    ap.add_argument("--target-r", type=float, default=2.0,
                    help="R-multiple target; <=0 means ride to the close")
    ap.add_argument("--cost-bps", type=float, default=2.0, help="per-side spread+slippage")
    args = ap.parse_args()

    if args.synthetic:
        df, sym = synthetic_intraday(), args.symbol
    elif args.csv:
        df, sym = load_csv(args.csv), args.symbol
    else:
        df, sym = fetch_yfinance(args.yf, period=args.period, interval=args.interval), args.yf

    cfg = ORBConfig(or_minutes=args.or_minutes, direction=args.direction,
                    target_r=(args.target_r if args.target_r > 0 else None),
                    cost_bps_per_side=args.cost_bps)
    trades = run_backtest(df, cfg, symbol=sym)
    s = summary(net_returns(trades))

    print("=" * 70)
    print(f"ORB INTRADAY BACKTEST — {sym}  ({len(df)} bars, {args.or_minutes}m OR, "
          f"dir={args.direction}, target_r={args.target_r}, cost={args.cost_bps}bps/side)")
    print("=" * 70)
    print(render(s, label=f"{sym} ORB"))
    print("-" * 70)
    for t in trades[-12:]:
        print(f"  {t.date} {t.side:<5} entry {t.entry_px:>9.2f} → exit {t.exit_px:>9.2f} "
              f"[{t.reason:<6}] net={t.net_ret*100:+.2f}%")
    print("-" * 70)
    print("CAVEATS (read before trusting):")
    print("  • IN-SAMPLE backtest. Walk-forward / out-of-sample + a correction for how")
    print("    many parameter sets you tried (deflated Sharpe) come BEFORE real money.")
    print("  • Costs modelled as spread+slippage only; real fills, halts, gaps differ.")
    print("  • Live US day-trading <$25k equity is capped by the PDT rule (3/5 days).")
    print("  • This is paper/sim. No broker is connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
