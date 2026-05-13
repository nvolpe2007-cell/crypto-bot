"""
Round 6: Find strategies that actually generate enough trades.
Testing classic approaches known to work in trending markets.
Each needs 20+ trades/year to be valid.

Walk-forward: Train 2023-2024, Validate 2025-2026.
"""

import asyncio
import sys, os
from types import ModuleType as _M
if 'numba' not in sys.modules:
    _n = _M('numba'); _n.njit = lambda *a, **k: (a[0] if a and callable(a[0]) else lambda f: f); sys.modules['numba'] = _n
import pandas as pd
import pandas_ta as ta
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from backtest_runner import fetch_data, Sig, print_results, Result
from backtest_round4 import backtest_advanced


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIES THAT GENERATE MORE SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def donchian_breakout(df, btc_daily=None, period=20, exit_period=10, vol_mult=1.2):
    """
    Turtle-style Donchian breakout.
    Buy when price breaks above N-period high with volume surge.
    Exit when price drops below M-period low.
    + Optional BTC macro filter.
    """
    close  = df['close']
    high   = df['high']
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * vol_mult

    highest = close.rolling(period).max().shift(1)
    lowest  = close.rolling(exit_period).min().shift(1)

    breakout_up   = close > highest
    breakdown     = close < lowest

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[breakout_up & vol_ok] = Sig.BUY
    sig[breakdown]            = Sig.SELL

    if btc_daily is not None:
        btc_ema = ta.ema(btc_daily['close'], length=200)
        btc_ok  = (btc_daily['close'] > btc_ema).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_ok)] = Sig.HOLD

    return sig


def momentum_surge(df, btc_daily=None, lookback=10, surge_pct=3.0, vol_mult=1.5):
    """
    Momentum surge: when price rises X% in lookback candles with a volume surge,
    the trend is strong — ride it.
    """
    close  = df['close']
    returns = close.pct_change(lookback) * 100
    ema50   = ta.ema(close, length=50)
    rsi     = ta.rsi(close, length=14)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * vol_mult

    surge  = returns > surge_pct
    above  = close > ema50

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[surge & above & vol_ok & (rsi < 80)]  = Sig.BUY
    sig[(returns < -surge_pct) & vol_ok]       = Sig.SELL

    if btc_daily is not None:
        btc_ema = ta.ema(btc_daily['close'], length=200)
        btc_ok  = (btc_daily['close'] > btc_ema).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_ok)] = Sig.HOLD

    return sig


def rsi_divergence_simple(df, btc_daily=None):
    """
    RSI momentum alignment: buy when RSI crosses above 50 from below in uptrend.
    Simple, generates reasonable signal count.
    """
    close  = df['close']
    rsi    = ta.rsi(close, length=14)
    ema50  = ta.ema(close, length=50)
    ema200 = ta.ema(close, length=200)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    rsi_cross_up   = (rsi > 50) & (rsi.shift(1) <= 50)
    rsi_cross_down = (rsi < 50) & (rsi.shift(1) >= 50)
    uptrend   = close > ema200
    downtrend = close < ema200

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[rsi_cross_up   & uptrend   & vol_ok] = Sig.BUY
    sig[rsi_cross_down & downtrend & vol_ok] = Sig.SELL

    if btc_daily is not None:
        btc_ema = ta.ema(btc_daily['close'], length=200)
        btc_ok  = (btc_daily['close'] > btc_ema).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_ok)] = Sig.HOLD

    return sig


def ema_pullback_to_ema(df, btc_daily=None, fast=21, slow=55, pullback_ema=21):
    """
    In an uptrend (fast > slow), buy when price pulls back to touch the fast EMA
    then bounces. Exit when fast crosses below slow.
    """
    close  = df['close']
    emaf   = ta.ema(close, length=fast)
    emas   = ta.ema(close, length=slow)
    rsi    = ta.rsi(close, length=14)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    uptrend = emaf > emas
    # Price touches EMA from above and bounces: low touched EMA area
    touched_ema = (df['low'] <= emaf * 1.005) & (close > emaf)
    prev_above  = close.shift(1) > emaf.shift(1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & touched_ema & prev_above & (rsi > 40) & (rsi < 65) & vol_ok] = Sig.BUY
    sig[~uptrend & (close < emaf)] = Sig.SELL

    if btc_daily is not None:
        btc_ema = ta.ema(btc_daily['close'], length=200)
        btc_ok  = (btc_daily['close'] > btc_ema).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_ok)] = Sig.HOLD

    return sig


def three_bar_pattern(df, btc_daily=None):
    """
    3-bar momentum pattern:
    - 3 consecutive closes in same direction
    - Each bar closes higher/lower than previous
    - Volume increasing
    - In direction of 50 EMA trend
    """
    close  = df['close']
    ema50  = ta.ema(close, length=50)
    ema200 = ta.ema(close, length=200)
    vol    = df['volume']

    c1, c2, c3 = close.shift(2), close.shift(1), close
    v1, v2, v3 = vol.shift(2), vol.shift(1), vol

    three_up   = (c3 > c2) & (c2 > c1) & (v3 > v2) & (v2 > v1) & (close > ema50)
    three_down = (c3 < c2) & (c2 < c1) & (v3 > v2) & (v2 > v1) & (close < ema50)

    rsi = ta.rsi(close, length=14)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[three_up   & (close > ema200) & (rsi < 75)] = Sig.BUY
    sig[three_down & (close < ema200) & (rsi > 25)] = Sig.SELL

    if btc_daily is not None:
        btc_ema = ta.ema(btc_daily['close'], length=200)
        btc_ok  = (btc_daily['close'] > btc_ema).reindex(df.index, method='ffill').fillna(False)
        sig[(sig == Sig.BUY) & (~btc_ok)] = Sig.HOLD

    return sig


