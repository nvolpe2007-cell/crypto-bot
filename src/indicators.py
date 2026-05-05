"""
Technical Indicators for Scalping Strategy
EMA Crossover + RSI + VWAP + Bollinger Band Squeeze + Volume Profile + CME Gap
"""

import logging
from datetime import datetime, timezone
from typing import Tuple, Optional, List
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


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


# ── VWAP ──────────────────────────────────────────────────────────────────────

@dataclass
class VWAPResult:
    vwap: float
    upper_band: float
    lower_band: float
    deviation_pct: float   # (price - vwap) / vwap * 100
    is_above: bool
    is_below: bool


class VWAPCalculator:
    """
    Computes session VWAP anchored at midnight UTC with ±1-std-dev bands.
    Falls back to a rolling 390-bar window when intraday data is sparse.
    """

    def __init__(self, rolling_window: int = 390):
        self.window = rolling_window

    def calculate(self, df: pd.DataFrame) -> Optional[VWAPResult]:
        if df is None or len(df) < 20:
            return None
        try:
            typical = (df['high'] + df['low'] + df['close']) / 3
            vol = df['volume']

            # Use intraday bars since midnight UTC, fallback to rolling window
            if hasattr(df.index[0], 'normalize'):
                today = df.index[-1].normalize()
                session_mask = df.index >= today
            else:
                idx = pd.to_datetime(df.index)
                today = idx[-1].normalize()
                session_mask = idx >= today

            if session_mask.sum() < 10:
                session_mask = pd.Series(True, index=df.index)
                session_mask.iloc[:-self.window] = False

            tp_vol = (typical * vol)[session_mask]
            vol_sess = vol[session_mask]
            vol_sum = float(vol_sess.sum())

            if vol_sum <= 0:
                vwap_v = float(typical.iloc[-1])
                std_v = float(typical.std())
            else:
                vwap_v = float(tp_vol.cumsum().iloc[-1] / vol_sess.cumsum().iloc[-1])
                tp_sess = typical[session_mask]
                variance = float(((tp_sess - vwap_v) ** 2 * vol_sess).sum() / vol_sum)
                std_v = max(float(typical.std()), variance ** 0.5)

            price = float(df['close'].iloc[-1])
            deviation_pct = (price - vwap_v) / vwap_v * 100 if vwap_v > 0 else 0.0

            return VWAPResult(
                vwap=vwap_v,
                upper_band=vwap_v + std_v,
                lower_band=vwap_v - std_v,
                deviation_pct=deviation_pct,
                is_above=price > vwap_v,
                is_below=price < vwap_v,
            )
        except Exception as e:
            logger.debug(f"[VWAP] calc error: {e}")
            return None


# ── Bollinger Band Squeeze ─────────────────────────────────────────────────────

@dataclass
class BBSqueezeResult:
    is_squeezing: bool
    just_broke_up: bool
    just_broke_down: bool
    bandwidth: float
    upper: float
    lower: float
    middle: float


class BBSqueezeDetector:
    """
    Detects Bollinger Band squeeze (low-volatility coiling before breakout).
    Squeeze = bandwidth < squeeze_threshold fraction of price.
    Breakout = first close outside the bands after a squeeze.
    """

    def __init__(self, period: int = 20, std: float = 2.0,
                 squeeze_threshold: float = 0.02):
        self.period = period
        self.std = std
        self.squeeze_threshold = squeeze_threshold

    def calculate(self, df: pd.DataFrame) -> Optional[BBSqueezeResult]:
        if df is None or len(df) < self.period + 5:
            return None
        try:
            close = df['close']
            bb = ta.bbands(close, length=self.period, std=self.std)
            if bb is None or bb.empty:
                return None

            upper  = bb.iloc[:, 0]
            middle = bb.iloc[:, 1]
            lower  = bb.iloc[:, 2]

            bandwidth = (upper - lower) / middle

            # Was there a squeeze in the last 5 closed bars?
            recent_bw = bandwidth.iloc[-6:-1]
            was_squeezing = (recent_bw < self.squeeze_threshold).any()

            current_bw = float(bandwidth.iloc[-1])
            is_squeezing_now = current_bw < self.squeeze_threshold

            price    = float(close.iloc[-1])
            upper_v  = float(upper.iloc[-1])
            lower_v  = float(lower.iloc[-1])
            middle_v = float(middle.iloc[-1])

            just_broke_up   = was_squeezing and not is_squeezing_now and price > upper_v
            just_broke_down = was_squeezing and not is_squeezing_now and price < lower_v

            return BBSqueezeResult(
                is_squeezing=is_squeezing_now,
                just_broke_up=just_broke_up,
                just_broke_down=just_broke_down,
                bandwidth=current_bw,
                upper=upper_v,
                lower=lower_v,
                middle=middle_v,
            )
        except Exception as e:
            logger.debug(f"[BB] calc error: {e}")
            return None


# ── Volume Profile ─────────────────────────────────────────────────────────────

@dataclass
class VolumeProfileResult:
    hvn_levels: List[float]         # high-volume nodes (support/resistance)
    lvn_levels: List[float]         # low-volume nodes (fast-travel zones)
    poc: float                      # point of control (highest-volume price)
    nearest_hvn_above: Optional[float]
    nearest_hvn_below: Optional[float]


