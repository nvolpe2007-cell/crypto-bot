"""
Backtest the exact AdvancedStrategy running in the bot right now.
Uses real Kraken 1m data, paginated back as far as Kraken allows.
Walk-forward: first 70% = train, last 30% = test (unseen data).
"""

import asyncio
import sys
import os
from types import ModuleType as _M
if 'numba' not in sys.modules:
    _n = _M('numba'); _n.njit = lambda *a, **k: (a[0] if a and callable(a[0]) else lambda f: f); sys.modules['numba'] = _n

import pandas as pd
import pandas_ta as ta
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


# ── Data fetch ────────────────────────────────────────────────────────────────

async def fetch_kraken_ohlcv(symbol: str, timeframe: str = '5m', days: int = 30) -> pd.DataFrame:
    import ccxt.async_support as ccxt
    from datetime import datetime, timedelta, timezone

    exchange = ccxt.kraken({'enableRateLimit': True})
    await exchange.load_markets()

    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_candles = []
    while True:
        batch = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=720)
        if not batch:
            break
        all_candles.extend(batch)
        print(f"  fetched {len(all_candles):>5} candles...", end='\r')
        if len(batch) < 720:
            break
        since = batch[-1][0] + 1
        if since >= int(datetime.now(timezone.utc).timestamp() * 1000):
            break

    await exchange.close()
    if not all_candles:
        raise ValueError(f"No {timeframe} data from Kraken for {symbol}")

    df = pd.DataFrame(all_candles, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    for col in ['open','high','low','close','volume']:
        df[col] = pd.to_numeric(df[col])
    return df.loc[~df.index.duplicated(keep='last')]


# ── Backtest engine ───────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, symbol: str,
                 capital: float = 1000.0,
                 position_pct: float = 0.5,
                 fee_pct: float = 0.0026,
                 slippage_pct: float = 0.001) -> dict:

    # Signal diagnostics — count how often each condition passes
    def count_condition(series): return int(series.sum())

    # Replicate AdvancedStrategy logic exactly
    close = df['close']
    high  = df['high']
    low   = df['low']

    ema9   = ta.ema(close, length=9)
    ema21  = ta.ema(close, length=21)
    ema50  = ta.ema(close, length=50)
    rsi    = ta.rsi(close, length=14)
    atr    = ta.atr(high, low, close, length=14)
    adx_df = ta.adx(high, low, close, length=14)
    adx    = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(25.0, index=df.index)

    macd_df  = ta.macd(close, fast=12, slow=26, signal=9)
    macd_h   = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0.0, index=df.index)

    vol_sma   = df['volume'].rolling(20).mean()
    vol_ratio = df['volume'] / vol_sma.replace(0, 1)
    # Require any volume present — same as fixed advanced_strategy.py
    vol_ok = vol_ratio > 0

    cross_up   = (ema9 > ema21) & (ema9.shift(1) <= ema21.shift(1))
    cross_down = (ema9 < ema21) & (ema9.shift(1) >= ema21.shift(1))

    buy_cond = (
        cross_up &
        (rsi < 70) &
        vol_ok &
        (adx >= 20)
    )
    sell_cond = (
        cross_down &
        (rsi > 30) &
        vol_ok &
        (adx >= 20)
    )

    # Simulate trades
    equity = capital
    position = None
    trades = []
    warmup = 60

    for i in range(warmup, len(df)):
        price = float(close.iloc[i])
        atr_i = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else price * 0.01

        if position:
            ep, sl, tp, size = position
            exit_price = None
            if price <= sl:
                exit_price = price * (1 - slippage_pct)
                reason = 'SL'
            elif price >= tp:
                exit_price = price * (1 - slippage_pct)
                reason = 'TP'
            elif sell_cond.iloc[i]:
                exit_price = price * (1 - slippage_pct)
                reason = 'Signal'

            if exit_price:
                fee = exit_price * size * fee_pct
                pnl = (exit_price - ep) * size - fee
                equity += pnl
                trades.append({
                    'pnl': pnl,
                    'pnl_pct': (exit_price - ep) / ep * 100,
                    'reason': reason,
                    'won': pnl > 0,
                })
                position = None

        if position is None and buy_cond.iloc[i]:
            entry = price * (1 + slippage_pct)
            sl    = entry - atr_i * 1.5
            tp    = entry + atr_i * 3.0
            size  = (equity * position_pct) / entry
            fee   = entry * size * fee_pct
            equity -= fee
            position = (entry, sl, tp, size)

    # Close any open position at end
    if position:
        ep, sl, tp, size = position
        exit_price = float(close.iloc[-1]) * (1 - slippage_pct)
        fee = exit_price * size * fee_pct
        pnl = (exit_price - ep) * size - fee
        equity += pnl
        trades.append({'pnl': pnl, 'pnl_pct': (exit_price - ep) / ep * 100,
                       'reason': 'EOD', 'won': pnl > 0})

    if not trades:
        n = len(df)
        print(f"    NO TRADES — filter breakdown ({n} candles):")
        print(f"      EMA cross up: {count_condition(cross_up):>4}  RSI<70: {count_condition(rsi<70):>4}  "
              f"MACD>0: {count_condition(macd_h>0):>4}  VolOK: {count_condition(vol_ok):>4}  ADX>=20: {count_condition(adx>=20):>4}")
        print(f"      buy_cond total: {count_condition(buy_cond)}")
        # Show what failed at each crossover candle
        cross_idx = df.index[cross_up.fillna(False)]
        if len(cross_idx) > 0:
            print(f"      Crossover events ({len(cross_idx)}) — why each failed:")
            for idx in cross_idx[:8]:
                loc = df.index.get_loc(idx)
                r = lambda s: bool(s.iloc[loc]) if not pd.isna(s.iloc[loc]) else False
                fails = []
                if not r(rsi < 70):   fails.append(f"RSI={rsi.iloc[loc]:.1f}")
                if not r(macd_h > 0): fails.append(f"MACD={macd_h.iloc[loc]:.4f}")
                if not r(vol_ok):     fails.append("no-vol")
                if not r(adx >= 20):  fails.append(f"ADX={adx.iloc[loc]:.1f}")
                print(f"        {idx.strftime('%H:%M')}  {'PASS' if not fails else 'FAIL: ' + ', '.join(fails)}")
        return {'symbol': symbol, 'trades': 0, 'win_rate': 0, 'return_pct': 0,
                'profit_factor': 0, 'max_dd': 0, 'avg_win_pct': 0, 'avg_loss_pct': 0,
                'sl_exits': 0, 'tp_exits': 0}

    wins   = [t for t in trades if t['won']]
    losses = [t for t in trades if not t['won']]
    gross_win  = sum(t['pnl'] for t in wins)  if wins   else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 1e-9
    pf = gross_win / gross_loss

    # Max drawdown
    eq_curve = [capital]
    running = capital
    for t in trades:
        running += t['pnl']
        eq_curve.append(running)
    peak = capital
    max_dd = 0.0
    for v in eq_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    return {
        'symbol':        symbol,
        'trades':        len(trades),
        'win_rate':      round(len(wins) / len(trades) * 100, 1),
        'return_pct':    round((equity - capital) / capital * 100, 2),
        'profit_factor': round(pf, 2),
        'max_dd':        round(max_dd, 1),
        'avg_win_pct':   round(np.mean([t['pnl_pct'] for t in wins])  if wins   else 0, 2),
        'avg_loss_pct':  round(np.mean([t['pnl_pct'] for t in losses]) if losses else 0, 2),
        'sl_exits':      sum(1 for t in trades if t['reason'] == 'SL'),
        'tp_exits':      sum(1 for t in trades if t['reason'] == 'TP'),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    symbols   = ['BTC/USD', 'ETH/USD']
    timeframe = '1m'   # Test on actual bot timeframe — limited to ~12h but shows real signal rate

    print("=" * 70)
    print(f"  LIVE STRATEGY BACKTEST  (AdvancedStrategy  {timeframe} candles)")
    print("  Data: Kraken — as far back as the API allows per timeframe")
    print("=" * 70)

    for symbol in symbols:
        print(f"\nFetching {symbol} {timeframe}...")
        try:
            df = await fetch_kraken_ohlcv(symbol, timeframe, days=30)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue

        tf_mins = {'1m':1,'5m':5,'15m':15,'1h':60,'4h':240,'1d':1440}
        candle_hours = len(df) * tf_mins.get(timeframe, 60) / 60
        print(f"  Got {len(df)} candles ({candle_hours:.0f} hours)  {df.index[0].strftime('%Y-%m-%d %H:%M')} to {df.index[-1].strftime('%Y-%m-%d %H:%M')}")

        split = int(len(df) * 0.70)
        df_train = df.iloc[:split]
        df_test  = df.iloc[split:]

        print(f"\n  {'Period':<12} {'Trades':>7} {'WinRate':>8} {'Return':>9} {'PF':>7} {'MaxDD':>8} {'AvgW':>7} {'AvgL':>7}")
        print(f"  {'-'*68}")

        for label, subset in [('TRAIN 70%', df_train), ('TEST  30%', df_test)]:
            if len(subset) < 200:
                print(f"  {label:<12} not enough data")
                continue
            r = run_backtest(subset, symbol)
            marker = ' <-- REAL EDGE' if r['profit_factor'] > 1.3 and r['trades'] >= 10 else ''
            print(f"  {label:<12} {r['trades']:>7} {r['win_rate']:>7.1f}% {r['return_pct']:>8.1f}% "
                  f"{r['profit_factor']:>6.2f}x {r['max_dd']:>7.1f}% "
                  f"{r['avg_win_pct']:>6.2f}% {r['avg_loss_pct']:>6.2f}%{marker}")

        # Full period
        r_full = run_backtest(df, symbol)
        print(f"  {'FULL'::<12} {r_full['trades']:>7} {r_full['win_rate']:>7.1f}% "
              f"{r_full['return_pct']:>8.1f}% {r_full['profit_factor']:>6.2f}x "
              f"{r_full['max_dd']:>7.1f}%")
        print(f"  SL exits: {r_full['sl_exits']}   TP exits: {r_full['tp_exits']}")

    print("\n" + "=" * 70)
    print("  Profit Factor > 1.0 = strategy makes money on average")
    print("  Profit Factor < 1.0 = strategy loses money on average")
    print("  Need >= 20 trades for statistical significance")
    print("=" * 70)


if __name__ == '__main__':
    asyncio.run(main())
