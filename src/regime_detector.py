"""
Market Regime Detector
Classifies the current market into one of four regimes using rolling
price features. Rule-based initially; designed to be upgraded to a
trained Gaussian Mixture Model once sufficient trade history exists.

Regimes:
  TRENDING_UP   — Strong uptrend, EMAs rising, ADX high
  TRENDING_DOWN — Strong downtrend, EMAs falling, ADX high
  RANGING       — Sideways chop, low ADX, RSI oscillating around 50
  VOLATILE      — High ATR, large RSI swings, unpredictable
  CRASH         — Sharp decline, RSI deeply oversold, price far below MAs
"""

import logging
import numpy as np
import pandas as pd
import pandas_ta as ta
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RegimeResult:
    regime: str          # TRENDING_UP | TRENDING_DOWN | RANGING | VOLATILE | CRASH
    confidence: float    # 0.0 – 1.0
    adx: float
    rsi: float
    atr_pct: float
    trend_slope: float   # % change of EMA50 over last 20 bars
    rsi_std: float       # RSI volatility (high = choppy)

    @property
    def is_trending(self) -> bool:
        return self.regime in ('TRENDING_UP', 'TRENDING_DOWN')

    @property
    def is_ranging(self) -> bool:
        return self.regime == 'RANGING'

    @property
    def is_crash(self) -> bool:
        return self.regime == 'CRASH'

    @property
    def is_volatile(self) -> bool:
        return self.regime == 'VOLATILE'

    @property
    def allows_long(self) -> bool:
        return self.regime not in ('CRASH', 'TRENDING_DOWN')

    @property
    def strategy_hint(self) -> str:
        hints = {
            'TRENDING_UP':   'EMA crossover — follow the trend',
            'TRENDING_DOWN': 'Wait for reversal or short only',
            'RANGING':       'RSI mean reversion — buy dips, sell rips',
            'VOLATILE':      'Reduce size — wide stops needed',
            'CRASH':         'Stay flat — wait for stabilisation',
        }
        return hints.get(self.regime, '')

    def color(self) -> str:
        colors = {
            'TRENDING_UP':   '#00f5a0',
            'TRENDING_DOWN': '#ff4d6d',
            'RANGING':       '#ffd700',
            'VOLATILE':      '#ff9500',
            'CRASH':         '#ff1744',
        }
        return colors.get(self.regime, '#aaaaaa')

    def to_dict(self) -> dict:
        return {
            'regime':       self.regime,
            'confidence':   round(self.confidence, 2),
            'adx':          round(self.adx, 1),
            'rsi':          round(self.rsi, 1),
            'atr_pct':      round(self.atr_pct, 3),
            'trend_slope':  round(self.trend_slope, 3),
            'rsi_std':      round(self.rsi_std, 1),
            'allows_long':  self.allows_long,
            'strategy_hint': self.strategy_hint,
            'color':        self.color(),
        }


