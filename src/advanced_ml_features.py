"""
Advanced Feature Engineering - Senior Quant Level

Goal: Replace indicator values with behavior-describing features

BAD traditional features (no longer used):
- RSI = 45 (just a number, no context)
- EMA9 > EMA21 (binary, no strength)
- MACD crossover (lagging)
- ADX = 22 (no direction or momentum)

GOOD probability-based features:
- RSI momentum (rate of change)
- Trend strength (how strong, accelerating?)
- Volatility expansion (increasing/decreasing)
- Momentum convergence/divergence
- Pullback depth (mean reversion potential)
- Volume flow (institutional participation)
- Order flow power (aggression of buyers/sellers)
- Distance from key levels (confluence)
"""

import numpy as np
import pandas as pd
import pandas_ta as ta
from scipy import stats
from typing import Dict, List


class BehaviorFeatures:
    """
    Process OHLCV into behavior-describing features

    Key insight: Features should describe MARKET BEHAVIOR,
    not just indicator values.
    """

    @staticmethod
    def compute_all_features(df: pd.DataFrame) -> Dict[str, float]:
        """
        Compute complete feature set from OHLCV

        Args:
            df: DataFrame with open, high, low, close, volume

        Returns:
            Dict of feature values ready for ML
        """

        if len(df) < 50:  # Need minimum data
            return {'error': 'insufficient_data'}

        features = {}

        # ── 1. MOMENTUM BEHAVIOR FEATURES ──────────────────────────────────

        # RSI momentum (rate of change) - more important than level
        rsi = ta.rsi(df['close'], length=14)
        rsi_mom = BehaviorFeatures._momentum(rsi, periods=5)
        features['rsi_momentum'] = float(rsi_mom.iloc[-1]) if rsi_mom is not None else 0.0

        # RSI position (normalized to 0-1, not 0-100)
        rsi_val = float(rsi.iloc[-1]) if rsi is not None else 50.0
        features['rsi_normalized'] = rsi_val / 100.0

        # RSI slope (strong momentum = better probability)
        rsi_slope = BehaviorFeatures._slope(rsi, lookback=7)
        features['rsi_slope'] = float(rsi_slope.iloc[-1]) if rsi_slope is not None else 0.0

        # RSI volatility (oscillation behavior means mean reversion)
        rsi_vol = rsi.rolling(20).std().iloc[-1]
        features['rsi_volatility'] = float(rsi_vol) if rsi_vol is not None else 0.0

        # ── 2. TREND BEHAVIOR FEATURES ────────────────────────────────────

        # Trend slope (strength, not just direction)
        ema50 = ta.ema(df['close'], length=50)
        ema200 = ta.ema(df['close'], length=200)

        if ema50 is not None and len(ema50) > 20:
            # Slope as % per bar
            ema50_slope = (ema50.iloc[-1] - ema50.iloc[-20]) / 20 / df['close'].iloc[-1]
            features['trend_slope_pct'] = float(ema50_slope) * 100

            # EMA acceleration
            ema50_slope_5 = (ema50.iloc[-1] - ema50.iloc[-5]) / 5 / df['close'].iloc[-1]
            features['trend_acceleration'] = (float(ema50_slope) - float(ema50_slope_5)) * 100

            # EMA distance ratio
            if ema200 is not None:
                ema_distance = (ema50.iloc[-1] - ema200.iloc[-1]) / ema200.iloc[-1]
                features['ema_distance'] = float(ema_distance) * 100
        else:
            features['trend_slope_pct'] = 0.0
            features['trend_acceleration'] = 0.0
            features['ema_distance'] = 0.0

        # MACD behavior (momentum convergence/divergence)
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd is not None and len(macd) > 0:
            hist = macd.iloc[:, 2]

            # Histogram slope (acceleration of momentum)
            hist_slope = BehaviorFeatures._slope(hist, lookback=5)
            features['macd_hist_slope'] = float(hist_slope.iloc[-1]) if hist_slope is not None else 0.0

            # Histogram expanding (confidence in momentum)
            hist_expanding = hist - hist.rolling(5).mean()
            features['macd_hist_expand'] = float(hist_expanding.iloc[-1]) if hist_expanding.iloc[-1] is not None else 0.0
        else:
            features['macd_hist_slope'] = 0.0
            features['macd_hist_expand'] = 0.0

        # ── 3. VOLATILITY BEHAVIOR FEATURES ───────────────────────────────

        # ATR trend (volatility expansion/contraction)
        atr = ta.atr(df['high'], df['low'], df['close'], length=14)

        if atr is not None and len(atr) > 5:
            # Volatility expansion ratio
            atr_sma_short = atr.rolling(5).mean()
            atr_sma_long = atr.rolling(20).mean()

            if atr_sma_long.iloc[-1] > 0:
                vol_expansion = atr_sma_short.iloc[-1] / atr_sma_long.iloc[-1]
                features['volatility_expansion'] = float(vol_expansion)

                # ATR slope (accelerating volatility)
                atr_slope = BehaviorFeatures._slope(atr, lookback=7)
                features['atr_slope'] = float(atr_slope.iloc[-1]) if atr_slope is not None else 0.0

                # ATR percentile (where is current volatility vs historical)
                atr_percentile = stats.percentileofscore(atr[-30:], atr.iloc[-1])
                features['atr_percentile'] = atr_percentile / 100.0

            else:
                features['volatility_expansion'] = 1.0
                features['atr_slope'] = 0.0
                features['atr_percentile'] = 0.5
        else:
            features['volatility_expansion'] = 1.0
            features['atr_slope'] = 0.0
            features['atr_percentile'] = 0.5

        # Bollinger squeeze (tight range = potential breakout)
        bb = ta.bbands(df['close'], length=20, std=2.0)
        if bb is not None:
            bandwidth = (bb.iloc[:, 0] - bb.iloc[:, 2]) / bb.iloc[:, 1]  # Width normalized
            squeeze = bandwidth < bandwidth.rolling(50).quantile(0.20)
            features['is_squeezed'] = float(squeeze.iloc[-1]) if squeeze.iloc[-1] is not None else 0.0
        else:
            features['is_squeezed'] = 0.0

        # ── 4. PULLBACK DEPTH ─────────────────────────────────────────────

        # Distance from recent high/low
        period = 20
        recent_max = df['high'].iloc[-period:].max()
        recent_min = df['low'].iloc[-period:].min()

        current = float(df['close'].iloc[-1])

        # Pullback from high (for longs)
        if recent_max > 0:
            pullback_from_high = (recent_max - current) / recent_max
            features['pullback_depth_high'] = max(0.0, min(1.0, float(pullback_from_high)))
        else:
            features['pullback_depth_high'] = 0.0

        # Rally from low (for shorts)
        if recent_min > 0:
            rally_from_low = (current - recent_min) / recent_min
            features['pullback_depth_low'] = max(0.0, min(1.0, float(rally_from_low)))
        else:
            features['pullback_depth_low'] = 0.0

        # ── 5. VOLUME BEHAVIOR ──────────────────────────────────────────

        # Volume trend (increasing = institution participation)
        vol = df['volume']
        vol_change = (vol.iloc[-1] - vol.iloc[-10]) / vol.iloc[-10] if vol.iloc[-10] > 0 else 0
        features['volume_trend'] = float(vol_change)

        # Volume momentum
        vol_mom = BehaviorFeatures._momentum(vol, periods=5)
        features['volume_momentum'] = float(vol_mom.iloc[-1]) if vol_mom.iloc[-1] is not None else 0.0

        # Volume to volatility ratio (participation vs movement)
        if atr_pct > 0:
            vol_atr_ratio = features['volume_trend'] / atr_pct
            features['volume_to_volatility'] = float(vol_atr_ratio)
        else:
            features['volume_to_volatility'] = 0.0

        # ── 6. MOMENTUM BEHAVIOR ────────────────────────────────────────

        # Rate of change (price velocity and acceleration)
        roc_short = ta.roc(df['close'], length=5)
        roc_long = ta.roc(df['close'], length=20)

        if roc_short is not None and roc_long is not None:
            features['price_momentum_5'] = float(roc_short.iloc[-1]) / 100 if roc_short.iloc[-1] is not None else 0.0
            features['price_momentum_20'] = float(roc_long.iloc[-1]) / 100 if roc_long.iloc[-1] is not None else 0.0

            # Momentum convergence/divergence
            if roc_short.iloc[-1] is not None and roc_long.iloc[-1] is not None:
                mom_divergence = roc_short.iloc[-1] - roc_long.iloc[-1]
                features['momentum_divergence'] = float(mom_divergence) / 100 if mom_divergence < 100 else 0.0
            else:
                features['momentum_divergence'] = 0.0
        else:
            features['price_momentum_5'] = 0.0
            features['price_momentum_20'] = 0.0
            features['momentum_divergence'] = 0.0

        # ── 7. ORDER FLOW POWER ───────────────────────────────────────────

        # Note: These would come from OFI calculator
        # For now, stub them out - they should be computed in order_flow.py
        # and added here
        features['ofi_power'] = 0.5  # Placeholder
        features['ofi_aligned'] = 0.0  # Placeholder

        # ── 8. MOMENTUM QUALITY ───────────────────────────────────────────

        # Higher timeframe alignment
        # Stub: would require multi-timeframe data
        features['higher_tf_trend'] = 0.0  # Placeholder
        features['higher_tf_momentum'] = 0.0

        # ── 9. SENTIMENT ───────────────────────────────────────────────

        # Funding rate benefit
        # Stub: would come from funding rate data
        features['funding_annualized'] = 0.0
        features['funding_favors_long'] = 0.5

        # Add all original indicator values for reference
        # (but these are less important for ML)
        features['_rsi_original'] = rsi_val / 100.0

        return features

    @staticmethod
    def _momentum(series: pd.Series, periods: int = 5) -> pd.Series:
        """Calculate momentum as rate of change"""
        return series.diff(periods) / series.shift(periods) * 100

    @staticmethod
    def _slope(series: pd.Series, lookback: int = 10) -> pd.Series:
        """Calculate slope using linear regression over lookback"""
        slopes = []

        for i in range(lookback, len(series)):
            y = series.iloc[i-lookback:i].values
            if len(y) < 3:
                slopes.append(0)
                continue

            x = np.arange(len(y))
            slope, _, _, _, _ = stats.linregress(x, y)
            slopes.append(slope)

        # Pad beginning
        result = [0] * lookback
        result.extend(slopes)

        return pd.Series(result, index=series.index)


