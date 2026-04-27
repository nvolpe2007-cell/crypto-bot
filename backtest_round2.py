"""
Round 2: Deeper analysis
- 2 years of data (captures bull + bear)
- Optimise RSI Pullback parameters
- Test Regime Adaptive strategy (switches based on market conditions)
- Test BTC-only (strongest trend asset)
"""

import asyncio
import sys, os
import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass
from typing import List

sys.path.insert(0, os.path.dirname(__file__))

# ── Reuse fetch from round 1 ──────────────────────────────────────────────────
from backtest_runner import fetch_data, backtest, Sig, print_results, Result


# ── Strategies ────────────────────────────────────────────────────────────────

def rsi_pullback(df, rsi_period=14, oversold=40, recovery=45, ema_trend=200):
    close  = df['close']
    rsi    = ta.rsi(close, length=rsi_period)
    trend  = ta.ema(close, length=ema_trend)
    uptrend   = close > trend
    downtrend = close < trend
    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend   & (rsi.shift(1) < oversold)    & (rsi >= recovery)]        = Sig.BUY
    sig[downtrend & (rsi.shift(1) > (100-oversold)) & (rsi <= (100-recovery))] = Sig.SELL
    return sig


def rsi_pullback_v2(df):
    """Stricter: require EMA50 aligned + volume spike"""
    close  = df['close']
    rsi    = ta.rsi(close, length=14)
    ema50  = ta.ema(close, length=50)
    ema200 = ta.ema(close, length=200)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok = df['volume'] > vol_sma * 1.2

    uptrend = (close > ema50) & (ema50 > ema200)
    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & (rsi.shift(1) < 38) & (rsi >= 45) & vol_ok] = Sig.BUY
    sig[~uptrend & (rsi.shift(1) > 62) & (rsi <= 55) & vol_ok] = Sig.SELL
    return sig


def regime_adaptive(df):
    """
    Regime-switching strategy:
    - ADX > 28: trending → use EMA 9/21 crossover (follow the trend)
    - ADX < 20: ranging → use RSI pullback (mean reversion)
    - 20-28: wait
    Also requires price on correct side of EMA 200.
    """
    close  = df['close']
    fast   = ta.ema(close, length=9)
    slow   = ta.ema(close, length=21)
    ema200 = ta.ema(close, length=200)
    rsi    = ta.rsi(close, length=14)
    adx_df = ta.adx(df['high'], df['low'], close, length=14)
    adx    = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    hist   = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0, index=df.index)
    atr    = ta.atr(df['high'], df['low'], close, length=14)

    trending = adx >= 28
    ranging  = adx < 20

    cross_up   = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    sig = pd.Series(Sig.HOLD, index=df.index)

    # Trend mode: EMA cross + MACD confirm
    sig[trending & cross_up   & (hist > 0) & (close > ema200) & (rsi < 70)] = Sig.BUY
    sig[trending & cross_down & (hist < 0) & (close < ema200) & (rsi > 30)] = Sig.SELL

    # Range mode: RSI pullback
    sig[ranging & (close > ema200) & (rsi.shift(1) < 38) & (rsi >= 45)] = Sig.BUY
    sig[ranging & (close < ema200) & (rsi.shift(1) > 62) & (rsi <= 55)] = Sig.SELL

    return sig


def regime_adaptive_v2(df):
    """
    Enhanced regime strategy with tighter entries:
    - Trending (ADX > 25): Supertrend flip + EMA align
    - Ranging (ADX < 22): RSI divergence from extremes
    - Volume confirmation on all entries
    """
    close  = df['close']
    ema200 = ta.ema(close, length=200)
    ema50  = ta.ema(close, length=50)
    rsi    = ta.rsi(close, length=14)
    adx_df = ta.adx(df['high'], df['low'], close, length=14)
    adx    = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    st = ta.supertrend(df['high'], df['low'], close, length=10, multiplier=3.0)
    if st is not None and not st.empty:
        st_dir = st.iloc[:, 1]
    else:
        st_dir = pd.Series(1, index=df.index)

    trending = adx >= 25
    ranging  = adx < 22

    st_flip_up   = (st_dir == 1)  & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    sig = pd.Series(Sig.HOLD, index=df.index)

    # Trend: Supertrend flip + above EMA200 + volume
    sig[trending & st_flip_up   & (close > ema200) & (rsi < 72) & vol_ok] = Sig.BUY
    sig[trending & st_flip_down & (close < ema200) & (rsi > 28) & vol_ok] = Sig.SELL

    # Range: RSI pullback + EMA50 on correct side + volume
    sig[ranging & (close > ema50) & (rsi.shift(1) < 40) & (rsi >= 46) & vol_ok] = Sig.BUY
    sig[ranging & (close < ema50) & (rsi.shift(1) > 60) & (rsi <= 54) & vol_ok] = Sig.SELL

    return sig


