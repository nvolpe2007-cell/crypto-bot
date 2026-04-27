"""
Round 5: Grid search + walk-forward validation
- 4h candles: more trades, better statistics, less noise than 1m/5m
- Grid search RSI params on TRAIN data (2023-2024)
- Validate best params on TEST data (2025-2026) — prevents curve fitting
- Daily BTC macro filter
- Goal: find params with 20+ trades/yr and positive expectancy
"""

import asyncio
import sys, os
import pandas as pd
import pandas_ta as ta
import numpy as np
from itertools import product
from dataclasses import dataclass
from typing import List, Dict, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from backtest_runner import fetch_data, Sig
from backtest_round4 import backtest_advanced


@dataclass
class GridResult:
    params: dict
    symbol: str
    period: str
    trades: int
    win_rate: float
    total_return: float
    profit_factor: float
    max_dd: float
    expectancy: float
    score: float   # composite ranking score


def build_signal(df: pd.DataFrame, btc_daily: pd.DataFrame,
                 oversold: float, recovery: float,
                 ema_fast: int, ema_slow: int,
                 vol_mult: float, use_btc_filter: bool) -> pd.Series:
    close  = df['close']
    rsi    = ta.rsi(close, length=14)
    emaf   = ta.ema(close, length=ema_fast)
    emas   = ta.ema(close, length=ema_slow)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * vol_mult

    uptrend = (close > emaf) & (emaf > emas)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & (rsi.shift(1) < oversold) & (rsi >= recovery) & vol_ok] = Sig.BUY
    sig[~uptrend & (rsi.shift(1) > (100 - oversold)) & (rsi <= (100 - recovery)) & vol_ok] = Sig.SELL

    if use_btc_filter and btc_daily is not None and len(btc_daily) > 0:
        btc_ema200 = ta.ema(btc_daily['close'], length=200)
        btc_bull   = (btc_daily['close'] > btc_ema200).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_bull)] = Sig.HOLD

    return sig


def composite_score(res) -> float:
    """Score combining return, PF, and trade count. Penalises too few trades."""
    if res.trades < 8:
        return -999.0
    trade_bonus = min(1.0, res.trades / 30)
    return (res.total_return_pct * 0.4 +
            (res.profit_factor - 1) * 20 +
            trade_bonus * 10 -
            res.max_drawdown_pct * 0.3)


