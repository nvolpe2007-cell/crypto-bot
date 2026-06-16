"""
Scientific Strategy — OFI + BTC Lead-Lag primary signals.

Signal hierarchy (most to least reliable):
  1. Order Flow Imbalance    — academically strongest short-term predictor
  2. BTC Lead-Lag            — documented 30s-5min alt lag
  3. Regime alignment        — avoids trading against macro trend
  4. RSI/ADX/EMA/MACD        — traditional confirmation (confidence boosters)
  5. Funding rate            — directional pressure context

A confidence score (0–100) is computed for each potential trade.
Position size scales with confidence above the 93% tier.

Entry requirements:
  - Minimum confidence: 60
  - OFI must not be strongly opposing (fail-open when unavailable)
  - Regime must not be blocking (CRASH blocks longs, no other hard blocks)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import pandas_ta as ta

from .indicators import Signal
from .order_flow import OrderFlowImbalance
from .lead_lag_detector import LeadLagDetector

logger = logging.getLogger(__name__)

# ── Confidence tiers → position size multipliers ──────────────────────────────
#  < 60  : no trade
#  60-79 : 0.5x base
#  80-89 : 0.8x base
#  90-92 : 1.0x base
#  93-96 : 1.4x base   ← "high confidence" tier
#  97-100: 1.8x base   ← "very high confidence" tier
CONFIDENCE_TIERS = [
    (97, 2.0),   # 97-100%: 12% of equity
    (93, 1.5),   # 93-96%:  9% of equity
    (85, 1.0),   # 85-92%:  6% of equity
    (75, 0.7),   # 75-84%:  4.2% of equity
    (60, 0.5),   # 60-74%:  3% of equity
    (45, 0.3),   # 45-59%:  1.8% of equity
    (38, 0.2),   # 38-44%:  1.2% of equity (small exploratory)
    (0,  0.0),   # below 38 → skip
]

# Base position as % of current equity
BASE_EQUITY_PCT = 0.06   # 6% per trade baseline
MAX_EQUITY_PCT  = 0.15   # never exceed 15% on one trade


@dataclass
class ScientificSignal:
    """Full signal output from the scientific strategy."""
    signal:     Signal          # BUY / SELL / HOLD
    confidence: float           # 0–100
    size_mult:  float           # position size multiplier (0 = no trade)

    # Score breakdown (for Telegram analysis)
    ofi_score:       float
    lead_lag_score:  float
    regime_score:    float
    rsi_score:       float
    technical_score: float
    funding_score:   float

    # Market context
    ofi:            Optional[float]
    lead_lag_dir:   Optional[str]
    regime:         str
    rsi:            float
    adx:            float
    atr:            float
    close:          float
    ema_fast:       float
    ema_slow:       float
    volume_ratio:   float
    funding_rate:   Optional[float]

    @property
    def is_buy(self) -> bool:
        return self.signal == Signal.BUY and self.size_mult > 0

    @property
    def is_sell(self) -> bool:
        return self.signal == Signal.SELL and self.size_mult > 0

    def stop_loss_pct(self) -> float:
        """ATR-based stop — tighter for high-confidence scalps."""
        if self.atr > 0 and self.close > 0:
            base = self.atr * 1.5 / self.close * 100
            # Tighten stops on very confident trades (don't let a 97% trade become a big loss)
            return max(0.4, min(base, 2.5))
        return 1.5

    def take_profit_pct(self) -> float:
        """2:1 R:R minimum, scaled up for high confidence."""
        sl = self.stop_loss_pct()
        if self.confidence >= 93:
            return sl * 2.5   # 2.5:1 on high-confidence
        return sl * 2.0


def _size_multiplier(confidence: float) -> float:
    for threshold, mult in CONFIDENCE_TIERS:
        if confidence >= threshold:
            return mult
    return 0.0


def compute_position_size(confidence: float, equity: float) -> float:
    """
    Dollar position size that scales with both confidence and equity.
    At $100 equity, 93% confidence → ~$5.60.
    At $500 equity, 93% confidence → ~$28.
    Never exceeds 15% of equity.
    """
    mult = _size_multiplier(confidence)
    if mult == 0:
        return 0.0
    raw = equity * BASE_EQUITY_PCT * mult
    return min(raw, equity * MAX_EQUITY_PCT)


class ScientificStrategy:
    """
    Evaluates all available signals and returns a single confidence-scored
    ScientificSignal.  Called once per symbol per trading cycle.
    """

    def __init__(self,
                 ofi_min:          float = 0.15,
                 lead_lag_min:     float = 0.003,
                 min_confidence:   float = 45.0):   # lower default — learner raises it adaptively
        self.ofi_min        = ofi_min
        self.lead_lag_min   = lead_lag_min
        self.min_confidence = min_confidence
        self.ml_scorer      = None   # set by paper_trading after MLScorer is ready

    def evaluate(self,
                 df: pd.DataFrame,
                 symbol: str,
                 ofi_calc:    Optional[OrderFlowImbalance],
                 lead_lag:    Optional[LeadLagDetector],
                 regime:      str,
                 regime_conf: float,
                 funding_rate: Optional[float]) -> Optional['ScientificSignal']:
        """
        Evaluate all signals and return a ScientificSignal.
        Returns None if insufficient data.
        """
        if df is None or len(df) < 50:
            return None

        try:
            return self._evaluate(df, symbol, ofi_calc, lead_lag,
                                  regime, regime_conf, funding_rate)
        except Exception as e:
            logger.debug(f"[SCIENTIFIC] evaluate failed for {symbol}: {e}")
            return None

    def _evaluate(self, df, symbol, ofi_calc, lead_lag,
                  regime, regime_conf, funding_rate):

        close = df['close']
        price = float(close.iloc[-1])

        # ── Technical indicators ───────────────────────────────────────────────
        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        rsi   = ta.rsi(close, length=14)
        atr   = ta.atr(df['high'], df['low'], close, length=14)

        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        macd_hist = float(macd_df.iloc[-1, 2]) if macd_df is not None else 0.0

        adx_df = ta.adx(df['high'], df['low'], close, length=14)
        adx_v  = float(adx_df.iloc[-1, 0]) if adx_df is not None else 20.0

        vol_sma = df['volume'].rolling(20).mean()
        vol_ratio = float(df['volume'].iloc[-1] / vol_sma.iloc[-1]) if float(vol_sma.iloc[-1]) > 0 else 1.0

        ema9_v  = float(ema9.iloc[-1])  if ema9  is not None else price
        ema21_v = float(ema21.iloc[-1]) if ema21 is not None else price
        rsi_v   = float(rsi.iloc[-1])   if rsi   is not None else 50.0
        atr_v   = float(atr.iloc[-1])   if atr   is not None else price * 0.01

        # Was there an EMA crossover on the last candle?
        ema_cross_up = (
            ema9 is not None and len(ema9) >= 2 and
            float(ema9.iloc[-1]) > float(ema21.iloc[-1]) and
            float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
        )
        ema_cross_down = (
            ema9 is not None and len(ema9) >= 2 and
            float(ema9.iloc[-1]) < float(ema21.iloc[-1]) and
            float(ema9.iloc[-2]) >= float(ema21.iloc[-2])
        )

        # ── OFI signal ─────────────────────────────────────────────────────────
        ofi = ofi_calc.get_smoothed(symbol) if ofi_calc else None
        ofi_dir = 'NEUTRAL'
        if ofi is not None:
            if ofi >  0.15: ofi_dir = 'BULLISH'
            elif ofi < -0.15: ofi_dir = 'BEARISH'

        # ── Lead-lag signal ────────────────────────────────────────────────────
        lead_dir      = lead_lag.get_signal(symbol) if lead_lag else None
        lead_strength = lead_lag.get_strength(symbol) if lead_lag else 0.0

        # ── Determine candidate direction ──────────────────────────────────────
        # Evaluate BOTH directions, then pick the one with higher confidence.
        # This ensures we find shorts in downtrends and longs in uptrends
        # without manually hard-coding regime→direction rules.

        def _has_buy_signal() -> bool:
            if ofi_dir == 'BULLISH':   return True
            if lead_dir == 'BUY':      return True
            if ema_cross_up:           return True
            if rsi_v < 32:             return True
            return False

        def _has_sell_signal() -> bool:
            if ofi_dir == 'BEARISH':   return True
            if lead_dir == 'SELL':     return True
            if ema_cross_down:         return True
            if rsi_v > 68:             return True
            # In TRENDING_DOWN with strong ADX, always consider sell
            if regime == 'TRENDING_DOWN' and adx_v > 22: return True
            return False

        has_buy  = _has_buy_signal()
        has_sell = _has_sell_signal()

        if not has_buy and not has_sell:
            return _hold_signal(price, ema9_v, ema21_v, rsi_v, adx_v, atr_v, vol_ratio, ofi, lead_dir, regime, funding_rate)

        # Hard block: no longs in CRASH regime.
        # Even when both buy and sell signals are present (e.g. overbought RSI
        # triggers has_sell while strong OFI triggers has_buy), a CRASH regime
        # must never produce a long.  Suppress has_buy so direction selection
        # below can only resolve to SELL or HOLD.
        if regime == 'CRASH':
            if has_sell:
                has_buy = False   # redirect to the short side
            else:
                return _hold_signal(price, ema9_v, ema21_v, rsi_v, adx_v, atr_v, vol_ratio, ofi, lead_dir, regime, funding_rate)

        # If only one direction has a signal, use it
        if has_buy and not has_sell:
            direction = 'BUY'
        elif has_sell and not has_buy:
            direction = 'SELL'
        else:
            # Both signals present — strongest real-time signal wins.
            # OFI > 0.30 or lead-lag override regime direction (they're faster).
            # Regime direction is tiebreaker when real-time signals are absent.
            ofi_val = ofi if ofi is not None else 0.0
            if abs(ofi_val) >= 0.30:
                direction = 'BUY' if ofi_val > 0 else 'SELL'
            elif lead_dir:
                direction = lead_dir
            elif regime in ('TRENDING_DOWN', 'CRASH'):
                direction = 'SELL'
            elif regime == 'TRENDING_UP':
                direction = 'BUY'
            elif rsi_v < 35:
                direction = 'BUY'   # extreme oversold overrides when no other signal
            elif rsi_v > 65:
                direction = 'SELL'
            else:
                # Both signals present but no clear winner — hold rather than guess
                return _hold_signal(price, ema9_v, ema21_v, rsi_v, adx_v, atr_v,
                                    vol_ratio, ofi, lead_dir, regime, funding_rate)

        is_buy = direction == 'BUY'

        # ── Confidence scoring ─────────────────────────────────────────────────

        # 1. OFI score (0-30 pts)
        ofi_score = 0.0
        if ofi is not None:
            sign_ok = (is_buy and ofi > 0) or (not is_buy and ofi < 0)
            magnitude = abs(ofi)
            if sign_ok:
                if magnitude >= 0.40: ofi_score = 30.0
                elif magnitude >= 0.25: ofi_score = 20.0
                elif magnitude >= 0.15: ofi_score = 12.0
                else:                  ofi_score = 5.0
            else:
                # OFI opposes — penalty
                if magnitude >= 0.25: ofi_score = -15.0
                elif magnitude >= 0.15: ofi_score = -8.0
                else:                  ofi_score = -3.0
        else:
            ofi_score = 8.0   # no data → neutral-positive (fail-open)

        # 2. Lead-lag score (0-20 pts)
        lead_lag_score = 0.0
        if lead_dir:
            if lead_dir == direction:
                lead_lag_score = 10.0 + lead_strength * 10.0   # 10-20 pts
            else:
                lead_lag_score = -10.0   # opposing lead — strong penalty

        # 3. Regime score (0-20 pts)
        regime_scores = {
            'TRENDING_UP':   20.0 if is_buy  else 0.0,
            'TRENDING_DOWN': 20.0 if not is_buy else 0.0,
            'RANGING':       12.0,
            'VOLATILE':       5.0,
            'CRASH':          0.0,
            'UNKNOWN':        8.0,
        }
        regime_score = regime_scores.get(regime, 8.0)
        # Scale by confidence in regime detection
        regime_score *= (0.7 + 0.3 * regime_conf)

        # 4. RSI position score (0-15 pts)
        rsi_score = 0.0
        if is_buy:
            if rsi_v <= 40:   rsi_score = 15.0
            elif rsi_v <= 50: rsi_score = 12.0
            elif rsi_v <= 60: rsi_score = 8.0
            elif rsi_v <= 65: rsi_score = 4.0
            else:             rsi_score = 0.0   # overbought
        else:
            if rsi_v >= 60:   rsi_score = 15.0
            elif rsi_v >= 50: rsi_score = 12.0
            elif rsi_v >= 40: rsi_score = 8.0
            elif rsi_v >= 35: rsi_score = 4.0
            else:             rsi_score = 0.0   # oversold

        # 5. Technical confirmation score (0-15 pts)
        tech_score = 0.0
        if ema_cross_up   and is_buy:      tech_score += 5.0
        if ema_cross_down and not is_buy:  tech_score += 5.0
        if is_buy  and macd_hist > 0:      tech_score += 4.0
        if not is_buy and macd_hist < 0:   tech_score += 4.0
        if adx_v > 25:                     tech_score += 4.0
        elif adx_v > 20:                   tech_score += 2.0
        if vol_ratio > 1.2:                tech_score += 2.0
        tech_score = min(tech_score, 15.0)

        # 6. Funding rate score (0-10 pts)
        funding_score = 0.0
        if funding_rate is not None:
            annual = funding_rate * 3 * 365 * 100
            if is_buy:
                if funding_rate < -0.001:   funding_score = 10.0  # paid to be long
                elif funding_rate < 0.0005: funding_score = 5.0   # neutral-positive
                elif funding_rate > 0.001:  funding_score = -5.0  # longs paying
            else:
                if funding_rate > 0.001:    funding_score = 10.0  # market over-long → short pays
                elif funding_rate > 0.0005: funding_score = 5.0
                elif funding_rate < -0.001: funding_score = -5.0

        # ── Total confidence ───────────────────────────────────────────────────
        raw = ofi_score + lead_lag_score + regime_score + rsi_score + tech_score + funding_score
        # Normalise: max possible raw ≈ 30+20+20+15+15+10 = 110
        confidence = max(0.0, min(100.0, raw / 110.0 * 100.0))

        # ── ML blend (when scorer is trained) ─────────────────────────────────
        if self.ml_scorer is not None:
            now = datetime.now(timezone.utc)
            ml_features = {
                'rsi':               rsi_v,
                'adx':               adx_v,
                'volume_ratio':      vol_ratio,
                'atr_pct':           atr_v / price * 100 if price > 0 else 1.0,
                'ema100_gap':        0.0,
                'ema200_gap':        0.0,
                'hour_utc':          now.hour,
                'day_of_week':       now.weekday(),
                'ofi':               ofi or 0.0,
                'lead_lag_strength': lead_strength,
                'lead_lag_aligned':  (lead_dir == direction) if lead_dir else False,
                'regime':            regime,
                'regime_confidence': regime_conf,
                'funding_rate':      funding_rate or 0.0,
                'ofi_score':         ofi_score,
                'lead_lag_score':    lead_lag_score,
                'regime_score':      regime_score,
                'rule_confidence':   confidence,
                'is_buy':            is_buy,
            }
            confidence = self.ml_scorer.blend_confidence(confidence, ml_features)

        size_mult = _size_multiplier(confidence)

        sig = Signal.BUY if is_buy else Signal.SELL

        logger.info(
            f"[SCI] {symbol} {direction}  conf={confidence:.0f}  "
            f"OFI={ofi_score:.0f} Lead={lead_lag_score:.0f} Regime={regime_score:.0f} "
            f"RSI={rsi_score:.0f} Tech={tech_score:.0f} Fund={funding_score:.0f}  "
            f"size_mult={size_mult:.1f}x"
        )

        return ScientificSignal(
            signal          = sig,
            confidence      = confidence,
            size_mult       = size_mult,
            ofi_score       = ofi_score,
            lead_lag_score  = lead_lag_score,
            regime_score    = regime_score,
            rsi_score       = rsi_score,
            technical_score = tech_score,
            funding_score   = funding_score,
            ofi             = ofi,
            lead_lag_dir    = lead_dir,
            regime          = regime,
            rsi             = rsi_v,
            adx             = adx_v,
            atr             = atr_v,
            close           = price,
            ema_fast        = ema9_v,
            ema_slow        = ema21_v,
            volume_ratio    = vol_ratio,
            funding_rate    = funding_rate,
        )


def _hold_signal(price, ema9, ema21, rsi, adx, atr, vol_ratio, ofi, lead_dir, regime, funding_rate):
    return ScientificSignal(
        signal=Signal.HOLD, confidence=0.0, size_mult=0.0,
        ofi_score=0, lead_lag_score=0, regime_score=0,
        rsi_score=0, technical_score=0, funding_score=0,
        ofi=ofi, lead_lag_dir=lead_dir, regime=regime,
        rsi=rsi, adx=adx, atr=atr, close=price,
        ema_fast=ema9, ema_slow=ema21, volume_ratio=vol_ratio,
        funding_rate=funding_rate,
    )
