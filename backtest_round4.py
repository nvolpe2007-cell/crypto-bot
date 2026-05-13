"""
Round 4: Bull market test + BTC macro filter + trailing stop
- Test on 2023 bull run (BTC $16k → $44k)
- Test on 2024 bull run (BTC $40k → $100k)
- Test on 2025-2026 bear (current)
- Add BTC macro filter: only trade when BTC above 200 EMA daily
- Compare trailing stop vs fixed ATR stop
"""

import asyncio
import sys, os
import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass
from typing import List, Dict

sys.path.insert(0, os.path.dirname(__file__))
from backtest_runner import fetch_data, Sig, Result, print_results


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST ENGINE — supports trailing stop
# ─────────────────────────────────────────────────────────────────────────────

def backtest_advanced(df: pd.DataFrame, signals: pd.Series,
                      symbol: str, name: str,
                      capital: float = 1000.0,
                      position_pct: float = 0.50,
                      fee_pct: float = 0.0026,
                      slippage_pct: float = 0.001,
                      atr_sl_mult: float = 1.5,
                      atr_tp_mult: float = 3.0,
                      use_trailing: bool = False,
                      trail_atr_mult: float = 1.0,
                      timeframe: str = '1h') -> Result:

    atr = ta.atr(df['high'], df['low'], df['close'], length=14)
    equity = capital
    position = None   # (entry, sl, tp, size, highest_price)
    trades, equity_curve = [], [capital]
    warmup = 210

    for i in range(warmup, len(df)):
        price  = df['close'].iloc[i]
        sig    = signals.iloc[i]
        atr_i  = atr.iloc[i] if not pd.isna(atr.iloc[i]) else price * 0.01

        if position:
            ep, sl, tp, size, high_price = position

            # Update trailing stop
            if use_trailing and price > high_price:
                high_price = price
                new_sl = high_price - atr_i * trail_atr_mult
                sl = max(sl, new_sl)
                position = (ep, sl, tp, size, high_price)

            # Exit conditions
            exit_price = None
            if price <= sl:
                exit_price = price * (1 - slippage_pct)
            elif price >= tp:
                exit_price = price * (1 - slippage_pct)
            elif sig == Sig.SELL:
                exit_price = price * (1 - slippage_pct)

            if exit_price:
                fee = exit_price * size * fee_pct
                pnl = (exit_price - ep) * size - fee
                equity += pnl
                trades.append({'pnl': pnl, 'pnl_pct': (exit_price - ep) / ep * 100})
                position = None

        if position is None and sig == Sig.BUY:
            entry = price * (1 + slippage_pct)
            sl    = entry - atr_i * atr_sl_mult
            tp    = entry + atr_i * atr_tp_mult
            trade_usd = equity * position_pct
            size  = trade_usd / entry
            fee   = entry * size * fee_pct
            equity -= fee
            position = (entry, sl, tp, size, entry)

        equity_curve.append(equity)

    if position:
        ep, sl, tp, size, _ = position
        price = df['close'].iloc[-1]
        exit_p = price * (1 - slippage_pct)
        fee = exit_p * size * fee_pct
        pnl = (exit_p - ep) * size - fee
        equity += pnl
        trades.append({'pnl': pnl, 'pnl_pct': (exit_p - ep) / ep * 100})
        equity_curve.append(equity)

    if not trades:
        return Result(name, symbol, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / len(trades) * 100
    total_return = (equity - capital) / capital * 100
    gross_win  = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else 0
    avg_win_pct  = np.mean([t['pnl_pct'] for t in wins])   if wins   else 0
    avg_loss_pct = np.mean([t['pnl_pct'] for t in losses]) if losses else 0
    expectancy   = np.mean([t['pnl'] for t in trades])

    eq = pd.Series(equity_curve)
    running_max = eq.expanding().max()
    drawdown = (eq - running_max) / running_max * 100
    max_dd = abs(drawdown.min())
    bars_per_year = {
        '1m': 365 * 24 * 60, '3m': 365 * 24 * 20, '5m': 365 * 24 * 12,
        '15m': 365 * 24 * 4, '30m': 365 * 24 * 2,
        '1h': 365 * 24, '2h': 365 * 12, '4h': 365 * 6,
        '6h': 365 * 4, '8h': 365 * 3, '12h': 365 * 2,
        '1d': 365, '3d': 365 / 3, '1w': 52,
    }.get(timeframe, 365 * 24)
    returns = eq.pct_change().dropna()
    sharpe  = (returns.mean() / returns.std()) * np.sqrt(bars_per_year) if returns.std() > 0 else 0

    return Result(name, symbol, len(trades), round(win_rate, 1),
                  round(total_return, 2), round(profit_factor, 2),
                  round(max_dd, 2), round(sharpe, 2),
                  round(avg_win_pct, 2), round(avg_loss_pct, 2),
                  round(expectancy, 4))


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

def rsi_pullback_base(df):
    close  = df['close']
    rsi    = ta.rsi(close, length=14)
    ema100 = ta.ema(close, length=100)
    ema200 = ta.ema(close, length=200)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1
    uptrend = (close > ema100) & (ema100 > ema200)
    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & (rsi.shift(1) < 36) & (rsi >= 43) & vol_ok] = Sig.BUY
    sig[~uptrend & (rsi.shift(1) > 64) & (rsi <= 57) & vol_ok] = Sig.SELL
    return sig