def supertrend_rsi_filter(df):
    """Supertrend with RSI + volume filter (cleaner than raw supertrend)"""
    close  = df['close']
    ema200 = ta.ema(close, length=200)
    rsi    = ta.rsi(close, length=14)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    st = ta.supertrend(df['high'], df['low'], close, length=10, multiplier=2.5)
    if st is None or st.empty:
        return pd.Series(Sig.HOLD, index=df.index)
    st_dir = st.iloc[:, 1]

    st_flip_up   = (st_dir == 1)  & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[st_flip_up   & (close > ema200) & (rsi < 70) & vol_ok] = Sig.BUY
    sig[st_flip_down & (close < ema200) & (rsi > 30) & vol_ok] = Sig.SELL
    return sig


def ichimoku_cloud(df):
    """
    Ichimoku Cloud: price crosses above cloud = buy, below cloud = sell.
    Strong, used by professional traders. Long-term signals only.
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    tenkan  = (high.rolling(9).max()  + low.rolling(9).min())  / 2
    kijun   = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senA    = ((tenkan + kijun) / 2).shift(26)
    senB    = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    top     = pd.concat([senA, senB], axis=1).max(axis=1)
    bottom  = pd.concat([senA, senB], axis=1).min(axis=1)

    rsi = ta.rsi(close, length=14)

    above_cloud = close > top
    below_cloud = close < bottom

    tenkan_cross_up   = (tenkan > kijun) & (tenkan.shift(1) <= kijun.shift(1))
    tenkan_cross_down = (tenkan < kijun) & (tenkan.shift(1) >= kijun.shift(1))

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[above_cloud & tenkan_cross_up   & (rsi < 70)] = Sig.BUY
    sig[below_cloud & tenkan_cross_down & (rsi > 30)] = Sig.SELL
    return sig


STRATEGIES_R2 = {
    'RSI Pullback (baseline)':    lambda df: rsi_pullback(df),
    'RSI Pullback v2 (strict)':   rsi_pullback_v2,
    'RSI Pullback (rsi=35/42)':   lambda df: rsi_pullback(df, oversold=35, recovery=42),
    'RSI Pullback (rsi=45/52)':   lambda df: rsi_pullback(df, oversold=45, recovery=52),
    'Regime Adaptive':            regime_adaptive,
    'Regime Adaptive v2':         regime_adaptive_v2,
    'Supertrend + RSI filter':    supertrend_rsi_filter,
    'Ichimoku Cloud':             ichimoku_cloud,
}

SYMBOLS_R2   = ['BTC/USD', 'ETH/USD', 'SOL/USD']
TIMEFRAME_R2 = '1h'
DAYS_R2      = 730   # 2 full years


async def main():
    print("=" * 60)
    print("  ROUND 2: REGIME + RSI OPTIMISATION")
    print(f"  Timeframe: {TIMEFRAME_R2}  |  Period: {DAYS_R2} days (2 yrs)")
    print(f"  Strategies: {len(STRATEGIES_R2)}  |  Symbols: {len(SYMBOLS_R2)}")
    print("=" * 60)

    data = {}
    for symbol in SYMBOLS_R2:
        try:
            data[symbol] = await fetch_data(symbol, TIMEFRAME_R2, DAYS_R2)
        except Exception as e:
            print(f"  ERROR: {e}")

    if not data:
        print("No data.")
        return

    results = []
    total = len(STRATEGIES_R2) * len(data)
    done  = 0

    for name, fn in STRATEGIES_R2.items():
        for symbol, df in data.items():
            done += 1
            print(f"  [{done}/{total}] {name} on {symbol}...")
            try:
                signals = fn(df.copy())
                result  = backtest(df, signals, symbol, name)
                results.append(result)
            except Exception as e:
                print(f"    ERROR: {e}")

    print_results(results)

    rows = [vars(r) for r in results]
    pd.DataFrame(rows).to_csv('/opt/crypto-bot/data/backtest_round2.csv', index=False)
    print("Saved to /opt/crypto-bot/data/backtest_round2.csv")


if __name__ == '__main__':
    asyncio.run(main())
