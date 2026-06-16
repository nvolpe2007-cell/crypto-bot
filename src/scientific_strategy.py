"""
Simplified momentum strategy — Supertrend + CVD + OFI consensus.

Signal scoring (0-3):
  +1  Supertrend aligned (determines direction)
  +1  1-minute CVD (cumulative volume delta) agrees
  +1  OFI order book imbalance agrees

Score ≥ 2 → trade  (Supertrend + at least one other confirmation).
Score < 2 → hold.

Session filter: no new entries 23:00–02:00 UTC (dead hours, low liquidity).
Existing positions run freely through dead hours — only new entries blocked.

ATR exits (computed from signal at entry time):
  Initial stop loss:   entry ± 2.0 × ATR_14
  Initial take profit: entry ± 3.0 × ATR_14
  Chandelier trailing: trail at (highest_high – 2.5 × ATR) for longs,
                               (lowest_low  + 2.5 × ATR) for shorts
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import pandas_ta as ta

from .indicators import Signal, supertrend as compute_supertrend
from .orderflow_ws import OrderFlowWS

logger = logging.getLogger(__name__)

DEAD_HOUR_START = 23   # 23:00 UTC — no new entries from here …
DEAD_HOUR_END   = 2    # … until 02:00 UTC

ATR_SL_MULT    = 2.0   # stop loss   = entry ± (ATR × this)
ATR_TP_MULT    = 3.0   # take profit = entry ± (ATR × this)
ATR_TRAIL_MULT = 2.5   # chandelier  = peak ± (ATR × this)

MAX_SPREAD_PCT = 0.05  # skip entry if spread > 0.05% of price


@dataclass
class ScientificSignal:
    signal:     Signal
    confidence: float       # 70 for score=2, 100 for score=3 (for compat with entry gates)
    size_mult:  float       # 0.6 for score=2, 1.0 for score=3

    score: int = 0          # 0-3: the core decision variable

    # Sub-scores kept at 0 for backward-compat with journal / notifications
    ofi_score:       float = 0.0
    lead_lag_score:  float = 0.0
    regime_score:    float = 0.0
    rsi_score:       float = 0.0
    technical_score: float = 0.0
    funding_score:   float = 0.0

    ofi:          Optional[float] = None
    lead_lag_dir: Optional[str]   = None
    regime:       str             = 'UNKNOWN'
    rsi:          float           = 50.0
    adx:          float           = 20.0
    atr:          float           = 0.0
    close:        float           = 0.0
    ema_fast:     float           = 0.0
    ema_slow:     float           = 0.0
    volume_ratio: float           = 1.0
    funding_rate: Optional[float] = None

    consensus_votes: int       = 0
    data_stale:      bool      = False
    contradictions:  List[str] = field(default_factory=list)
    rationale:       Dict      = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.signal == Signal.BUY and self.size_mult > 0

    @property
    def is_sell(self) -> bool:
        return self.signal == Signal.SELL and self.size_mult > 0

    def stop_loss_pct(self) -> float:
        """ATR-based stop loss as % of entry price."""
        if self.atr > 0 and self.close > 0:
            return self.atr * ATR_SL_MULT / self.close * 100
        return 1.5   # fallback ~1.5%

    def take_profit_pct(self) -> float:
        """ATR-based initial take profit as % of entry price."""
        if self.atr > 0 and self.close > 0:
            return self.atr * ATR_TP_MULT / self.close * 100
        return 3.0   # fallback ~3%


def _size_multiplier(confidence: float) -> float:
    """Backward-compat size mapping used by paper_trading and other callers."""
    if confidence >= 100: return 1.0
    if confidence >= 70:  return 0.6
    return 0.0


def compute_position_size(confidence: float, equity: float) -> float:
    """Backward-compat sizing for paper_trading. Live trading uses BTC-tiered sizing."""
    BASE_PCT = 0.06
    MAX_PCT  = 0.15
    mult = _size_multiplier(confidence)
    if mult == 0:
        return 0.0
    return min(equity * BASE_PCT * mult, equity * MAX_PCT)


def _in_dead_hours() -> bool:
    """True between 23:00 and 02:00 UTC — low-volume window, no new entries."""
    hour = datetime.now(timezone.utc).hour
    return hour >= DEAD_HOUR_START or hour < DEAD_HOUR_END


class ScientificStrategy:
    def __init__(self,
                 lead_lag_min:   float = 0.003,   # ignored, kept for compat
                 min_confidence: float = 45.0):   # ignored, kept for compat
        self.ml_scorer = None   # kept for compat; not used by this strategy

    def evaluate(self,
                 df:           pd.DataFrame,
                 symbol:       str,
                 ofi_calc:     Optional[object]      = None,   # ignored (WS preferred)
                 lead_lag:     Optional[object]      = None,   # removed, ignored
                 regime:       str                   = 'UNKNOWN',
                 regime_conf:  float                 = 0.5,
                 funding_rate: Optional[float]       = None,
                 ofw:          Optional[OrderFlowWS] = None,
                 ) -> Optional['ScientificSignal']:
        if df is None or len(df) < 30:
            return None
        try:
            return self._evaluate(df, symbol, funding_rate, ofw)
        except Exception as e:
            logger.debug(f"[STRATEGY] {symbol} evaluate error: {e}")
            return None

    def _evaluate(self, df: pd.DataFrame, symbol: str,
                  funding_rate: Optional[float],
                  ofw: Optional[OrderFlowWS]) -> Optional['ScientificSignal']:

        if _in_dead_hours():
            logger.debug(f"[STRATEGY] {symbol} dead hours (23-02 UTC) — no new entries")
            return None

        price = float(df['close'].iloc[-1])

        # ATR_14 — drives all exit levels
        atr_s = ta.atr(df['high'], df['low'], df['close'], length=14)
        atr_v = (float(atr_s.iloc[-1])
                 if atr_s is not None and not pd.isna(atr_s.iloc[-1])
                 else price * 0.01)

        # Spread filter — skip if live book spread is too wide
        if ofw is not None:
            spread_pct = ofw.get_spread_pct(symbol)
            if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
                logger.debug(f"[STRATEGY] {symbol} spread={spread_pct:.4f}% > {MAX_SPREAD_PCT}% — skip")
                return None

        # RSI for informational logging only (no gate)
        rsi_s = ta.rsi(df['close'], length=14)
        rsi_v = (float(rsi_s.iloc[-1])
                 if rsi_s is not None and not pd.isna(rsi_s.iloc[-1])
                 else 50.0)

        # Signal 1: Supertrend — determines direction
        st_df   = compute_supertrend(df, period=10, multiplier=2.5)
        st_bull = bool(st_df['supertrend_bull'].iloc[-1])
        is_buy  = st_bull

        # Signal 2: CVD (cumulative volume delta) from WebSocket
        cvd_bull: Optional[bool] = ofw.get_cvd_trend(symbol) if ofw is not None else None

        # Signal 3: OFI (order book imbalance) from WebSocket
        obi: Optional[float] = ofw.get_obi(symbol) if ofw is not None else None

        # Scoring
        #   Supertrend always contributes +1 since it defines direction.
        #   CVD and OFI each add +1 when they agree with that direction.
        score    = 1   # Supertrend
        cvd_vote = False
        ofi_vote = False

        if is_buy:
            if cvd_bull is True:                score += 1; cvd_vote = True
            if obi is not None and obi > 0.55:  score += 1; ofi_vote = True
        else:
            if cvd_bull is False:               score += 1; cvd_vote = True
            if obi is not None and obi < 0.45:  score += 1; ofi_vote = True

        obi_str = f"{obi:.3f}" if obi is not None else "n/a"
        cvd_str = '↑' if cvd_bull else ('↓' if cvd_bull is False else '?')

        if score < 2:
            logger.debug(
                f"[STRATEGY] {symbol} score={score}/3 HOLD  "
                f"ST={'↑' if st_bull else '↓'} CVD={cvd_str} OBI={obi_str}"
            )
            return _hold(price, atr_v, obi, rsi_v)

        direction  = 'BUY' if is_buy else 'SELL'
        confidence = 70.0 if score == 2 else 100.0   # maps to LIVE_MIN_CONFIDENCE gate
        size_mult  = 0.6  if score == 2 else 1.0

        logger.info(
            f"[STRATEGY] {symbol} {direction}  score={score}/3  "
            f"ST={'↑' if st_bull else '↓'} "
            f"CVD={cvd_str}({'+' if cvd_vote else '-'}) "
            f"OBI={obi_str}({'+' if ofi_vote else '-'})  "
            f"ATR={atr_v:.2f}  RSI={rsi_v:.0f}"
        )

        return ScientificSignal(
            signal=Signal.BUY if is_buy else Signal.SELL,
            confidence=confidence,
            size_mult=size_mult,
            score=score,
            ofi=obi,
            rsi=rsi_v,
            atr=atr_v,
            close=price,
            ema_fast=price,
            ema_slow=price,
            funding_rate=funding_rate,
            consensus_votes=score,
            rationale={
                'direction':  direction,
                'score':      score,
                'confidence': confidence,
                'indicators': {
                    'supertrend': {'bull': st_bull, 'vote': True},
                    'cvd':        {'trend': cvd_str, 'vote': cvd_vote},
                    'ofi':        {'obi': obi_str, 'vote': ofi_vote},
                },
            },
        )


def _hold(price: float, atr: float, obi: Optional[float], rsi: float) -> ScientificSignal:
    return ScientificSignal(
        signal=Signal.HOLD, confidence=0.0, size_mult=0.0, score=0,
        atr=atr, close=price, ofi=obi, rsi=rsi,
    )


_hold_signal = _hold   # backward-compat alias
