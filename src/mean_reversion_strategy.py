"""
Mean Reversion Strategy
Designed for RANGING markets where price oscillates between support/resistance.

Logic:
  BUY  when RSI < 35 AND price touches/crosses below lower Bollinger Band
  SELL when RSI > 65 AND price touches/crosses above upper Bollinger Band
  EXIT long when price returns to middle band OR RSI > 60
  EXIT short when price returns to middle band OR RSI < 40

Tighter stops than trend-following: 0.6x ATR SL, 1.2x ATR TP.
Fires more frequently — designed to capture small range oscillations.
"""

import pandas as pd
import pandas_ta as ta
from typing import Optional
from dataclasses import dataclass
from .indicators import Signal


@dataclass
class MRSignal:
    signal: Signal
    close: float
    rsi: float
    bb_upper: float
    bb_lower: float
    bb_mid: float
    atr: float
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
        return 0.6

    def take_profit_pct(self) -> float:
        if self.close > 0:
            return abs(self.take_profit_price - self.close) / self.close * 100
        return 1.2


class MeanReversionStrategy:
    """
    Bollinger Band + RSI mean reversion.
    Works best when ADX < 25 (confirmed ranging) and price is at band extremes.
    """

    def __init__(self,
                 bb_period: int = 20,
                 bb_std: float = 2.0,
                 rsi_period: int = 14,
                 rsi_buy: float = 35.0,
                 rsi_sell: float = 65.0,
                 atr_sl_mult: float = 0.6,
                 atr_tp_mult: float = 1.2,
                 band_touch_pct: float = 0.2):   # how close to band to count as "touch"
        self.bb_period    = bb_period
        self.bb_std       = bb_std
        self.rsi_period   = rsi_period
        self.rsi_buy      = rsi_buy
        self.rsi_sell     = rsi_sell
        self.atr_sl_mult  = atr_sl_mult
        self.atr_tp_mult  = atr_tp_mult
        self.band_touch   = band_touch_pct / 100

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df['close']

        rsi = ta.rsi(close, length=self.rsi_period)
        df['rsi'] = rsi

        try:
            bb = ta.bbands(close, length=self.bb_period, std=self.bb_std)
        except Exception:
            bb = None
        if bb is not None:
            # pandas-ta bbands column order: BBL (lower), BBM (mid), BBU (upper), ...
            df['bb_lower'] = bb.iloc[:, 0]
            df['bb_mid']   = bb.iloc[:, 1]
            df['bb_upper'] = bb.iloc[:, 2]
        else:
            sma = close.rolling(self.bb_period).mean()
            std = close.rolling(self.bb_period).std()
            df['bb_upper'] = sma + self.bb_std * std
            df['bb_mid']   = sma
            df['bb_lower'] = sma - self.bb_std * std

        df['atr'] = ta.atr(df['high'], df['low'], close, length=14)

        adx_df = ta.adx(df['high'], df['low'], close, length=14)
        df['adx'] = adx_df.iloc[:, 0] if adx_df is not None else 20.0

        # BUY: RSI oversold + price at or below lower band
        near_lower = close <= df['bb_lower'] * (1 + self.band_touch)
        df['mr_buy'] = (rsi < self.rsi_buy) & near_lower

        # SELL (go short or exit long): RSI overbought + price at or above upper band
        near_upper = close >= df['bb_upper'] * (1 - self.band_touch)
        df['mr_sell'] = (rsi > self.rsi_sell) & near_upper

        # Exit long: price back at mid band OR RSI recovered to 58+
        df['exit_long'] = (close >= df['bb_mid']) | (rsi >= 58)

        # Exit short: price back at mid band OR RSI fell to 42-
        df['exit_short'] = (close <= df['bb_mid']) | (rsi <= 42)

        df['signal'] = Signal.HOLD
        df.loc[df['mr_buy'],  'signal'] = Signal.BUY
        df.loc[df['mr_sell'], 'signal'] = Signal.SELL

        return df

    def get_latest_signal(self, df: pd.DataFrame) -> Optional[MRSignal]:
        min_bars = self.bb_period + self.rsi_period + 10
        if df is None or len(df) < min_bars:
            return None

        try:
            df = self.calculate(df)
            row = df.iloc[-1]

            if pd.isna(row.get('bb_lower')) or pd.isna(row.get('atr')):
                return None

            price = float(row['close'])
            atr   = float(row['atr']) if not pd.isna(row['atr']) else price * 0.001

            sig = row['signal']
            if sig == Signal.BUY:
                sl = price - atr * self.atr_sl_mult
                tp = float(row['bb_mid'])   # target the middle band
                # If mid is too close, use ATR-based TP
                if tp - price < atr * 0.3:
                    tp = price + atr * self.atr_tp_mult
            elif sig == Signal.SELL:
                sl = price + atr * self.atr_sl_mult
                tp = float(row['bb_mid'])
                if price - tp < atr * 0.3:
                    tp = price - atr * self.atr_tp_mult
            else:
                sl = price - atr * self.atr_sl_mult
                tp = price + atr * self.atr_tp_mult

            return MRSignal(
                signal=sig,
                close=price,
                rsi=float(row['rsi']),
                bb_upper=float(row['bb_upper']),
                bb_lower=float(row['bb_lower']),
                bb_mid=float(row['bb_mid']),
                atr=atr,
                adx=float(row['adx']) if not pd.isna(row['adx']) else 20.0,
                stop_loss_price=sl,
                take_profit_price=tp,
                timestamp=int(df.index[-1].timestamp() * 1000) if isinstance(df.index[-1], pd.Timestamp) else None,
            )
        except Exception as e:
            return None

    def should_exit_long(self, df: pd.DataFrame) -> bool:
        """True when a held long position should be closed (price reached mid band)."""
        try:
            df = self.calculate(df)
            return bool(df['exit_long'].iloc[-1])
        except Exception:
            return False

    def should_exit_short(self, df: pd.DataFrame) -> bool:
        """True when a held short position should be closed (price reached mid band)."""
        try:
            df = self.calculate(df)
            return bool(df['exit_short'].iloc[-1])
        except Exception:
            return False
