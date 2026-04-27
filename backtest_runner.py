"""
Comprehensive Backtest Runner
Tests multiple strategies on historical Kraken data and ranks them.
Run on VPS: python backtest_runner.py
"""

import asyncio
import sys
import os
import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from enum import Enum

sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_data_binance(symbol: str, timeframe: str = '1h', days: int = 500) -> pd.DataFrame:
    """
    Fetch historical data from Binance public API.
    Binance provides 1000 candles per request, paginated.
    symbol: e.g. 'BTC/USD' → converted to 'BTCUSDT'
    """
    import aiohttp
    from datetime import datetime, timedelta, timezone

    tf_map = {'1m':'1m','5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    interval = tf_map.get(timeframe, '1h')

    # Convert symbol format
    base = symbol.split('/')[0]
    binance_symbol = f"{base}USDT"

    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    all_candles = []
    current_ms  = start_ms

    async with aiohttp.ClientSession() as session:
        while current_ms < end_ms:
            params = {
                'symbol':    binance_symbol,
                'interval':  interval,
                'startTime': current_ms,
                'endTime':   end_ms,
                'limit':     1000
            }
            try:
                async with session.get(
                    'https://api.binance.com/api/v3/klines',
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        break
                    batch = await resp.json()
                    if not batch:
                        break
                    all_candles.extend(batch)
                    current_ms = batch[-1][0] + 1
                    if len(batch) < 1000:
                        break
            except Exception as e:
                print(f"    Binance fetch error: {e}")
                break

    if not all_candles:
        raise ValueError(f"No data from Binance for {binance_symbol}")

    df = pd.DataFrame(all_candles, columns=[
        'timestamp','open','high','low','close','volume',
        'close_time','quote_vol','trades','taker_base','taker_quote','ignore'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    for col in ['open','high','low','close','volume']:
        df[col] = pd.to_numeric(df[col])
    df = df[['open','high','low','close','volume']]

    return df


async def fetch_data(symbol: str, timeframe: str = '1h', days: int = 500) -> pd.DataFrame:
    print(f"  Fetching {symbol} {timeframe} ({days} days) from Binance...")
    df = await fetch_data_binance(symbol, timeframe, days)
    print(f"  Got {len(df)} candles for {symbol} ({df.index[0].date()} to {df.index[-1].date()})")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL ENUM
# ─────────────────────────────────────────────────────────────────────────────

class Sig(Enum):
    BUY  = 1
    SELL = -1
    HOLD = 0


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

def strat_ema_cross_basic(df: pd.DataFrame) -> pd.Series:
    """Baseline: EMA 9/21 cross only"""
    fast = ta.ema(df['close'], length=9)
    slow = ta.ema(df['close'], length=21)
    rsi  = ta.rsi(df['close'], length=14)
    cross_up   = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[cross_up   & (rsi < 70)] = Sig.BUY
    sig[cross_down & (rsi > 30)] = Sig.SELL
    return sig


def strat_advanced_loose(df: pd.DataFrame) -> pd.Series:
    """Advanced strategy with relaxed filters (ADX>20, Vol>1.1x)"""
    fast = ta.ema(df['close'], length=9)
    slow = ta.ema(df['close'], length=21)
    trend = ta.ema(df['close'], length=50)
    rsi  = ta.rsi(df['close'], length=14)
    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    hist = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0, index=df.index)
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    adx = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(25, index=df.index)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ratio = df['volume'] / vol_sma.replace(0, 1)

    cross_up   = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    sig = pd.Series(Sig.HOLD, index=df.index)
    buy_cond  = cross_up  & (rsi < 70) & (hist > 0) & (vol_ratio >= 1.1) & (adx >= 20) & (df['close'] > trend)
    sell_cond = cross_down & (rsi > 30) & (hist < 0) & (vol_ratio >= 1.1) & (adx >= 20) & (df['close'] < trend)
    sig[buy_cond]  = Sig.BUY
    sig[sell_cond] = Sig.SELL
    return sig


def strat_advanced_tight(df: pd.DataFrame) -> pd.Series:
    """Advanced strategy with tight filters (ADX>28, Vol>1.5x)"""
    fast = ta.ema(df['close'], length=9)
    slow = ta.ema(df['close'], length=21)
    trend = ta.ema(df['close'], length=50)
    rsi  = ta.rsi(df['close'], length=14)
    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    hist = macd_df.iloc[:, 2] if macd_df is not None else pd.Series(0, index=df.index)
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    adx = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(25, index=df.index)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ratio = df['volume'] / vol_sma.replace(0, 1)

    cross_up   = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    sig = pd.Series(Sig.HOLD, index=df.index)
    buy_cond  = cross_up  & (rsi < 65) & (hist > 0) & (vol_ratio >= 1.5) & (adx >= 28) & (df['close'] > trend)
    sell_cond = cross_down & (rsi > 35) & (hist < 0) & (vol_ratio >= 1.5) & (adx >= 28) & (df['close'] < trend)
    sig[buy_cond]  = Sig.BUY
    sig[sell_cond] = Sig.SELL
    return sig


def strat_bb_squeeze(df: pd.DataFrame) -> pd.Series:
    """Bollinger Band squeeze breakout — enters when volatility expands after compression"""
    close = df['close']
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is None:
        return pd.Series(Sig.HOLD, index=df.index)
    upper = bb.iloc[:, 0]
    mid   = bb.iloc[:, 1]
    lower = bb.iloc[:, 2]
    bw    = (upper - lower) / mid   # bandwidth

    rsi  = ta.rsi(close, length=14)
    trend = ta.ema(close, length=50)

    # Squeeze: bandwidth in lowest 20% over last 50 bars
    bw_low = bw.rolling(50).quantile(0.20)
    was_squeezed = bw.shift(1) <= bw_low.shift(1)
    expanding    = bw > bw.shift(1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    buy_cond  = was_squeezed & expanding & (close > mid) & (rsi > 50) & (close > trend)
    sell_cond = was_squeezed & expanding & (close < mid) & (rsi < 50) & (close < trend)
    sig[buy_cond]  = Sig.BUY
    sig[sell_cond] = Sig.SELL
    return sig


def strat_rsi_pullback(df: pd.DataFrame) -> pd.Series:
    """
    Trend + RSI pullback:
    In uptrend (price > EMA 200), buy when RSI dips below 40 then recovers above 45.
    In downtrend, sell when RSI spikes above 60 then drops below 55.
    """
    close = df['close']
    rsi   = ta.rsi(close, length=14)
    trend = ta.ema(close, length=200)

    uptrend   = close > trend
    downtrend = close < trend

    # RSI recovery from oversold
    rsi_was_low  = rsi.shift(1) < 40
    rsi_now_ok   = rsi >= 45
    # RSI rejection from overbought
    rsi_was_high = rsi.shift(1) > 60
    rsi_now_back = rsi <= 55

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[uptrend   & rsi_was_low  & rsi_now_ok ] = Sig.BUY
    sig[downtrend & rsi_was_high & rsi_now_back] = Sig.SELL
    return sig


def strat_macd_zero_cross(df: pd.DataFrame) -> pd.Series:
    """MACD line crosses zero — momentum confirmation"""
    close = df['close']
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is None:
        return pd.Series(Sig.HOLD, index=df.index)
    macd_line = macd_df.iloc[:, 0]
    hist      = macd_df.iloc[:, 2]
    trend = ta.ema(close, length=50)
    rsi   = ta.rsi(close, length=14)

    cross_up   = (macd_line > 0) & (macd_line.shift(1) <= 0)
    cross_down = (macd_line < 0) & (macd_line.shift(1) >= 0)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[cross_up   & (hist > 0) & (close > trend) & (rsi < 70)] = Sig.BUY
    sig[cross_down & (hist < 0) & (close < trend) & (rsi > 30)] = Sig.SELL
    return sig


def strat_supertrend(df: pd.DataFrame) -> pd.Series:
    """Supertrend indicator — trend following with ATR-based stops"""
    close = df['close']
    st = ta.supertrend(df['high'], df['low'], close, length=10, multiplier=3.0)
    if st is None or st.empty:
        return pd.Series(Sig.HOLD, index=df.index)

    # Supertrend direction column
    direction = st.iloc[:, 1]   # 1 = uptrend, -1 = downtrend
    rsi = ta.rsi(close, length=14)

    prev_dir = direction.shift(1)
    flip_up   = (direction == 1)  & (prev_dir == -1)
    flip_down = (direction == -1) & (prev_dir == 1)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[flip_up   & (rsi < 75)] = Sig.BUY
    sig[flip_down & (rsi > 25)] = Sig.SELL
    return sig


def strat_ema_ribbon(df: pd.DataFrame) -> pd.Series:
    """
    EMA ribbon: all short EMAs aligned in same direction + RSI + volume.
    More reliable than single crossover.
    """
    close = df['close']
    e5  = ta.ema(close, length=5)
    e8  = ta.ema(close, length=8)
    e13 = ta.ema(close, length=13)
    e21 = ta.ema(close, length=21)
    e34 = ta.ema(close, length=34)
    e55 = ta.ema(close, length=55)
    rsi = ta.rsi(close, length=14)
    vol_sma = df['volume'].rolling(20).mean()
    vol_ok  = df['volume'] > vol_sma * 1.2

    bullish = (e5 > e8) & (e8 > e13) & (e13 > e21) & (e21 > e34) & (e34 > e55)
    bearish = (e5 < e8) & (e8 < e13) & (e13 < e21) & (e21 < e34) & (e34 < e55)

    # Enter on first candle all EMAs align
    prev_bull = bullish.shift(1)
    prev_bear = bearish.shift(1)
    flip_bull = bullish & ~prev_bull.fillna(False)
    flip_bear = bearish & ~prev_bear.fillna(False)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[flip_bull & (rsi < 75) & vol_ok] = Sig.BUY
    sig[flip_bear & (rsi > 25) & vol_ok] = Sig.SELL
    return sig


def strat_heikin_ashi_trend(df: pd.DataFrame) -> pd.Series:
    """
    Heikin-Ashi candles smooth out noise.
    Enter when HA candles flip direction + ADX confirms trend.
    """
    ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    ha_open  = ha_close.copy()
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2

    ha_bull = ha_close > ha_open   # green HA candle
    ha_bear = ha_close < ha_open   # red HA candle

    prev_bull = ha_bull.shift(1)
    flip_bull = ha_bull & ~prev_bull.fillna(True)
    flip_bear = ha_bear & ha_bull.shift(1).fillna(False)

    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    adx = adx_df.iloc[:, 0] if adx_df is not None else pd.Series(20, index=df.index)
    trend = ta.ema(df['close'], length=50)

    sig = pd.Series(Sig.HOLD, index=df.index)
    sig[flip_bull & (adx >= 20) & (df['close'] > trend)] = Sig.BUY
    sig[flip_bear & (adx >= 20) & (df['close'] < trend)] = Sig.SELL
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    name: str
    symbol: str
    trades: int
    win_rate: float
    total_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy: float   # avg $ per trade


def backtest(df: pd.DataFrame, signals: pd.Series,
             symbol: str, name: str,
             capital: float = 1000.0,
             position_pct: float = 0.50,
             fee_pct: float = 0.0026,
             slippage_pct: float = 0.001,
             atr_sl_mult: float = 1.5,
             atr_tp_mult: float = 3.0) -> Result:

    atr = ta.atr(df['high'], df['low'], df['close'], length=14)
    equity = capital
    position = None
    trades, equity_curve = [], [capital]
    warmup = 60

    for i in range(warmup, len(df)):
        price = df['close'].iloc[i]
        sig   = signals.iloc[i]
        atr_i = atr.iloc[i] if not pd.isna(atr.iloc[i]) else price * 0.01

        # Check SL/TP on open position
        if position:
            ep, sl, tp, size = position
            pnl_pct = (price - ep) / ep
            if price <= sl or price >= tp or sig == Sig.SELL:
                reason = 'sl' if price <= sl else ('tp' if price >= tp else 'signal')
                exit_p = price * (1 - slippage_pct)
                fee    = exit_p * size * fee_pct
                pnl    = (exit_p - ep) * size - fee
                equity += pnl
                trades.append({'pnl': pnl, 'pnl_pct': (exit_p - ep) / ep * 100})
                position = None

        # Entry
        if position is None and sig == Sig.BUY:
            entry   = price * (1 + slippage_pct)
            sl      = entry - atr_i * atr_sl_mult
            tp      = entry + atr_i * atr_tp_mult
            trade_usd = equity * position_pct
            size      = trade_usd / entry
            fee     = entry * size * fee_pct
            equity -= fee
            position = (entry, sl, tp, size)

        equity_curve.append(equity)

    # Close at end
    if position:
        ep, sl, tp, size = position
        price  = df['close'].iloc[-1]
        exit_p = price * (1 - slippage_pct)
        fee    = exit_p * size * fee_pct
        pnl    = (exit_p - ep) * size - fee
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
    running_max  = eq.expanding().max()
    drawdown     = (eq - running_max) / running_max * 100
    max_dd       = abs(drawdown.min())

    returns = eq.pct_change().dropna()
    sharpe  = (returns.mean() / returns.std()) * np.sqrt(365 * 24) if returns.std() > 0 else 0

    return Result(name, symbol, len(trades), round(win_rate, 1),
                  round(total_return, 2), round(profit_factor, 2),
                  round(max_dd, 2), round(sharpe, 2),
                  round(avg_win_pct, 2), round(avg_loss_pct, 2),
                  round(expectancy, 4))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

STRATEGIES = {
    'EMA Cross (baseline)':    strat_ema_cross_basic,
    'Advanced (loose)':        strat_advanced_loose,
    'Advanced (tight)':        strat_advanced_tight,
    'BB Squeeze Breakout':     strat_bb_squeeze,
    'RSI Pullback':            strat_rsi_pullback,
    'MACD Zero Cross':         strat_macd_zero_cross,
    'Supertrend':              strat_supertrend,
    'EMA Ribbon':              strat_ema_ribbon,
    'Heikin-Ashi Trend':       strat_heikin_ashi_trend,
}

SYMBOLS   = ['BTC/USD', 'ETH/USD', 'SOL/USD']
TIMEFRAME = '1h'
DAYS      = 500   # ~16 months — covers bull + bear


def print_results(results: List[Result]):
    # Sort by expectancy (avg $ per trade), then profit factor
    results.sort(key=lambda r: (r.profit_factor * (1 if r.trades >= 10 else 0), r.total_return_pct), reverse=True)

    print("\n" + "=" * 110)
    print(f"{'Strategy':<28} {'Symbol':<10} {'Trades':>6} {'WinRate':>8} {'Return%':>9} {'PF':>6} {'MaxDD%':>8} {'Sharpe':>7} {'AvgW%':>7} {'AvgL%':>7} {'Expect$':>9}")
    print("=" * 110)

    for r in results:
        if r.trades == 0:
            print(f"  {r.name:<26} {r.symbol:<10} {'NO TRADES':>6}")
            continue
        flag = " ★" if r.profit_factor >= 1.5 and r.win_rate >= 45 and r.trades >= 10 else ""
        print(f"  {r.name:<26} {r.symbol:<10} {r.trades:>6} {r.win_rate:>7.1f}% {r.total_return_pct:>8.1f}% {r.profit_factor:>6.2f} {r.max_drawdown_pct:>7.1f}% {r.sharpe:>7.2f} {r.avg_win_pct:>6.2f}% {r.avg_loss_pct:>6.2f}% {r.expectancy:>9.4f}{flag}")

    print("=" * 110)
    print("★ = profit factor ≥ 1.5 AND win rate ≥ 45% AND ≥ 10 trades\n")

    # Best per symbol
    print("BEST STRATEGY PER SYMBOL:")
    for symbol in SYMBOLS:
        sym_results = [r for r in results if r.symbol == symbol and r.trades >= 5]
        if sym_results:
            best = max(sym_results, key=lambda r: r.profit_factor)
            print(f"  {symbol:<10} → {best.name:<28} PF={best.profit_factor:.2f}  Return={best.total_return_pct:.1f}%  WR={best.win_rate:.1f}%  Trades={best.trades}")

    # Overall winner
    valid = [r for r in results if r.trades >= 10 and r.profit_factor > 0]
    if valid:
        winner = max(valid, key=lambda r: r.profit_factor)
        print(f"\n  OVERALL WINNER: {winner.name} on {winner.symbol} | PF={winner.profit_factor:.2f} | Return={winner.total_return_pct:.1f}% | WR={winner.win_rate:.1f}%")


async def main():
    print("=" * 60)
    print("  CRYPTO BOT — STRATEGY BACKTEST")
    print(f"  Timeframe: {TIMEFRAME}  |  Period: {DAYS} days")
    print(f"  Strategies: {len(STRATEGIES)}  |  Symbols: {len(SYMBOLS)}")
    print("=" * 60)

    # Fetch data for all symbols
    data = {}
    for symbol in SYMBOLS:
        try:
            data[symbol] = await fetch_data(symbol, TIMEFRAME, DAYS)
        except Exception as e:
            print(f"  ERROR fetching {symbol}: {e}")

    if not data:
        print("No data fetched. Exiting.")
        return

    # Run all backtests
    results = []
    total = len(STRATEGIES) * len(data)
    done  = 0

    for name, strat_fn in STRATEGIES.items():
        for symbol, df in data.items():
            done += 1
            print(f"  [{done}/{total}] {name} on {symbol}...")
            try:
                signals = strat_fn(df.copy())
                result  = backtest(df, signals, symbol, name)
                results.append(result)
            except Exception as e:
                print(f"    ERROR: {e}")

    print_results(results)

    # Save to file
    rows = [vars(r) for r in results]
    pd.DataFrame(rows).to_csv('/opt/crypto-bot/data/backtest_results.csv', index=False)
    print("\nResults saved to /opt/crypto-bot/data/backtest_results.csv")


if __name__ == '__main__':
    asyncio.run(main())