class VolumeProfileCalculator:
    """
    Builds a simplified volume profile from OHLCV data.
    HVN = support/resistance; LVN = fast-travel zones between levels.
    """

    def __init__(self, n_bins: int = 30, lookback: int = 200,
                 hvn_pct: float = 0.70, lvn_pct: float = 0.30):
        self.n_bins   = n_bins
        self.lookback = lookback
        self.hvn_pct  = hvn_pct
        self.lvn_pct  = lvn_pct
        self._cache_key    = None
        self._cache_result: Optional[VolumeProfileResult] = None

    def calculate(self, df: pd.DataFrame) -> Optional[VolumeProfileResult]:
        if df is None or len(df) < 50:
            return None

        # Cache: only recompute when new bars arrive
        cache_key = (len(df), str(df.index[-1]))
        if cache_key == self._cache_key:
            return self._cache_result

        try:
            data = df.iloc[-self.lookback:].copy()
            price_min = float(data['low'].min())
            price_max = float(data['high'].max())
            if price_max <= price_min:
                return None

            bins = np.linspace(price_min, price_max, self.n_bins + 1)
            vol_by_bin = np.zeros(self.n_bins)

            for _, row in data.iterrows():
                tp = (row['high'] + row['low'] + row['close']) / 3
                idx = int(np.searchsorted(bins, tp, side='right')) - 1
                idx = max(0, min(idx, self.n_bins - 1))
                vol_by_bin[idx] += row['volume']

            bin_centers = (bins[:-1] + bins[1:]) / 2
            hvn_thresh = float(np.percentile(vol_by_bin, self.hvn_pct * 100))
            lvn_thresh = float(np.percentile(vol_by_bin, self.lvn_pct * 100))

            hvn_levels = sorted(float(bin_centers[i]) for i in range(self.n_bins)
                                 if vol_by_bin[i] >= hvn_thresh)
            lvn_levels = sorted(float(bin_centers[i]) for i in range(self.n_bins)
                                 if vol_by_bin[i] <= lvn_thresh)
            poc = float(bin_centers[int(np.argmax(vol_by_bin))])

            price = float(df['close'].iloc[-1])
            hvn_above = next((h for h in hvn_levels if h > price), None)
            hvn_below = next((h for h in reversed(hvn_levels) if h < price), None)

            result = VolumeProfileResult(
                hvn_levels=hvn_levels,
                lvn_levels=lvn_levels,
                poc=poc,
                nearest_hvn_above=hvn_above,
                nearest_hvn_below=hvn_below,
            )
            self._cache_key    = cache_key
            self._cache_result = result
            return result
        except Exception as e:
            logger.debug(f"[VP] calc error: {e}")
            return None


# ── CME Gap Detector ───────────────────────────────────────────────────────────

@dataclass
class CMEGapResult:
    gap_exists: bool
    gap_direction: Optional[str]   # 'UP' or 'DOWN'
    gap_pct: float
    gap_filled: bool
    friday_close: Optional[float]
    monday_open:  Optional[float]
    fill_target:  Optional[float]


class CMEGapDetector:
    """
    Detects open CME BTC futures gaps.
    CME trades Mon-Fri; weekend spot moves leave a gap at Monday open.
    BTC spot price reliably fills these gaps, making them a high-probability bias.
    """

    def __init__(self, min_gap_pct: float = 0.3):
        self.min_gap_pct = min_gap_pct
        self._cache_key    = None
        self._cache_result: Optional[CMEGapResult] = None

    def detect(self, df: pd.DataFrame) -> Optional[CMEGapResult]:
        if df is None or len(df) < 200:
            return None

        cache_key = (len(df), str(df.index[-1]))
        if cache_key == self._cache_key:
            return self._cache_result

        try:
            df_copy = df.copy()
            if df_copy.index.tz is None:
                df_copy.index = pd.to_datetime(df_copy.index).tz_localize('UTC')

            # Find most recent Friday close (weekday==4) before 22:01 UTC
            friday_close = None
            for ts in reversed(df_copy.index.tolist()):
                if ts.weekday() == 4 and ts.hour <= 22:
                    friday_close = float(df_copy.at[ts, 'close'])
                    break

            # Find subsequent Monday open (weekday==0)
            monday_open = None
            for ts in df_copy.index.tolist():
                if ts.weekday() == 0:
                    monday_open = float(df_copy.at[ts, 'open'])
                    break

            if friday_close is None or monday_open is None:
                result = CMEGapResult(False, None, 0.0, False, None, None, None)
                self._cache_key    = cache_key
                self._cache_result = result
                return result

            gap_pct = (monday_open - friday_close) / friday_close * 100
            if abs(gap_pct) < self.min_gap_pct:
                result = CMEGapResult(False, None, abs(gap_pct), False, friday_close, monday_open, None)
                self._cache_key    = cache_key
                self._cache_result = result
                return result

            gap_dir = 'UP' if monday_open > friday_close else 'DOWN'
            fill_target = friday_close

            current_price = float(df_copy['close'].iloc[-1])
            gap_filled = (current_price <= friday_close) if gap_dir == 'UP' else (current_price >= friday_close)

            result = CMEGapResult(
                gap_exists=not gap_filled,
                gap_direction=gap_dir,
                gap_pct=abs(gap_pct),
                gap_filled=gap_filled,
                friday_close=friday_close,
                monday_open=monday_open,
                fill_target=fill_target,
            )
            self._cache_key    = cache_key
            self._cache_result = result
            return result
        except Exception as e:
            logger.debug(f"[CME] detect error: {e}")
            return None


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