def combined_best(df, btc_daily=None):
    """
    Combination of Donchian breakout OR RSI 50 cross, with BTC filter.
    Union of signals from two approaches — more signals, still filtered.
    """
    s1 = donchian_breakout(df, btc_daily, period=15, exit_period=8, vol_mult=1.2)
    s2 = rsi_divergence_simple(df, btc_daily)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[(s1 == Sig.BUY) | (s2 == Sig.BUY)]   = Sig.BUY
    sig[(s1 == Sig.SELL) | (s2 == Sig.SELL)]  = Sig.SELL
    return sig


STRATEGIES = {
    'Donchian 20/10':           lambda d, b: donchian_breakout(d, b, 20, 10),
    'Donchian 15/8':            lambda d, b: donchian_breakout(d, b, 15, 8),
    'Donchian 20/10 no BTC':    lambda d, _: donchian_breakout(d, None, 20, 10),
    'Momentum Surge':           lambda d, b: momentum_surge(d, b),
    'RSI 50 Cross':             lambda d, b: rsi_divergence_simple(d, b),
    'RSI 50 Cross no BTC':      lambda d, _: rsi_divergence_simple(d, None),
    'EMA Pullback 21/55':       lambda d, b: ema_pullback_to_ema(d, b),
    '3-Bar Pattern':            lambda d, b: three_bar_pattern(d, b),
    'Combined (Donchian+RSI)':  lambda d, b: combined_best(d, b),
}

SYMBOLS    = ['BTC/USD', 'ETH/USD']
TRAIN_END  = '2024-12-31'
TEST_START = '2025-01-01'


async def main():
    print("=" * 65)
    print("  ROUND 6: BREAKOUT + MOMENTUM STRATEGIES (daily candles)")
    print("=" * 65)

    print("\nFetching data...")
    dfs, btc_daily = {}, None
    for sym in SYMBOLS:
        dfs[sym] = await fetch_data(sym, '1d', 720)
    btc_daily = dfs.get('BTC/USD', await fetch_data('BTC/USD', '1d', 720))

    all_results = []

    for label, (start, end) in [('TRAIN 2023-24', (None, TRAIN_END)),
                                  ('TEST  2025-26', (TEST_START, None))]:
        print(f"\n{'='*65}")
        print(f"  {label}")
        print(f"{'='*65}")

        results = []
        for strat_name, fn in STRATEGIES.items():
            for symbol in SYMBOLS:
                df_full = dfs[symbol]
                df = df_full.copy()
                if start: df = df[df.index >= start]
                if end:   df = df[df.index <= end]

                btc = btc_daily.copy()
                if start: btc = btc[btc.index >= start]
                if end:   btc = btc[btc.index <= end]

                if len(df) < 200:
                    continue
                try:
                    sigs = fn(df.copy(), btc.copy())
                    res  = backtest_advanced(df, sigs, symbol, strat_name,
                                             atr_sl_mult=1.5, atr_tp_mult=2.5,
                                             timeframe='1d')
                    results.append(res)
                    all_results.append((label, res))
                except Exception as e:
                    print(f"  ERROR {strat_name} {symbol}: {e}")

        print_results(results)

    # Summary: which strategies are positive on BOTH train AND test?
    print("=" * 65)
    print("  CONSISTENT ACROSS TRAIN + TEST (the real winners)")
    print("=" * 65)

    train_map = {(r.name, r.symbol): r for lbl, r in all_results if 'TRAIN' in lbl}
    test_map  = {(r.name, r.symbol): r for lbl, r in all_results if 'TEST'  in lbl}

    consistent = []
    for key in train_map:
        tr = train_map[key]
        te = test_map.get(key)
        if te and tr.profit_factor > 1.0 and te.profit_factor > 1.0 and tr.trades >= 8 and te.trades >= 4:
            consistent.append((key, tr, te))

    if consistent:
        print(f"\n  {'Strategy':<30} {'Symbol':<10} | {'Train PF':>8} {'Train Ret%':>10} {'Train T':>7} | {'Test PF':>8} {'Test Ret%':>9} {'Test T':>6}")
        print(f"  {'-'*90}")
        consistent.sort(key=lambda x: x[1].profit_factor + x[2].profit_factor, reverse=True)
        for (name, sym), tr, te in consistent:
            print(f"  {name:<30} {sym:<10} | {tr.profit_factor:>8.2f} {tr.total_return_pct:>9.1f}% {tr.trades:>7} | "
                  f"{te.profit_factor:>8.2f} {te.total_return_pct:>8.1f}% {te.trades:>6}")
        print(f"\n  WINNER: {consistent[0][0][0]} on {consistent[0][0][1]}")
    else:
        print("  No strategy was positive on both train AND test data.")
        print("  Best on test data:")
        test_only = [(r.name, r.symbol, r) for lbl, r in all_results
                     if 'TEST' in lbl and r.trades >= 4]
        test_only.sort(key=lambda x: x[2].profit_factor, reverse=True)
        for name, sym, r in test_only[:5]:
            print(f"    {name:<30} {sym:<10} PF={r.profit_factor:.2f}  Ret={r.total_return_pct:+.1f}%  T={r.trades}")

    out_path = os.path.join(os.path.dirname(__file__), 'data', 'backtest_round6.csv')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pd.DataFrame([vars(r) for _, r in all_results]).to_csv(out_path, index=False)
    print(f"\nSaved to {out_path}")


if __name__ == '__main__':
    asyncio.run(main())