class FeatureImportanceAnalyzer:
    """
    Analyzes which features actually matter for predicting success

    Key insight: Most features are noise. Find the 5-6 that actually matter.
    """

    def __init__(self):
        self.feature_correlation = {}

    def analyze_feature_importance(self, trades: list):
        """
        Analyze which features correlated with trade success

        Args:
            trades: List of trade objects with features and outcomes
        """
        if len(trades) < 10:
            return {}  # Not enough data

        # Collect features and outcomes
        features_list = []
        outcomes = []

        for trade in trades:
            if hasattr(trade, 'features'):
                features_list.append(trade.features())
                outcomes.append(1 if trade.won else 0)

        if not features_list:
            return {}

        # Convert to DataFrame
        df = pd.DataFrame(features_list)

        # Calculate correlation with outcome
        correlations = {}
        for col in df.columns:
            if col.startswith('_'):  # Skip meta features
                continue

            try:
                corr = df[col].corr(pd.Series(outcomes))
                if not pd.isna(corr):
                    correlations[col] = abs(corr)
            except:
                pass

        # Sort by absolute correlation
        sorted_corr = sorted(correlations.items(), key=lambda x: x[1], reverse=True)

        return sorted_corr

    def recommend_features_to_keep(self, trades: list, min_correlation: float = 0.05):
        """
        Recommend which features to keep based on predictive power

        Args:
            trades: List of trade objects
            min_correlation: Minimum correlation to be considered useful

        Returns:
            List of feature names that matter
        """
        correlations = self.analyze_feature_importance(trades)

        keep = []
        for feature, corr in correlations:
            if corr >= min_correlation:
                keep.append(feature)

        return keep

    def identify_redundant_features(self, trades: list):
        """
        Identify features that are highly correlated with each other
        (multicollinearity problem)

        Returns features that can be safely removed
        """
        if len(self.relevant_traders_dfs) < 10:
            return []

        df_features = pd.DataFrame([t.features() for t in trades])

        # Find pairs with correlation > 0.85
        redundant_pairs = []
        corr_matrix = df_features.corr()

        pair_to_remove = set()
        for i, col1 in enumerate(corr_matrix.columns):
            for col2 in list(corr_matrix.columns)[i+1:]:
                corr_val = abs(corr_matrix.loc[col1, col2])
                if corr_val > 0.85:
                    # Remove the least correlated to outcome
                    if col1 not in pair_to_remove and col2 not in pair_to_remove:
                        redundant_pairs.append((col1, col2, corr_val))
                        pair_to_remove.add(col2)  # Remove second of pair

        return list(pair_to_remove)


# Global instances
behavior_features = BehaviorFeatures()
feature_analyzer = FeatureImportanceAnalyzer()