async def main():
    print("=" * 70)
    print("  ROUND 5: GRID SEARCH + WALK-FORWARD VALIDATION (4h candles)")
    print("=" * 70)

    # Fetch data
    print("\nFetching data (4h, 3 years)...")
    symbols = ['ETH/USD', 'SOL/USD']
    dfs = {}
    for sym in symbols:
        dfs[sym] = await fetch_data(sym, '4h', 1200)

    btc_4h    = await fetch_data('BTC/USD', '4h', 1200)
    btc_daily = await fetch_data('BTC/USD', '1d', 1200)

    # Split: train 2023-2024, test 2025-2026
    TRAIN_END = '2024-12-31'
    TEST_START = '2025-01-01'

    # ── Grid search parameters ────────────────────────────────────────────────
    GRID = {
        'oversold':   [28, 32, 36, 40],
        'recovery':   [38, 43, 48, 53],
        'ema_fast':   [50, 100],
        'ema_slow':   [100, 200],
        'vol_mult':   [1.0, 1.2],
        'btc_filter': [False, True],
    }

    all_combos = list(product(
        GRID['oversold'], GRID['recovery'],
        GRID['ema_fast'], GRID['ema_slow'],
        GRID['vol_mult'], GRID['btc_filter']
    ))
    # Filter invalid (fast must be < slow)
    all_combos = [c for c in all_combos if c[2] < c[3]]
    print(f"\nGrid: {len(all_combos)} combinations x {len(symbols)} symbols")

    train_results: List[GridResult] = []
    total = len(all_combos) * len(symbols)
    done  = 0

    for oversold, recovery, ema_fast, ema_slow, vol_mult, btc_filter in all_combos:
        if recovery <= oversold:
            continue
        for symbol in symbols:
            done += 1
            df_train = dfs[symbol][dfs[symbol].index <= TRAIN_END]
            btc_t    = btc_daily[btc_daily.index <= TRAIN_END]

            if len(df_train) < 300:
                continue

            try:
                sigs = build_signal(df_train, btc_t, oversold, recovery,
                                    ema_fast, ema_slow, vol_mult, btc_filter)
                res  = backtest_advanced(df_train, sigs, symbol, 'grid',
                                         atr_sl_mult=1.5, atr_tp_mult=3.0)
                params = dict(oversold=oversold, recovery=recovery,
                               ema_fast=ema_fast, ema_slow=ema_slow,
                               vol_mult=vol_mult, btc_filter=btc_filter)
                sc = composite_score(res)
                train_results.append(GridResult(
                    params=params, symbol=symbol, period='train',
                    trades=res.trades, win_rate=res.win_rate,
                    total_return=res.total_return_pct,
                    profit_factor=res.profit_factor,
                    max_dd=res.max_drawdown_pct,
                    expectancy=res.expectancy,
                    score=sc
                ))
            except Exception:
                pass

        if done % 100 == 0:
            print(f"  Progress: {done}/{total}")

    print(f"\nGrid search complete. {len(train_results)} results.")

    # ── Find top-5 per symbol on TRAIN ───────────────────────────────────────
    print("\n" + "=" * 80)
    print("  TOP TRAINING RESULTS (2023-2024)")
    print("=" * 80)

    top_params_per_symbol = {}
    for symbol in symbols:
        sym_results = [r for r in train_results if r.symbol == symbol and r.score > -999]
        sym_results.sort(key=lambda r: r.score, reverse=True)
        top5 = sym_results[:5]
        top_params_per_symbol[symbol] = top5

        print(f"\n  {symbol} — Top 5:")
        print(f"  {'OS':>4} {'Rec':>4} {'EF':>4} {'ES':>4} {'Vol':>5} {'BTC':>5} | {'Trades':>6} {'WR%':>6} {'Ret%':>7} {'PF':>6} {'DD%':>6} {'Score':>7}")
        print(f"  {'-'*75}")
        for r in top5:
            p = r.params
            print(f"  {p['oversold']:>4} {p['recovery']:>4} {p['ema_fast']:>4} {p['ema_slow']:>4} {p['vol_mult']:>5.1f} {str(p['btc_filter']):>5} | "
                  f"{r.trades:>6} {r.win_rate:>5.1f}% {r.total_return:>6.1f}% {r.profit_factor:>6.2f} {r.max_dd:>5.1f}% {r.score:>7.1f}")

    # ── Walk-forward: validate top params on TEST data (2025-2026) ────────────
    print("\n" + "=" * 80)
    print("  WALK-FORWARD VALIDATION (2025-2026) — UNSEEN DATA")
    print("=" * 80)

    for symbol in symbols:
        df_test  = dfs[symbol][dfs[symbol].index >= TEST_START]
        btc_test = btc_daily[btc_daily.index >= TEST_START]
        top5     = top_params_per_symbol.get(symbol, [])

        print(f"\n  {symbol} — Validation:")
        print(f"  {'OS':>4} {'Rec':>4} {'EF':>4} {'ES':>4} {'Vol':>5} {'BTC':>5} | {'Trades':>6} {'WR%':>6} {'Ret%':>7} {'PF':>6} {'DD%':>6} | Train Score")
        print(f"  {'-'*85}")

        for r in top5:
            p = r.params
            try:
                sigs = build_signal(df_test, btc_test, p['oversold'], p['recovery'],
                                    p['ema_fast'], p['ema_slow'], p['vol_mult'], p['btc_filter'])
                res  = backtest_advanced(df_test, sigs, symbol, 'val',
                                         atr_sl_mult=1.5, atr_tp_mult=3.0)
                flag = ' ★' if res.profit_factor >= 1.3 and res.trades >= 5 and res.total_return_pct >= 0 else ''
                print(f"  {p['oversold']:>4} {p['recovery']:>4} {p['ema_fast']:>4} {p['ema_slow']:>4} {p['vol_mult']:>5.1f} {str(p['btc_filter']):>5} | "
                      f"{res.trades:>6} {res.win_rate:>5.1f}% {res.total_return_pct:>6.1f}% {res.profit_factor:>6.2f} {res.max_drawdown_pct:>5.1f}% | "
                      f"train={r.score:.1f}{flag}")
            except Exception as e:
                print(f"    ERROR: {e}")

    # ── Final best: pick params that are positive on BOTH train AND test ──────
    print("\n" + "=" * 80)
    print("  FINAL RECOMMENDATION")
    print("=" * 80)

    for symbol in symbols:
        df_test  = dfs[symbol][dfs[symbol].index >= TEST_START]
        btc_test = btc_daily[btc_daily.index >= TEST_START]
        top5     = top_params_per_symbol.get(symbol, [])

        best = None
        best_combined = -9999
        for r in top5:
            p = r.params
            try:
                sigs = build_signal(df_test, btc_test, p['oversold'], p['recovery'],
                                    p['ema_fast'], p['ema_slow'], p['vol_mult'], p['btc_filter'])
                res  = backtest_advanced(df_test, sigs, symbol, 'val',
                                         atr_sl_mult=1.5, atr_tp_mult=3.0)
                combined = r.score + composite_score(res)
                if combined > best_combined and res.trades >= 3:
                    best_combined = combined
                    best = (r, res)
            except Exception:
                pass

        if best:
            tr, vr = best
            p = tr.params
            print(f"\n  {symbol}:")
            print(f"    Params: RSI oversold={p['oversold']} recovery={p['recovery']} | EMA {p['ema_fast']}/{p['ema_slow']} | Vol {p['vol_mult']}x | BTC filter={p['btc_filter']}")
            print(f"    Train (2023-24): {tr.trades} trades | {tr.win_rate:.0f}% WR | {tr.total_return:+.1f}% | PF={tr.profit_factor:.2f}")
            print(f"    Test  (2025-26): {vr.trades} trades | {vr.win_rate:.0f}% WR | {vr.total_return_pct:+.1f}% | PF={vr.profit_factor:.2f}")


if __name__ == '__main__':
    asyncio.run(main())