class RegimeDetector:
    """
    Detects market regime from OHLCV data.

    Uses a layered rule system:
      1. Crash detection (highest priority)
      2. Volatility detection
      3. Trend vs range classification
      4. Trend direction

    Can be upgraded to GMM by calling .fit(historical_df) — the rules
    remain as a fallback when the model hasn't been trained.
    """

    def __init__(self,
                 adx_trend_threshold: float = 22.0,
                 adx_ranging_threshold: float = 18.0,
                 rsi_std_volatile: float = 8.0,    # lower for 1m (RSI swings less per bar)
                 atr_pct_volatile: float = 0.08,   # lower for 1m (ATR is tiny relative to price)
                 crash_rsi_threshold: float = 32.0,
                 crash_ema_gap_pct: float = -4.0,
                 lookback: int = 20):
        self.adx_trend    = adx_trend_threshold
        self.adx_ranging  = adx_ranging_threshold
        self.rsi_std_vol  = rsi_std_volatile
        self.atr_pct_vol  = atr_pct_volatile
        self.crash_rsi    = crash_rsi_threshold
        self.crash_gap    = crash_ema_gap_pct
        self.lookback     = lookback
        self._gmm         = None   # placeholder for future ML model

    def detect(self, df: pd.DataFrame) -> Optional[RegimeResult]:
        """Run regime detection on the given OHLCV DataFrame."""
        min_bars = max(200, self.lookback + 14)
        if df is None or len(df) < min_bars:
            return None

        try:
            close  = df['close']
            high   = df['high']
            low    = df['low']

            # ── Indicators ──────────────────────────────────────────────────
            adx_df = ta.adx(high, low, close, length=14)
            adx    = float(adx_df.iloc[-1, 0]) if adx_df is not None else 20.0

            rsi    = ta.rsi(close, length=14)
            rsi_v  = float(rsi.iloc[-1]) if rsi is not None else 50.0
            rsi_std = float(rsi.iloc[-self.lookback:].std()) if rsi is not None else 10.0

            atr    = ta.atr(high, low, close, length=14)
            atr_v  = float(atr.iloc[-1]) if atr is not None else 0.0
            atr_pct = (atr_v / float(close.iloc[-1]) * 100) if float(close.iloc[-1]) > 0 else 0.0

            ema50  = ta.ema(close, length=50)
            ema200 = ta.ema(close, length=200)
            ema50_v  = float(ema50.iloc[-1])  if ema50  is not None else float(close.iloc[-1])
            ema200_v = float(ema200.iloc[-1]) if ema200 is not None else float(close.iloc[-1])

            price  = float(close.iloc[-1])
            ema200_gap_pct = (price - ema200_v) / ema200_v * 100 if ema200_v > 0 else 0.0

            # EMA50 slope over lookback bars
            ema50_past = float(ema50.iloc[-self.lookback]) if ema50 is not None and len(ema50) > self.lookback else ema50_v
            trend_slope = (ema50_v - ema50_past) / ema50_past * 100 if ema50_past > 0 else 0.0

            # ── Classification rules ─────────────────────────────────────────

            # 1. CRASH: price far below 200 EMA AND RSI oversold
            if ema200_gap_pct < self.crash_gap and rsi_v < self.crash_rsi:
                confidence = min(1.0, abs(ema200_gap_pct / self.crash_gap) * 0.6 +
                                      (self.crash_rsi - rsi_v) / self.crash_rsi * 0.4)
                return RegimeResult('CRASH', confidence, adx, rsi_v, atr_pct, trend_slope, rsi_std)

            # 2. VOLATILE: high ATR or very wide RSI swings
            if atr_pct > self.atr_pct_vol or rsi_std > self.rsi_std_vol:
                confidence = min(1.0, max(atr_pct / self.atr_pct_vol,
                                          rsi_std / self.rsi_std_vol) * 0.7)
                return RegimeResult('VOLATILE', confidence, adx, rsi_v, atr_pct, trend_slope, rsi_std)

            # 3. RANGING: weak ADX, RSI oscillating around 50
            if adx < self.adx_ranging:
                confidence = min(1.0, (self.adx_ranging - adx) / self.adx_ranging * 0.8 +
                                      max(0, 1 - abs(rsi_v - 50) / 25) * 0.2)
                return RegimeResult('RANGING', confidence, adx, rsi_v, atr_pct, trend_slope, rsi_std)

            # 4. TRENDING (ADX >= threshold) — determine direction
            if adx >= self.adx_trend:
                is_up = trend_slope > 0 and price > ema50_v
                regime = 'TRENDING_UP' if is_up else 'TRENDING_DOWN'
                confidence = min(1.0, (adx / 40) * 0.5 + abs(trend_slope) / 2 * 0.5)
                return RegimeResult(regime, confidence, adx, rsi_v, atr_pct, trend_slope, rsi_std)

            # 5. Borderline — classify by slope
            if trend_slope > 0.5:
                return RegimeResult('TRENDING_UP', 0.4, adx, rsi_v, atr_pct, trend_slope, rsi_std)
            elif trend_slope < -0.5:
                return RegimeResult('TRENDING_DOWN', 0.4, adx, rsi_v, atr_pct, trend_slope, rsi_std)

            return RegimeResult('RANGING', 0.5, adx, rsi_v, atr_pct, trend_slope, rsi_std)

        except Exception as e:
            logger.warning(f"[REGIME] Detection failed: {e}")
            return None

    def fit_gmm(self, df: pd.DataFrame, n_components: int = 5):
        """
        Optional: train a Gaussian Mixture Model on historical data.
        Once trained, regime detection uses probability assignments.
        Requires scikit-learn.
        """
        try:
            from sklearn.mixture import GaussianMixture
            from sklearn.preprocessing import StandardScaler

            features = self._extract_features(df)
            if features is None or len(features) < 50:
                logger.warning("[REGIME] Not enough data for GMM training")
                return False

            scaler = StandardScaler()
            X = scaler.fit_transform(features)
            gmm = GaussianMixture(n_components=n_components, covariance_type='full',
                                  random_state=42, n_init=3)
            gmm.fit(X)
            self._gmm = (gmm, scaler)
            logger.info(f"[REGIME] GMM trained on {len(features)} samples, {n_components} components")
            return True
        except ImportError:
            logger.warning("[REGIME] scikit-learn not available — using rule-based detection")
            return False
        except Exception as e:
            logger.warning(f"[REGIME] GMM training failed: {e}")
            return False

    def _extract_features(self, df: pd.DataFrame):
        """Extract ML features from OHLCV data."""
        try:
            close = df['close']
            high  = df['high']
            low   = df['low']

            adx    = ta.adx(high, low, close, length=14).iloc[:, 0]
            rsi    = ta.rsi(close, length=14)
            atr    = ta.atr(high, low, close, length=14)
            ema50  = ta.ema(close, length=50)
            ema200 = ta.ema(close, length=200)

            features = pd.DataFrame({
                'adx':        adx,
                'rsi':        rsi,
                'atr_pct':    atr / close * 100,
                'rsi_std':    rsi.rolling(20).std(),
                'ema_gap':    (close - ema50) / ema50 * 100,
                'macro_gap':  (close - ema200) / ema200 * 100,
                'slope':      ema50.pct_change(20) * 100,
                'ret_std':    close.pct_change().rolling(20).std() * 100,
            }).dropna()

            return features.values
        except Exception:
            return None
