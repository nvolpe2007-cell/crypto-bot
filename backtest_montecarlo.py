"""
Monte Carlo over the strategy by block-bootstrapping real OHLCV.

Why block bootstrap rather than GBM:
  Pure geometric Brownian motion strips out the microstructure the strategy
  trades on (volume bursts, momentum runs, mean-reversion bands).  Block
  bootstrap keeps each contiguous chunk's structure intact while randomising
  the order of regimes the strategy sees.  Output: a distribution of P&L,
  PF, and drawdown across many plausible alternative months.

Run:
  python backtest_montecarlo.py --days 7 --paths 30 --block-hours 12
"""
import argparse
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_scientific import fetch_ohlcv, run_backtest, _sim_time  # noqa: E402


def block_bootstrap(df: pd.DataFrame, block_bars: int, target_bars: int,
                    rng: np.random.Generator) -> pd.DataFrame:
    """Stitch random contiguous chunks of `block_bars` from df into a new
    series of length ~`target_bars`. Timestamps are re-stamped to a new
    contiguous range so the regime detector / lead-lag have monotonic time."""
    if len(df) < block_bars + 2:
        raise ValueError(f"not enough bars ({len(df)}) for block size {block_bars}")
    chunks = []
    needed = target_bars
    while needed > 0:
        start = int(rng.integers(0, len(df) - block_bars))
        chunk = df.iloc[start:start + block_bars].copy()
        chunks.append(chunk)
        needed -= len(chunk)
    out = pd.concat(chunks, ignore_index=False).iloc[:target_bars].copy()
    # Re-stamp index to monotonic 1-minute steps anchored at df's start
    tf_seconds = int((df.index[1] - df.index[0]).total_seconds())
    new_index = pd.date_range(start=df.index[0],
                              periods=len(out),
                              freq=f"{tf_seconds}s",
                              tz=df.index.tz)
    out.index = new_index
    return out


