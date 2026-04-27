"""
Technical Indicators for Scalping Strategy
EMA Crossover + RSI
"""

import pandas as pd
import pandas_ta as ta
from typing import Tuple, Optional
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class IndicatorResult:
    """Result from indicator calculation"""
    signal: Signal
    ema_fast: float
    ema_slow: float
    rsi: float
    close: float
    timestamp: Optional[int] = None

    @property
    def is_buy(self) -> bool:
        return self.signal == Signal.BUY

    @property
    def is_sell(self) -> bool:
        return self.signal == Signal.SELL


class EMACrossRSI:
    """
    EMA Crossover with RSI filter strategy

    Buy signal: Fast EMA crosses above Slow EMA AND RSI < overbought
    Sell signal: Fast EMA crosses below Slow EMA AND RSI > oversold

    Parameters:
        fast_ema: Period for fast EMA (default: 9)
        slow_ema: Period for slow EMA (default: 21)
        rsi_period: RSI calculation period (default: 14)
        rsi_overbought: RSI overbought threshold (default: 70)
        rsi_oversold: RSI oversold threshold (default: 30)
    """

    def __init__(self, fast_ema: int = 9, slow_ema: int = 21,
                 rsi_period: int = 14, rsi_overbought: int = 70,
                 rsi_oversold: int = 30):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate indicators on dataframe

        Expects df with columns: 'open', 'high', 'low', 'close', 'volume'
        Adds columns: 'ema_fast', 'ema_slow', 'rsi', 'signal'
        """
        df = df.copy()

        # Calculate EMAs
        df['ema_fast'] = ta.ema(df['close'], length=self.fast_ema)
        df['ema_slow'] = ta.ema(df['close'], length=self.slow_ema)

        # Calculate RSI
        df['rsi'] = ta.rsi(df['close'], length=self.rsi_period)

        # Generate signals
        df['signal'] = Signal.HOLD

        # Buy: EMA fast crosses above slow AND RSI not overbought
        df['ema_cross_up'] = (df['ema_fast'] > df['ema_slow']) & \
                             (df['ema_fast'].shift(1) <= df['ema_slow'].shift(1))
        df['buy_signal'] = df['ema_cross_up'] & (df['rsi'] < self.rsi_overbought)

        # Sell: EMA fast crosses below slow AND RSI not oversold
        df['ema_cross_down'] = (df['ema_fast'] < df['ema_slow']) & \
                               (df['ema_fast'].shift(1) >= df['ema_slow'].shift(1))
        df['sell_signal'] = df['ema_cross_down'] & (df['rsi'] > self.rsi_oversold)

        # Set final signals
        df.loc[df['buy_signal'], 'signal'] = Signal.BUY
        df.loc[df['sell_signal'], 'signal'] = Signal.SELL

        # Cleanup temp columns
        df.drop(columns=['ema_cross_up', 'ema_cross_down', 'buy_signal', 'sell_signal'],
                inplace=True, errors='ignore')

        return df

    def get_latest_signal(self, df: pd.DataFrame) -> Optional[IndicatorResult]:
        """Get the most recent signal from dataframe"""
        if df.empty or len(df) < self.slow_ema:
            return None

        df = self.calculate(df)
        last_row = df.iloc[-1]

        return IndicatorResult(
            signal=last_row['signal'],
            ema_fast=last_row['ema_fast'],
            ema_slow=last_row['ema_slow'],
            rsi=last_row['rsi'],
            close=last_row['close'],
            timestamp=int(df.index[-1].timestamp() * 1000) if isinstance(df.index[-1], pd.Timestamp) else None
        )

    def get_signals_history(self, df: pd.DataFrame) -> pd.DataFrame:
        """Get dataframe with all signals marked"""
        return self.calculate(df)


def prepare_ohlcv_dataframe(ohlcv_data: list, columns: list = None) -> pd.DataFrame:
    """
    Convert OHLCV list from exchange to pandas DataFrame

    Args:
        ohlcv_data: List of [timestamp, open, high, low, close, volume]
        columns: Column names (default: ['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    Returns:
        DataFrame with datetime index
    """
    if not ohlcv_data:
        return pd.DataFrame()

    columns = columns or ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    df = pd.DataFrame(ohlcv_data, columns=columns)

    # Convert timestamp to datetime
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    # Ensure numeric types
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    return df


if __name__ == '__main__':
    # Test with sample data
    import numpy as np

    # Generate test data
    dates = pd.date_range('2024-01-01', periods=100, freq='1min')
    test_df = pd.DataFrame({
        'open': np.random.uniform(40000, 41000, 100),
        'high': np.random.uniform(41000, 42000, 100),
        'low': np.random.uniform(39000, 40000, 100),
        'close': np.random.uniform(40000, 41000, 100),
        'volume': np.random.uniform(100, 1000, 100)
    }, index=dates)

    strategy = EMACrossRSI()
    result_df = strategy.get_signals_history(test_df)

    print("Sample signals:")
    signals = result_df[result_df['signal'] != Signal.HOLD]
    print(signals[['close', 'ema_fast', 'ema_slow', 'rsi', 'signal']])
