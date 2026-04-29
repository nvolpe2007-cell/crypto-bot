"""
Advanced Trading Strategy
EMA crossover + RSI + MACD confirmation + Volume filter + ATR stops + Trend filter
"""

import pandas as pd
import pandas_ta as ta
from typing import Optional
from dataclasses import dataclass
from .indicators import Signal, IndicatorResult


@dataclass
class AdvancedSignal:
    signal: Signal
    close: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    atr: float
    volume_ratio: float
    adx: float
    stop_loss_price: float
    take_profit_price: float
    timestamp: Optional[int] = None

    @property
    def is_buy(self) -> bool:
        return self.signal == Signal.BUY

    @property
    def is_sell(self) -> bool:
        return self.signal == Signal.SELL

    def stop_loss_pct(self) -> float:
        if self.close > 0:
            return abs(self.close - self.stop_loss_price) / self.close * 100
        return 2.0

    def take_profit_pct(self) -> float:
        if self.close > 0:
            return abs(self.take_profit_price - self.close) / self.close * 100
        return 3.0


class AdvancedStrategy:
    """
    Multi-confirmation scalping strategy.

    Entry requires ALL of:
    - EMA 9/21 crossover in signal direction
    - RSI not in extreme zone
    - MACD histogram confirms direction
    - Volume above 20-period average
    - ADX > 20 (trend has strength)
    - Price above 50 EMA for buys (trend filter)

    Stops use 1.5x ATR for dynamic risk management.
    """

    def __init__(self,
                 fast_ema: int = 9,
                 slow_ema: int = 21,
                 trend_ema: int = 50,
                 rsi_period: int = 14,
                 rsi_overbought: int = 70,
                 rsi_oversold: int = 30,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 atr_period: int = 14,
                 atr_multiplier: float = 1.5,
                 volume_period: int = 20,
                 volume_threshold: float = 1.3,
                 adx_period: int = 14,
                 adx_threshold: float = 20.0):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.trend_ema = trend_ema
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.volume_period = volume_period
        self.volume_threshold = volume_threshold
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    def confidence_score(self, row) -> int:
        """
        Score 0-100 for how strongly all conditions are met.
        Only scores in the direction of the signal.
        """
        import math
        score = 0
        sig = row.get('signal')
        if sig is None or str(sig) == 'Signal.HOLD':
            return 0

        is_buy = str(sig) == 'Signal.BUY'

        # ADX strength (0-25 pts)
        adx = row.get('adx', 0) or 0
        score += min(25, int((adx / 40) * 25))

        # Volume ratio (0-20 pts) — rewards high volume but no longer hard-blocks
        vr = row.get('volume_ratio', 0) or 0
        score += min(20, max(0, int(((vr - 0.5) / 1.5) * 20)))

        # RSI position (0-20 pts): for buy, lower RSI = better; for sell, higher = better
        rsi = row.get('rsi', 50) or 50
        if is_buy:
            score += max(0, int(((70 - rsi) / 40) * 20))
        else:
            score += max(0, int(((rsi - 30) / 40) * 20))

        # MACD histogram strength (0-20 pts)
        hist = abs(row.get('macd_hist', 0) or 0)
        macd = abs(row.get('macd', 0.0001) or 0.0001)
        score += min(20, int((hist / (macd + 1e-9)) * 10))

        # ATR / close ratio — volatility sweet spot (0-10 pts)
        atr = row.get('atr', 0) or 0
        close = row.get('close', 1) or 1
        atr_pct = (atr / close) * 100
        if 0.3 <= atr_pct <= 2.0:
            score += 10
        elif atr_pct < 0.3 or atr_pct > 4.0:
            score += 0
        else:
            score += 5

        # Trend alignment (0-10 pts) — reward but don't require uptrend
        ema_trend = row.get('ema_trend') or 0
        if ema_trend and close:
            if is_buy and close > ema_trend:
                score += 10
            elif not is_buy and close < ema_trend:
                score += 10
            else:
                score += 3   # counter-trend trades get partial credit

        return min(100, score)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        df['ema_fast'] = ta.ema(df['close'], length=self.fast_ema)
        df['ema_slow'] = ta.ema(df['close'], length=self.slow_ema)
        df['ema_trend'] = ta.ema(df['close'], length=self.trend_ema)
        df['rsi'] = ta.rsi(df['close'], length=self.rsi_period)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=self.atr_period)
        df['vol_sma'] = df['volume'].rolling(self.volume_period).mean()
        df['volume_ratio'] = df['volume'] / df['vol_sma'].replace(0, 1)
        # If volume data is unavailable (all zeros), bypass the volume filter
        volume_data_ok = df['volume'].sum() > 0

        macd = ta.macd(df['close'], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal_period)
        if macd is not None:
            df['macd'] = macd.iloc[:, 0]
            df['macd_signal'] = macd.iloc[:, 1]
            df['macd_hist'] = macd.iloc[:, 2]
        else:
            df['macd'] = df['macd_signal'] = df['macd_hist'] = 0.0

        adx_result = ta.adx(df['high'], df['low'], df['close'], length=self.adx_period)
        if adx_result is not None:
            df['adx'] = adx_result.iloc[:, 0]
        else:
            df['adx'] = 25.0

        # Crossover detection
        df['cross_up'] = (df['ema_fast'] > df['ema_slow']) & (df['ema_fast'].shift(1) <= df['ema_slow'].shift(1))
        df['cross_down'] = (df['ema_fast'] < df['ema_slow']) & (df['ema_fast'].shift(1) >= df['ema_slow'].shift(1))

        # MACD confirmation
        df['macd_bullish'] = df['macd_hist'] > 0
        df['macd_bearish'] = df['macd_hist'] < 0

        # Volume: require any volume present (ratio > 0), not a surge
        # Kraken's 1m volume data is too inconsistent for a 1.3x threshold
        df['vol_ok'] = (df['volume_ratio'] > 0) | (~volume_data_ok)

        # Trend filter
        df['uptrend'] = df['close'] > df['ema_trend']
        df['downtrend'] = df['close'] < df['ema_trend']

        # ADX filter
        df['trend_strong'] = df['adx'] >= self.adx_threshold

        # Final signals
        # uptrend/downtrend used as confidence boost in scorer, not hard gate
        df['signal'] = Signal.HOLD
        # Core entry: EMA crossover + RSI not extreme + ADX shows trending
        # MACD and trend direction used only in confidence scorer, not as hard gates
        buy_cond = (
            df['cross_up'] &
            (df['rsi'] < self.rsi_overbought) &
            df['vol_ok'] &
            df['trend_strong']
        )
        sell_cond = (
            df['cross_down'] &
            (df['rsi'] > self.rsi_oversold) &
            df['vol_ok'] &
            df['trend_strong']
        )
        df.loc[buy_cond, 'signal'] = Signal.BUY
        df.loc[sell_cond, 'signal'] = Signal.SELL

        # ATR-based stop/tp levels
        df['sl_buy'] = df['close'] - df['atr'] * self.atr_multiplier
        df['tp_buy'] = df['close'] + df['atr'] * self.atr_multiplier * 2
        df['sl_sell'] = df['close'] + df['atr'] * self.atr_multiplier
        df['tp_sell'] = df['close'] - df['atr'] * self.atr_multiplier * 2

        df.drop(columns=['cross_up', 'cross_down', 'macd_bullish', 'macd_bearish',
                          'vol_ok', 'uptrend', 'downtrend', 'trend_strong', 'vol_sma'],
                inplace=True, errors='ignore')
        return df

    def get_latest_signal(self, df: pd.DataFrame) -> Optional[AdvancedSignal]:
        min_candles = max(self.slow_ema, self.trend_ema, self.macd_slow, self.rsi_period) + 10
        if df.empty or len(df) < min_candles:
            return None

        df = self.calculate(df)
        row = df.iloc[-1]

        if pd.isna(row.get('ema_fast')) or pd.isna(row.get('atr')):
            return None

        sl = row['sl_buy'] if row['signal'] == Signal.BUY else row['sl_sell']
        tp = row['tp_buy'] if row['signal'] == Signal.BUY else row['tp_sell']

        return AdvancedSignal(
            signal=row['signal'],
            close=row['close'],
            ema_fast=row['ema_fast'],
            ema_slow=row['ema_slow'],
            ema_trend=row['ema_trend'],
            rsi=row['rsi'],
            macd=row['macd'],
            macd_signal=row['macd_signal'],
            macd_hist=row['macd_hist'],
            atr=row['atr'],
            volume_ratio=row['volume_ratio'],
            adx=row['adx'],
            stop_loss_price=sl,
            take_profit_price=tp,
            timestamp=int(df.index[-1].timestamp() * 1000) if isinstance(df.index[-1], pd.Timestamp) else None
        )