def summarise(results: list) -> dict:
    arr = np.array
    rets = arr([(r['final_equity'] - r['capital']) / r['capital'] * 100 for r in results])
    n_trades = arr([len(r['trades']) for r in results])
    wrs = []
    pfs = []
    dds = []
    for r in results:
        ts = r['trades']
        if not ts:
            wrs.append(0); pfs.append(0); dds.append(0); continue
        wins = [t.pnl for t in ts if t.pnl > 0]
        losses = [t.pnl for t in ts if t.pnl <= 0]
        wrs.append(len(wins) / len(ts) * 100)
        pfs.append((sum(wins) / abs(sum(losses))) if losses else float('inf'))
        eq = [r['capital']]
        cur = r['capital']
        for t in ts:
            cur += t.pnl
            eq.append(cur)
        eq_arr = arr(eq)
        peak = np.maximum.accumulate(eq_arr)
        dd = ((peak - eq_arr) / peak * 100).max()
        dds.append(dd)
    return {
        'return_pct':   {'p5': np.percentile(rets,5),  'p50': np.median(rets),  'p95': np.percentile(rets,95),  'mean': rets.mean()},
        'trades':       {'p5': np.percentile(n_trades,5), 'p50': np.median(n_trades), 'p95': np.percentile(n_trades,95), 'mean': n_trades.mean()},
        'win_rate':     {'p5': np.percentile(wrs,5),   'p50': np.median(wrs),   'p95': np.percentile(wrs,95)},
        'profit_factor':{'p5': np.percentile(pfs,5),   'p50': np.median(pfs),   'p95': np.percentile(pfs,95)},
        'max_dd':       {'p5': np.percentile(dds,5),   'p50': np.median(dds),   'p95': np.percentile(dds,95)},
        'win_pct':      (rets > 0).mean() * 100,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7, help='length of each simulated path')
    ap.add_argument('--paths', type=int, default=30, help='number of paths to simulate')
    ap.add_argument('--block-hours', type=int, default=12, help='bootstrap block size')
    ap.add_argument('--seed-days', type=int, default=30, help='real history days to seed from')
    ap.add_argument('--timeframe', type=str, default='1m')
    ap.add_argument('--exchange', type=str, default='binance')
    ap.add_argument('--min-conf', type=float, default=20.0)
    ap.add_argument('--checklist', action='store_true', default=True)
    ap.add_argument('--no-filters', action='store_true')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    # Force the chosen exchange first in the fallback list
    import backtest_scientific as bts
    bts.EXCHANGE_FALLBACK = [args.exchange] + [e for e in bts.EXCHANGE_FALLBACK if e != args.exchange]

    symbols = ['BTC/USD', 'ETH/USD', 'SOL/USD']
    print(f"Seeding from {args.seed_days}d of real {args.timeframe} data on {args.exchange}...")
    seed: dict = {}
    for s in symbols:
        seed[s] = fetch_ohlcv(s, args.timeframe, args.seed_days)

    tf_minutes = {'1m':1,'5m':5,'15m':15,'1h':60}.get(args.timeframe, 1)
    block_bars  = max(50, args.block_hours * 60 // tf_minutes)
    target_bars = args.days * 24 * 60 // tf_minutes
    print(f"  block={block_bars} bars, target={target_bars} bars per path, paths={args.paths}")

    rng = np.random.default_rng(args.seed)
    results = []
    for i in range(args.paths):
        path_data = {s: block_bootstrap(seed[s], block_bars, target_bars, rng) for s in symbols}
        try:
            r = run_backtest(
                path_data,
                min_confidence=args.min_conf,
                use_filters=not args.no_filters,
                use_checklist=args.checklist,
            )
        except Exception as e:
            print(f"  path {i+1:>3}: FAILED {type(e).__name__}: {e}")
            continue
        results.append(r)
        ret = (r['final_equity'] - r['capital']) / r['capital'] * 100
        print(f"  path {i+1:>3}: trades={len(r['trades']):>3}  return={ret:+.2f}%", flush=True)

    if not results:
        print("No successful paths."); return

    # Save per-path summary for later inspection
    rows = []
    for i, r in enumerate(results):
        ts = r['trades']
        wins = [t for t in ts if t.pnl > 0]
        losses = [t for t in ts if t.pnl <= 0]
        rows.append({
            'path': i + 1,
            'trades': len(ts),
            'return_pct': (r['final_equity'] - r['capital']) / r['capital'] * 100,
            'win_rate': len(wins)/len(ts)*100 if ts else 0,
            'profit_factor': (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))) if losses else float('inf'),
        })
    pd.DataFrame(rows).to_csv('data/mc_paths.csv', index=False)
    s = summarise(results)
    print("\n" + "=" * 64)
    print(f"  MONTE CARLO — {len(results)} paths × {args.days}d ({args.timeframe})")
    print("=" * 64)
    print(f"  return %        p5={s['return_pct']['p5']:+.2f}  p50={s['return_pct']['p50']:+.2f}  p95={s['return_pct']['p95']:+.2f}  mean={s['return_pct']['mean']:+.2f}")
    print(f"  trades/path     p5={s['trades']['p5']:.0f}    p50={s['trades']['p50']:.0f}    p95={s['trades']['p95']:.0f}    mean={s['trades']['mean']:.0f}")
    print(f"  win rate %      p5={s['win_rate']['p5']:.0f}   p50={s['win_rate']['p50']:.0f}   p95={s['win_rate']['p95']:.0f}")
    print(f"  profit factor   p5={s['profit_factor']['p5']:.2f}  p50={s['profit_factor']['p50']:.2f}  p95={s['profit_factor']['p95']:.2f}")
    print(f"  max drawdown %  p5={s['max_dd']['p5']:.2f}  p50={s['max_dd']['p50']:.2f}  p95={s['max_dd']['p95']:.2f}")
    print(f"  paths positive: {s['win_pct']:.0f}%")


if __name__ == '__main__':
    main()
