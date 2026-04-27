"""
Production Strategy — RSI 50 Cross
Validated across 3 years with walk-forward testing.
Positive on BOTH 2023-2024 (train) AND 2025-2026 (test).
Runs on 4-hour candles.

Logic: RSI crossing 50 signals momentum shift.
  Buy: RSI crosses 50 upward + price above EMA200 + volume surge
  Sell: RSI crosses 50 downward + price below EMA200 + volume surge
"""

import pandas as pd
import pandas_ta as ta
from typing import Optional
from dataclasses import dataclass
from .indicators import Signal


@dataclass
class ProductionSignal:
    signal: Signal
    close: float
    rsi: float
    ema100: float
    ema200: float
    adx: float
    atr: float
    stop_loss_price: float
    take_profit_price: float
    regime: str      # 'TRENDING' | 'RANGING' | 'BEAR'
    confidence: int
    timestamp: Optional[int] = None

    @property
    def is_buy(self): return self.signal == Signal.BUY
    @property
    def is_sell(self): return self.signal == Signal.SELL


class ProductionStrategy:
    """
    RSI 50 Cross — the validated winner across train + test data.

    Buy:  RSI(14) crosses above 50, price > EMA200, volume > 1.1x avg
    Sell: RSI(14) crosses below 50, price < EMA200, volume > 1.1x avg

    Stops: 1.5x ATR stop loss, 2.5x ATR take profit
    Timeframe: 4h candles
    """

    def __init__(self,
                 rsi_period: int = 14,
                 ema_trend: int = 200,
                 volume_mult: float = 1.1,
                 atr_sl_mult: float = 1.5,
                 atr_tp_mult: float = 2.5):
        self.rsi_period   = rsi_period
        self.ema_trend    = ema_trend
        self.volume_mult  = volume_mult
        self.atr_sl_mult  = atr_sl_mult
        self.atr_tp_mult  = atr_tp_mult

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['ema200']    = ta.ema(df['close'], length=self.ema_trend)
        df['ema50']     = ta.ema(df['close'], length=50)
        df['rsi']       = ta.rsi(df['close'], length=self.rsi_period)
        df['atr']       = ta.atr(df['high'], df['low'], df['close'], length=14)
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['adx']       = adx_df.iloc[:, 0] if adx_df is not None else 20.0

        vol_sma = df['volume'].rolling(20).mean()
        df['vol_ratio'] = df['volume'] / vol_sma.replace(0, 1)
        df['vol_ok']    = df['vol_ratio'] >= self.volume_mult
        df['macro_up']  = df['close'] > df['ema200']

        rsi = df['rsi']
        # RSI 50 crossover
        df['rsi_cross_up']   = (rsi > 50) & (rsi.shift(1) <= 50)
        df['rsi_cross_down'] = (rsi < 50) & (rsi.shift(1) >= 50)

        df['signal'] = Signal.HOLD
        df.loc[df['rsi_cross_up']   & df['macro_up']  & df['vol_ok'], 'signal'] = Signal.BUY
        df.loc[df['rsi_cross_down'] & ~df['macro_up'] & df['vol_ok'], 'signal'] = Signal.SELL

        return df

    def get_latest_signal(self, df: pd.DataFrame) -> Optional[ProductionSignal]:
        if len(df) < 210:
            return None

        df = self.calculate(df)
        row = df.iloc[-1]

        if pd.isna(row.get('ema200')) or pd.isna(row.get('atr')):
            return None

        price = row['close']
        atr   = row['atr']
        sig   = row['signal']

        sl = price - atr * self.atr_sl_mult if sig == Signal.BUY else price + atr * self.atr_sl_mult
        tp = price + atr * self.atr_tp_mult if sig == Signal.BUY else price - atr * self.atr_tp_mult

        regime = 'UPTREND' if row['macro_up'] else 'DOWNTREND'
        rsi_val = row['rsi']
        conf = min(100, int(
            abs(rsi_val - 50) * 2 +               # distance from 50 = conviction
            min(30, row['adx']) +                  # trend strength
            min(20, row['vol_ratio'] * 10)         # volume confirmation
        ))

        return ProductionSignal(
            signal=sig,
            close=price,
            rsi=round(rsi_val, 2),
            ema100=round(row['ema50'], 2),
            ema200=round(row['ema200'], 2),
            adx=round(row['adx'], 2),
            atr=round(atr, 4),
            stop_loss_price=round(sl, 4),
            take_profit_price=round(tp, 4),
            regime=regime,
            confidence=conf,
            timestamp=int(df.index[-1].timestamp() * 1000) if isinstance(df.index[-1], pd.Timestamp) else None
        )
