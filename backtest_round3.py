"""
Round 3: 5-minute candles — the best strategies from rounds 1+2
tested on the timeframe closest to live trading.
Also tests the final "Production Strategy" candidate.
"""

import asyncio
import sys, os
import pandas as pd
import pandas_ta as ta
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from backtest_runner import fetch_data, backtest, Sig, print_results


def rsi_pullback_final(df):
    """
    Best from round 2: RSI 35/42, EMA200 trend filter, volume confirm.
    Adapted for 5m (uses EMA100 as trend on 5m = ~8h trend context).
    """
    close   = df['close']
    rsi     = ta.rsi(close, length=14)
    ema100  = ta.ema(close, length=100)   # ~8h on 5m = medium-term trend
    ema200  = ta.ema(close, length=200)   # ~17h on 5m = longer trend
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    uptrend = (close > ema100) & (ema100 > ema200)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & (rsi.shift(1) < 35) & (rsi >= 42) & vol_ok] = Sig.BUY
    sig[~uptrend & (rsi.shift(1) > 65) & (rsi <= 58) & vol_ok] = Sig.SELL
    return sig


def rsi_pullback_long_only(df):
    """
    Long-only version: only buy dips in uptrend.
    Simpler, fewer false signals, better in uncertain markets.
    """
    close   = df['close']
    rsi     = ta.rsi(close, length=14)
    ema100  = ta.ema(close, length=100)
    ema200  = ta.ema(close, length=200)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    uptrend = (close > ema100) & (ema100 > ema200)

    # Exit: RSI peaks above 65 or trend breaks
    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend & (rsi.shift(1) < 35) & (rsi >= 42) & vol_ok] = Sig.BUY
    sig[(rsi >= 65) | (~uptrend)] = Sig.SELL   # take profit early or exit on trend break
    return sig


def regime_final(df):
    """
    Best regime adaptive from round 2, tuned for 5m.
    Trending (ADX>25): Supertrend flip + EMA alignment
    Ranging (ADX<20): RSI pullback
    """
    close   = df['close']
    ema100  = ta.ema(close, length=100)
    ema200  = ta.ema(close, length=200)
    rsi     = ta.rsi(close, length=14)
    adx_df  = ta.adx(df['high'], df['low'], close, length=14)
    adx     = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.1

    st = ta.supertrend(df['high'], df['low'], close, length=10, multiplier=2.5)
    if st is not None and not st.empty:
        st_dir = st.iloc[:, 1]
    else:
        st_dir = pd.Series(1, index=df.index)

    trending = adx >= 25
    ranging  = adx < 20

    st_flip_up   = (st_dir == 1)  & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[trending & st_flip_up   & (close > ema200) & (rsi < 70) & vol_ok] = Sig.BUY
    sig[trending & st_flip_down & (close < ema200) & (rsi > 30) & vol_ok] = Sig.SELL
    sig[ranging & (close > ema100) & (rsi.shift(1) < 35) & (rsi >= 42) & vol_ok] = Sig.BUY
    sig[ranging & (close < ema100) & (rsi.shift(1) > 65) & (rsi <= 58) & vol_ok] = Sig.SELL
    return sig


def production_strategy(df):
    """
    PRODUCTION CANDIDATE:
    Combines best elements from all rounds:
    1. Regime detection (ADX)
    2. RSI pullback in ranges (the consistent winner)
    3. Supertrend momentum in trends
    4. Volume + EMA200 trend filter on everything
    5. MACD histogram confirmation
    Designed to work in both bull and bear markets.
    """
    close   = df['close']
    high    = df['high']
    low     = df['low']
    ema100  = ta.ema(close, length=100)
    ema200  = ta.ema(close, length=200)
    rsi     = ta.rsi(close, length=14)
    atr     = ta.atr(high, low, close, length=14)

    adx_df  = ta.adx(high, low, close, length=14)
    adx     = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)

    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    hist    = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0, index=df.index)

    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.15

    st = ta.supertrend(high, low, close, length=10, multiplier=2.5)
    if st is not None and not st.empty:
        st_dir = st.iloc[:, 1]
    else:
        st_dir = pd.Series(1, index=df.index)

    # Market regimes
    strong_trend = adx >= 27
    ranging      = adx < 21

    # Supertrend flips
    st_flip_up   = (st_dir == 1)  & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    # Trend context
    macro_up   = close > ema200
    macro_down = close < ema200
    micro_up   = close > ema100

    sig = pd.Series(Sig.HOLD, index=df.index)

    # TRENDING MODE: Supertrend flip + macro alignment + MACD
    buy_trend  = strong_trend & st_flip_up   & macro_up   & (hist > 0) & (rsi < 72) & vol_ok
    sell_trend = strong_trend & st_flip_down & macro_down & (hist < 0) & (rsi > 28) & vol_ok

    # RANGING MODE: RSI pullback from extremes
    buy_range  = ranging & micro_up   & macro_up   & (rsi.shift(1) < 36) & (rsi >= 43) & vol_ok
    sell_range = ranging & ~micro_up  & macro_down & (rsi.shift(1) > 64) & (rsi <= 57) & vol_ok

    sig[buy_trend  | buy_range]  = Sig.BUY
    sig[sell_trend | sell_range] = Sig.SELL
    return sig


STRATEGIES_R3 = {
    'RSI Pullback Final':       rsi_pullback_final,
    'RSI Pullback (long only)': rsi_pullback_long_only,
    'Regime Final':             regime_final,
    'Production Strategy':      production_strategy,
}

SYMBOLS   = ['BTC/USD', 'ETH/USD', 'SOL/USD']
TIMEFRAME = '5m'
DAYS      = 180   # 6 months of 5m data


async def main():
    print("=" * 60)
    print("  ROUND 3: 5m CANDLES — FINAL CANDIDATES")
    print(f"  Timeframe: {TIMEFRAME}  |  Period: {DAYS} days")
    print("=" * 60)

    data = {}
    for symbol in SYMBOLS:
        try:
            data[symbol] = await fetch_data(symbol, TIMEFRAME, DAYS)
        except Exception as e:
            print(f"  ERROR: {e}")

    if not data:
        return

    results = []
    total = len(STRATEGIES_R3) * len(data)
    done = 0
    for name, fn in STRATEGIES_R3.items():
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
    pd.DataFrame(rows).to_csv('/opt/crypto-bot/data/backtest_round3.csv', index=False)
    print("Saved to /opt/crypto-bot/data/backtest_round3.csv")


if __name__ == '__main__':
    asyncio.run(main())