def rsi_pullback_with_btc_filter(df, btc_df):
    """
    Same RSI Pullback but only trade when BTC is above its 200-period EMA.
    This acts as a macro bull/bear filter.
    """
    # Align BTC data index to this df's index
    btc_ema200 = ta.ema(btc_df['close'], length=200)
    btc_above  = btc_df['close'] > btc_ema200

    # Reindex to match target df's timestamps
    btc_aligned = btc_above.reindex(df.index, method='ffill').fillna(False)

    base = rsi_pullback_base(df)
    # Kill any buy signal when BTC macro is bearish
    result = base.copy()
    result[(base == Sig.BUY) & (~btc_aligned)] = Sig.HOLD
    return result


def production_strategy(df):
    close  = df['close']
    high   = df['high']
    low    = df['low']
    ema100 = ta.ema(close, length=100)
    ema200 = ta.ema(close, length=200)
    rsi    = ta.rsi(close, length=14)
    adx_df = ta.adx(high, low, close, length=14)
    adx    = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    hist   = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0, index=df.index)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.15
    st = ta.supertrend(high, low, close, length=10, multiplier=2.5)
    st_dir = st.iloc[:, 1] if st is not None and not st.empty else pd.Series(1, index=df.index)

    trending = adx >= 27
    ranging  = adx < 21
    macro_up = close > ema200
    micro_up = close > ema100

    st_flip_up   = (st_dir == 1)  & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[trending & st_flip_up   & macro_up  & (hist > 0) & (rsi < 72) & vol_ok] = Sig.BUY
    sig[trending & st_flip_down & ~macro_up & (hist < 0) & (rsi > 28) & vol_ok] = Sig.SELL
    sig[ranging & micro_up  & macro_up  & (rsi.shift(1) < 36) & (rsi >= 43) & vol_ok] = Sig.BUY
    sig[ranging & ~micro_up & ~macro_up & (rsi.shift(1) > 64) & (rsi <= 57) & vol_ok] = Sig.SELL
    return sig


def production_with_btc_filter(df, btc_df):
    btc_ema200  = ta.ema(btc_df['close'], length=200)
    btc_aligned = (btc_df['close'] > btc_ema200).reindex(df.index, method='ffill').fillna(False)
    base = production_strategy(df)
    result = base.copy()
    result[(base == Sig.BUY) & (~btc_aligned)] = Sig.HOLD
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PERIODS
# ─────────────────────────────────────────────────────────────────────────────

PERIODS = {
    '2023 Bull':     ('2023-01-01', '2023-12-31', 365),
    '2024 Bull':     ('2024-01-01', '2024-11-30', 335),
    '2025-26 Bear':  ('2025-01-01', '2026-04-27', 482),
    'Full 3yr':      ('2023-01-01', '2026-04-27', 1182),
}

SYMBOLS = ['ETH/USD', 'SOL/USD']


async def fetch_period(symbol, days):
    return await fetch_data(symbol, '1h', days)


async def main():
    print("=" * 65)
    print("  ROUND 4: BULL vs BEAR + BTC MACRO FILTER + TRAILING STOP")
    print("=" * 65)

    # Fetch BTC (for macro filter) — full 3yr
    print("\nFetching BTC/USD (macro filter)...")
    btc_df = await fetch_data('BTC/USD', '1h', 1200)

    # Fetch target symbols — full 3yr
    dfs = {}
    for sym in SYMBOLS:
        print(f"Fetching {sym} (full 3yr)...")
        dfs[sym] = await fetch_data(sym, '1h', 1200)

    results = []

    for period_name, (start, end, _) in PERIODS.items():
        print(f"\n  Period: {period_name} ({start} → {end})")

        for symbol in SYMBOLS:
            df_full  = dfs[symbol]
            btc_full = btc_df

            # Slice to period
            df  = df_full[df_full.index >= start]
            df  = df[df.index <= end]
            btc = btc_full[btc_full.index >= start]
            btc = btc[btc.index <= end]

            if len(df) < 300:
                print(f"    Not enough data for {symbol} in {period_name}")
                continue

            configs = [
                ('RSI Pullback',             lambda d, b: rsi_pullback_base(d),               False),
                ('RSI + BTC filter',         lambda d, b: rsi_pullback_with_btc_filter(d, b), False),
                ('RSI + trailing stop',      lambda d, b: rsi_pullback_base(d),               True),
                ('Production Strat',         lambda d, b: production_strategy(d),             False),
                ('Production + BTC filter',  lambda d, b: production_with_btc_filter(d, b),  False),
                ('Production + trailing',    lambda d, b: production_strategy(d),             True),
            ]

            for cfg_name, sig_fn, trailing in configs:
                label = f"{cfg_name} [{period_name}]"
                try:
                    sigs = sig_fn(df.copy(), btc.copy())
                    res  = backtest_advanced(df, sigs, symbol, label,
                                             use_trailing=trailing,
                                             trail_atr_mult=1.0)
                    results.append(res)
                except Exception as e:
                    print(f"    ERROR {cfg_name} {symbol}: {e}")

    # Print grouped by period
    for period_name in PERIODS:
        period_results = [r for r in results if period_name in r.name]
        if period_results:
            print(f"\n{'='*100}")
            print(f"  {period_name}")
            print(f"{'='*100}")
            print_results(period_results)

    # Overall summary
    rows = [vars(r) for r in results]
    pd.DataFrame(rows).to_csv('/opt/crypto-bot/data/backtest_round4.csv', index=False)
    print("\nSaved to /opt/crypto-bot/data/backtest_round4.csv")


if __name__ == '__main__':
    asyncio.run(main())
