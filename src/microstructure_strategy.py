"""
Microstructure Strategy — OFI + CVD + Lead-Lag + Structure.
Implements the 4-layer confluence gate from the strategy spec.

Replaces ScientificStrategy but returns a MicrostructureSignal that extends
ScientificSignal so paper_trading.py doesn't need rewriting.

Strategy bets that price moves because one side of the order book is losing
the tug-of-war, visible in the book BEFORE price moves. OFI measures this.
Lead-Lag lets you enter the lagging instrument before it catches up.

4-Layer Confluence Gate (ALL must pass):
  1. OFI:       norm > 0.35, acceleration positive, persisted 3+ ticks, book depth OK
  2. CVD:       slope same direction as OFI, no absorption, fresh within 5s
  3. Lead-Lag:  lead instrument fired in entry direction within 2s window,
                lag instrument has NOT repriced (< 3bps from pre-fire price)
  4. Structure: 15m making higher lows (longs) or lower highs (shorts)

Exit rules (3-layer):
  - Signal stop (highest priority): OFI flips past -0.15 opposite direction
  - Price stop:  price moves 2.5× spread against you
  - Time stop:   open > 20 seconds without hitting target
  - T1 partial (50%): price moves 2.0× spread in favor → close half, BE stop
  - T2 full:    price moves 4.5× spread in favor
  - Signal fade: OFI drops below 0.10 after T1
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pandas_ta as ta

from .indicators import Signal
from .scientific_strategy import ScientificSignal, _size_multiplier
from .order_flow import OrderFlowImbalance
from .lead_lag_detector import LeadLagDetector
from .ofi_v2 import OFICalculatorV2, OFIState
from .cvd_tracker import CVDTracker, CVDState
from .lead_lag_v2 import LeadLagV2
from .kill_filters import KillFilterState

logger = logging.getLogger(__name__)

# ── Position sizing constants ─────────────────────────────────────────────────
_RISK_PER_TRADE    = 0.005   # 0.5% of equity per trade
_STOP_SPREAD_MULT  = 2.5     # stop = spread × 2.5
_T1_SPREAD_MULT    = 2.0     # T1 partial = spread × 2.0 in favor
_T2_SPREAD_MULT    = 4.5     # T2 full exit = spread × 4.5 in favor
_SIG_STOP_THRESH   = -0.15   # OFI crosses past this in opposite dir → signal stop
_SIG_FADE_THRESH   = 0.10    # OFI drops below this after T1 → fade exit
_TIME_STOP_SECS    = 90.0    # max open time before time-stop (raised from 20s to match 2s polling)

# Regime size multipliers
_REGIME_MULT = {
    'TRENDING_UP':    1.0,
    'TRENDING_DOWN':  1.0,
    'RANGING':        0.7,
    'VOLATILE':       0.5,
    'CRASH':          0.0,   # no new positions in crash
    'UNKNOWN':        0.7,
}

# OFI strength bonus
_OFI_STRONG_MULT = 1.2   # applied when |ofi_norm| > 0.55

# Structure check: sample 1m bars to approximate 15m (every 15 rows)
_STRUCTURE_SAMPLE  = 15
_STRUCTURE_LOWS_N  = 3    # need this many consecutive higher lows / lower highs

# Lead symbol
_LEAD_SYMBOL = 'BTC/USD'

# Fallback confidence when microstructure signals not yet warmed up
_FALLBACK_MIN_BARS = 10   # need this many OFI ticks before trusting v2


@dataclass
class MicrostructureSignal(ScientificSignal):
    """
    Extends ScientificSignal with microstructure-specific exit management fields.
    All ScientificSignal fields are inherited and populated for compatibility.
    """
    # Entry context for exit management
    spread_at_entry:    float = 0.0     # bid-ask spread in price units at entry
    ofi_norm_at_entry:  float = 0.0     # OFI norm value at entry time
    entry_time:         float = 0.0     # unix timestamp of entry

    # Exit state flags (mutated in-place by check_exit)
    t1_taken:           bool  = False   # 50% partial has been closed at T1
    stop_at_breakeven:  bool  = False   # stop moved to breakeven after T1

    # Kill filter result
    kill_reason:        str   = ''      # non-empty when a kill filter triggered

    # Confluence layer pass/fail breakdown
    ofi_passed:         bool  = False
    cvd_passed:         bool  = False
    lead_passed:        bool  = False
    structure_passed:   bool  = False


def _hold_micro(price: float, ofi: Optional[float], regime: str,
                funding_rate: Optional[float],
                kill_reason: str = '') -> MicrostructureSignal:
    """Build a HOLD MicrostructureSignal."""
    return MicrostructureSignal(
        signal=Signal.HOLD, confidence=0.0, size_mult=0.0,
        ofi_score=0.0, lead_lag_score=0.0, regime_score=0.0,
        rsi_score=0.0, technical_score=0.0, funding_score=0.0,
        ofi=ofi, lead_lag_dir=None, regime=regime,
        rsi=50.0, adx=20.0, atr=0.0, close=price,
        ema_fast=price, ema_slow=price, volume_ratio=1.0,
        funding_rate=funding_rate,
        kill_reason=kill_reason,
    )


class MicrostructureStrategy:
    """
    Full microstructure strategy with 4-layer confluence gate.

    Drop-in replacement for ScientificStrategy with matching evaluate() signature.
    Adds update_book() and update_candle() for real-time data feeds.
    Adds check_exit() for 3-layer exit management.
    """

    def __init__(self):
        # Per-symbol OFI v2 calculators
        self._ofi_calcs:   Dict[str, OFICalculatorV2] = {}
        # Per-symbol OFI states (latest)
        self.ofi_states:   Dict[str, OFIState]        = {}

        # Per-symbol CVD trackers
        self._cvd_trackers: Dict[str, CVDTracker]     = {}
        # Per-symbol CVD states (latest)
        self._cvd_states:   Dict[str, CVDState]       = {}

        # Single lead-lag v2 detector (BTC is the lead)
        self.lead_lag       = LeadLagV2()

        # Kill filter state (tracks rolling spread/depth history per symbol)
        self.kill_state     = KillFilterState()

        # For paper_trading.py compatibility (it sets strategy.ml_scorer = ml_scorer)
        self.ml_scorer      = None

        # Per-symbol price tracking (for lead-lag lag-price tracking)
        self._symbol_prices: Dict[str, float] = {}
        # Price of each lag symbol when the last lead fire event occurred
        self._lag_prices_at_fire: Dict[str, float] = {}

        # Last WS price time per symbol (fed from paper_trading loop)
        self._last_price_time: Dict[str, float] = {}

        # Current candle volume and SMA20 per symbol (fed from candle updates)
        self._candle_volume: Dict[str, float] = {}
        self._volume_sma20:  Dict[str, float] = {}

        # Per-symbol entry spread tracking (for exit checks on open positions)
        # symbol → MicrostructureSignal at entry
        self._position_signals: Dict[str, MicrostructureSignal] = {}

    # ── Real-time data feed methods ───────────────────────────────────────────

    def update_book(self, symbol: str, bids: list, asks: list, timestamp: float):
        """
        Called whenever order book is fetched for a symbol.

        Updates OFI v2, kill filter state, and lead-lag if symbol is BTC.
        Also updates last_price_time for WS staleness filter.

        Args:
            symbol:    trading pair ('BTC/USD', etc.)
            bids:      [[price, size], ...] order book bids
            asks:      [[price, size], ...] order book asks
            timestamp: unix time of this fetch
        """
        # Ensure calculator exists
        if symbol not in self._ofi_calcs:
            self._ofi_calcs[symbol] = OFICalculatorV2()

        # Update OFI v2
        ofi_state = self._ofi_calcs[symbol].update(bids, asks)
        self.ofi_states[symbol] = ofi_state

        # Update kill filter rolling history
        self.kill_state.update_book_history(symbol, bids, asks)

        # Update last price time
        self._last_price_time[symbol] = timestamp

        # If this is the lead instrument, update lead-lag with new OFI
        if symbol == _LEAD_SYMBOL and ofi_state:
            btc_price = self._symbol_prices.get(symbol, 0.0)
            if btc_price > 0:
                fire_event = self.lead_lag.update_lead(ofi_state.ofi_norm, btc_price)
                if fire_event is not None:
                    # Record lag prices at fire time for repricing check
                    for sym, price in self._symbol_prices.items():
                        if sym != _LEAD_SYMBOL:
                            self._lag_prices_at_fire[sym] = price

    def update_price(self, symbol: str, price: float):
        """Update the current mid price for a symbol. Call on each WS tick."""
        self._symbol_prices[symbol] = price
        self._last_price_time[symbol] = time.time()

    def update_candle(self, symbol: str, candle):
        """
        Called on each confirmed candle close.

        Updates CVD tracker. candle can be a CandleClose namedtuple/dataclass
        with attributes: open, high, low, close, volume, timestamp
        or a dict with the same keys.

        Args:
            symbol: trading pair
            candle: candle data object or dict
        """
        if symbol not in self._cvd_trackers:
            self._cvd_trackers[symbol] = CVDTracker(symbol=symbol)

        # Support both attribute and dict access
        if isinstance(candle, dict):
            o    = float(candle.get('open',   candle.get('o', 0)))
            c    = float(candle.get('close',  candle.get('c', 0)))
            h    = float(candle.get('high',   candle.get('h', 0)))
            lo   = float(candle.get('low',    candle.get('l', 0)))
            vol  = float(candle.get('volume', candle.get('v', 0)))
            ts   = float(candle.get('timestamp', time.time()))
        else:
            o    = float(getattr(candle, 'open',      0))
            c    = float(getattr(candle, 'close',     0))
            h    = float(getattr(candle, 'high',      0))
            lo   = float(getattr(candle, 'low',       0))
            vol  = float(getattr(candle, 'volume',    0))
            ts   = float(getattr(candle, 'timestamp', time.time()))

        cvd_state = self._cvd_trackers[symbol].update(o, c, h, lo, vol, ts)
        self._cvd_states[symbol] = cvd_state

        # Track current candle volume (for whale filter)
        self._candle_volume[symbol] = vol

    def update_volume_sma(self, symbol: str, sma20: float):
        """Update the 20-bar volume SMA for the whale print filter."""
        self._volume_sma20[symbol] = sma20

    # ── Main evaluate() — matches ScientificStrategy.evaluate() signature ─────

    def evaluate(self,
                 df:           pd.DataFrame,
                 symbol:       str,
                 ofi_calc:     Optional[OrderFlowImbalance],
                 lead_lag_old: Optional[LeadLagDetector],
                 regime:       str,
                 regime_conf:  float,
                 funding_rate: Optional[float]) -> Optional['MicrostructureSignal']:
        """
        Evaluate all microstructure signals and return a MicrostructureSignal.

        Signature matches ScientificStrategy.evaluate() for drop-in compatibility.
        Uses OFI v2 data when available, falls back to ofi_calc for legacy compat.

        Args:
            df:           OHLCV DataFrame (≥50 bars)
            symbol:       trading pair
            ofi_calc:     legacy OFI calculator (fallback)
            lead_lag_old: legacy lead-lag detector (fallback / ignored for new logic)
            regime:       regime string from RegimeDetector
            regime_conf:  confidence in regime classification
            funding_rate: current funding rate

        Returns:
            MicrostructureSignal or None if insufficient data.
        """
        if df is None or len(df) < 50:
            return None

        try:
            return self._evaluate(df, symbol, ofi_calc, lead_lag_old,
                                  regime, regime_conf, funding_rate)
        except Exception as e:
            logger.debug(f"[MICRO] evaluate failed for {symbol}: {e}", exc_info=True)
            return None

    def _evaluate(self, df, symbol, ofi_calc, lead_lag_old,
                  regime, regime_conf, funding_rate):
        """Internal evaluate implementation."""
        price = float(df['close'].iloc[-1])

        # ── Technical indicators (for ScientificSignal compat fields) ─────────
        close = df['close']
        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        rsi   = ta.rsi(close, length=14)
        atr   = ta.atr(df['high'], df['low'], close, length=14)
        adx_df = ta.adx(df['high'], df['low'], close, length=14)

        ema9_v  = float(ema9.iloc[-1])   if ema9  is not None else price
        ema21_v = float(ema21.iloc[-1])  if ema21 is not None else price
        rsi_v   = float(rsi.iloc[-1])    if rsi   is not None else 50.0
        atr_v   = float(atr.iloc[-1])    if atr   is not None else price * 0.01
        adx_v   = float(adx_df.iloc[-1, 0]) if adx_df is not None else 20.0

        vol_sma = df['volume'].rolling(20).mean()
        vol_ratio = (float(df['volume'].iloc[-1]) / float(vol_sma.iloc[-1])
                     if float(vol_sma.iloc[-1]) > 0 else 1.0)

        # ── Get current OFI v2 state ──────────────────────────────────────────
        ofi_state = self.ofi_states.get(symbol)
        has_ofi_v2 = (ofi_state is not None and
                      ofi_state.ticks_above_threshold >= 0)  # any data is usable

        # Fall back to legacy OFI for the ofi field in the signal
        legacy_ofi = ofi_calc.get_smoothed(symbol) if ofi_calc else None
        ofi_norm   = ofi_state.ofi_norm if ofi_state else (legacy_ofi or 0.0)

        # ── Kill filters ──────────────────────────────────────────────────────
        # Gather data needed for kill filters
        bids = []
        asks = []
        last_price_time = self._last_price_time.get(symbol, 0.0)
        candle_volume   = self._candle_volume.get(symbol, 0.0)
        volume_sma20    = self._volume_sma20.get(symbol, float(vol_sma.iloc[-1]))

        # Get funding opps from state.json
        funding_opps = []
        try:
            from .state import read_state
            state = read_state()
            funding_opps = state.get('funding_opportunities', [])
        except Exception:
            pass

        daily_pnl_pct = 0.0  # paper_trading updates this via equity tracking
        if funding_rate is not None:
            # Use funding_rate as proxy if state unavailable
            funding_opps_proxy = []
        else:
            funding_opps_proxy = funding_opps

        is_killed, kill_reason = self.kill_state.check(
            symbol=symbol,
            bids=bids,      # empty when no book update was done yet — filters gracefully skip
            asks=asks,
            last_price_time=last_price_time,
            candle_volume=candle_volume,
            volume_sma20=volume_sma20,
            funding_opportunities=funding_opps,
            daily_pnl_pct=daily_pnl_pct,
        )

        if is_killed:
            logger.debug(f"[MICRO] {symbol} killed: {kill_reason}")
            return _hold_micro(price, legacy_ofi, regime, funding_rate, kill_reason)

        # ── Check if we have enough OFI v2 data to use microstructure gate ───
        ofi_v2_ready = (ofi_state is not None and
                        len(self._ofi_calcs.get(symbol, OFICalculatorV2())._ticks)
                        >= _FALLBACK_MIN_BARS)

        if not ofi_v2_ready:
            # Fall back to legacy confidence scoring while warming up
            return self._fallback_signal(
                df, symbol, price, ofi_calc, lead_lag_old,
                regime, regime_conf, funding_rate,
                ema9_v, ema21_v, rsi_v, atr_v, adx_v, vol_ratio, legacy_ofi,
            )

        # ── CVD state ─────────────────────────────────────────────────────────
        cvd_state = self._cvd_states.get(symbol)

        # ── Lead-lag check ─────────────────────────────────────────────────────
        lead_dir = self.lead_lag.get_direction()   # 1, -1, or 0

        # ── Run confluence gate for LONG and SHORT ─────────────────────────────
        long_ok, long_checks  = self._confluence_gate(symbol, 1,  ofi_state, cvd_state, df)
        short_ok, short_checks = self._confluence_gate(symbol, -1, ofi_state, cvd_state, df)

        if not long_ok and not short_ok:
            return _hold_micro(price, legacy_ofi, regime, funding_rate)

        # Pick the direction that passed
        if long_ok and short_ok:
            # Both pass — shouldn't happen (OFI has a single direction)
            # Use OFI direction as tiebreaker
            is_buy = ofi_state.direction >= 0
        elif long_ok:
            is_buy = True
        else:
            is_buy = False

        # Hard block: CRASH regime prevents longs
        if is_buy and regime == 'CRASH':
            return _hold_micro(price, legacy_ofi, regime, funding_rate)

        checks = long_checks if is_buy else short_checks
        direction = 1 if is_buy else -1

        # ── Compute spread for exit management ───────────────────────────────
        spread = self._estimate_spread(df, symbol, atr_v)

        # ── Confidence scoring (microstructure-based) ─────────────────────────
        # OFI score: up to 30 pts
        ofi_score = self._score_ofi(ofi_state, is_buy)

        # CVD score: up to 20 pts
        cvd_score = self._score_cvd(cvd_state, direction)

        # Lead-lag score: up to 20 pts
        lead_lag_score = self._score_lead_lag(lead_dir, direction)

        # Structure score: up to 20 pts
        structure_ok = checks.get('structure', False)
        structure_score = 20.0 if structure_ok else 0.0

        # Regime score: up to 10 pts
        regime_score = self._score_regime(regime, is_buy, regime_conf)

        # Total: max = 30+20+20+20+10 = 100
        confidence = max(0.0, min(100.0,
                         ofi_score + cvd_score + lead_lag_score + structure_score + regime_score))

        size_mult = _size_multiplier(confidence)

        # Apply regime multiplier to size_mult (not confidence)
        regime_mult = _REGIME_MULT.get(regime, 0.7)
        if ofi_state and abs(ofi_state.ofi_norm) > 0.55:
            regime_mult = min(regime_mult * _OFI_STRONG_MULT, 2.0)
        size_mult *= regime_mult

        sig = Signal.BUY if is_buy else Signal.SELL

        lead_dir_str = 'BUY' if lead_dir > 0 else ('SELL' if lead_dir < 0 else None)

        logger.info(
            f"[MICRO] {symbol} {'LONG' if is_buy else 'SHORT'}  "
            f"conf={confidence:.0f}  "
            f"OFI={ofi_score:.0f} CVD={cvd_score:.0f} Lead={lead_lag_score:.0f} "
            f"Struc={structure_score:.0f} Reg={regime_score:.0f}  "
            f"size_mult={size_mult:.2f}  spread={spread:.4f}"
        )

        return MicrostructureSignal(
            # ScientificSignal base fields
            signal          = sig,
            confidence      = confidence,
            size_mult       = size_mult,
            ofi_score       = ofi_score,
            lead_lag_score  = lead_lag_score,
            regime_score    = regime_score,
            rsi_score       = 0.0,            # not used in microstructure
            technical_score = structure_score,
            funding_score   = 0.0,
            ofi             = legacy_ofi,
            lead_lag_dir    = lead_dir_str,
            regime          = regime,
            rsi             = rsi_v,
            adx             = adx_v,
            atr             = atr_v,
            close           = price,
            ema_fast        = ema9_v,
            ema_slow        = ema21_v,
            volume_ratio    = vol_ratio,
            funding_rate    = funding_rate,
            # Microstructure-specific fields
            spread_at_entry = spread,
            ofi_norm_at_entry = ofi_norm,
            entry_time      = time.time(),
            kill_reason     = '',
            ofi_passed      = checks.get('ofi', False),
            cvd_passed      = checks.get('cvd', False),
            lead_passed     = checks.get('lead', False),
            structure_passed = checks.get('structure', False),
        )

    # ── Exit management ───────────────────────────────────────────────────────

    def check_exit(self,
                   symbol:             str,
                   position,           # PaperPosition object
                   current_price:      float,
                   current_ofi_norm:   float,
                   time_open_seconds:  float) -> Tuple[Optional[str], Optional[str]]:
        """
        Check all exit conditions for an open position.

        Returns (exit_reason, exit_type) if an exit is triggered, else (None, None).

        exit_type values:
          'FULL'      — close entire position (signal stop, price stop, time stop, T2, fade)
          'PARTIAL'   — close 50% (T1 partial)

        exit_reason values:
          'SIGNAL_STOP'  — OFI flipped hard against us
          'PRICE_STOP'   — price moved 2.5× spread against entry
          'TIME_STOP'    — open too long without reaching target
          'T1_PARTIAL'   — first target hit; close half
          'T2'           — second target hit; close all
          'SIGNAL_FADE'  — OFI faded below 0.10 after T1
        """
        if position is None:
            return None, None

        sig = position.entry_signal
        if not isinstance(sig, MicrostructureSignal):
            # Legacy position — no microstructure exits
            return None, None

        side      = position.side   # 'buy' or 'short'
        entry_px  = position.entry_price
        spread    = sig.spread_at_entry
        direction = 1 if side == 'buy' else -1

        # Avoid division by zero
        if spread <= 0:
            spread = entry_px * 0.0001   # 1 bp as fallback

        # Price move relative to entry (positive = favorable for the position)
        if side == 'buy':
            price_delta = current_price - entry_px
        else:
            price_delta = entry_px - current_price

        # ── 1. Signal stop (highest priority) ─────────────────────────────────
        # OFI crossed past -0.15 in the OPPOSITE direction of our position
        opposing_ofi = current_ofi_norm * direction   # negative = OFI opposing us
        if opposing_ofi < _SIG_STOP_THRESH:
            logger.info(
                f"[MICRO EXIT] {symbol} SIGNAL_STOP  "
                f"ofi={current_ofi_norm:+.3f}  opposing={opposing_ofi:+.3f}  "
                f"threshold={_SIG_STOP_THRESH}"
            )
            return 'SIGNAL_STOP', 'FULL'

        # ── 2. Price stop ──────────────────────────────────────────────────────
        stop_distance = spread * _STOP_SPREAD_MULT
        if price_delta < -stop_distance:
            logger.info(
                f"[MICRO EXIT] {symbol} PRICE_STOP  "
                f"delta={price_delta:.4f}  stop={-stop_distance:.4f}  "
                f"spread={spread:.4f}"
            )
            return 'PRICE_STOP', 'FULL'

        # ── 3. T1 partial (first, before T2, so we can mark t1_taken) ─────────
        t1_distance = spread * _T1_SPREAD_MULT
        if not sig.t1_taken and price_delta >= t1_distance:
            logger.info(
                f"[MICRO EXIT] {symbol} T1_PARTIAL  "
                f"delta={price_delta:.4f}  t1={t1_distance:.4f}"
            )
            sig.t1_taken = True
            sig.stop_at_breakeven = True
            return 'T1_PARTIAL', 'PARTIAL'

        # ── 4. Breakeven stop (after T1) ──────────────────────────────────────
        if sig.stop_at_breakeven and price_delta < 0:
            logger.info(f"[MICRO EXIT] {symbol} BE_STOP after T1  delta={price_delta:.4f}")
            return 'PRICE_STOP', 'FULL'

        # ── 5. T2 full exit ───────────────────────────────────────────────────
        t2_distance = spread * _T2_SPREAD_MULT
        if price_delta >= t2_distance:
            logger.info(
                f"[MICRO EXIT] {symbol} T2  "
                f"delta={price_delta:.4f}  t2={t2_distance:.4f}"
            )
            return 'T2', 'FULL'

        # ── 6. Signal fade (after T1) ─────────────────────────────────────────
        if sig.t1_taken and abs(current_ofi_norm) < _SIG_FADE_THRESH:
            logger.info(
                f"[MICRO EXIT] {symbol} SIGNAL_FADE after T1  "
                f"ofi={current_ofi_norm:+.3f}  threshold={_SIG_FADE_THRESH}"
            )
            return 'SIGNAL_FADE', 'FULL'

        # ── 7. Time stop ──────────────────────────────────────────────────────
        if time_open_seconds > _TIME_STOP_SECS:
            # Only apply if we haven't reached T1 yet (otherwise let it run)
            if not sig.t1_taken:
                logger.info(
                    f"[MICRO EXIT] {symbol} TIME_STOP  "
                    f"open={time_open_seconds:.1f}s  threshold={_TIME_STOP_SECS}s"
                )
                return 'TIME_STOP', 'FULL'

        return None, None

    # ── Position sizing ───────────────────────────────────────────────────────

    def compute_size(self,
                     equity:     float,
                     ofi_norm:   float,
                     regime:     str,
                     spread:     float,
                     price:      float) -> float:
        """
        Compute position size in USD.

        Formula from spec:
          stop_distance = spread × 2.5 (in price units)
          size_units    = (equity × risk_per_trade) / stop_distance
          size_usd      = size_units × price

        Then apply regime multipliers.

        Args:
            equity:   current account equity in USD
            ofi_norm: current OFI norm (for strength multiplier)
            regime:   current regime string
            spread:   bid-ask spread in price units
            price:    current asset price

        Returns:
            float: position size in USD (0 if untradeably small)
        """
        if spread <= 0 or price <= 0 or equity <= 0:
            return 0.0

        stop_distance = spread * _STOP_SPREAD_MULT
        if stop_distance <= 0:
            return 0.0

        size_units = (equity * _RISK_PER_TRADE) / stop_distance
        size_usd   = size_units * price

        # Apply regime multiplier
        regime_mult = _REGIME_MULT.get(regime, 0.7)

        # OFI strength bonus
        if abs(ofi_norm) > 0.55:
            regime_mult = min(regime_mult * _OFI_STRONG_MULT, 2.0)

        size_usd *= regime_mult

        # Safety cap: never risk more than 3% of equity in a single trade
        max_size = equity * 0.03
        size_usd = min(size_usd, max_size)

        logger.debug(
            f"[MICRO SIZE] equity=${equity:.2f}  spread={spread:.4f}  "
            f"stop_dist={stop_distance:.4f}  regime_mult={regime_mult:.2f}  "
            f"size=${size_usd:.2f}"
        )
        return size_usd

    # ── Confluence gate ───────────────────────────────────────────────────────

    def _confluence_gate(self,
                          symbol:    str,
                          direction: int,     # 1=long, -1=short
                          ofi_state: Optional[OFIState],
                          cvd_state: Optional[CVDState],
                          df:        pd.DataFrame) -> Tuple[bool, dict]:
        """
        Run all 4 confluence layers for a given direction.

        Returns (all_pass, checks_dict) where checks_dict has keys:
          ofi, cvd, lead, structure → bool for each layer.
        """
        checks = {
            'ofi':       False,
            'cvd':       False,
            'lead':      False,
            'structure': False,
        }

        # ── Layer 1: OFI ──────────────────────────────────────────────────────
        if ofi_state is not None:
            ofi_dir_match = (direction > 0 and ofi_state.ofi_norm > 0) or \
                            (direction < 0 and ofi_state.ofi_norm < 0)
            checks['ofi'] = (
                abs(ofi_state.ofi_norm) >= 0.35 and
                ofi_state.ofi_accel > 0 and
                ofi_state.ticks_above_threshold >= 3 and
                ofi_state.depth > 0.01 and
                ofi_dir_match
            )
        else:
            # No OFI v2 data yet — gate cannot pass without it
            checks['ofi'] = False

        if not checks['ofi']:
            return False, checks

        # ── Layer 2: CVD ──────────────────────────────────────────────────────
        if cvd_state is not None:
            tracker = self._cvd_trackers.get(symbol)
            cvd_dir_match = (cvd_state.cvd_direction == direction)
            price_ok      = cvd_state.price_responding
            # "fresh within 5s" — using candle count proxy: aligned within last 5 candles
            freshness_ok  = cvd_state.seconds_since_aligned < 300   # 5 candles × 60s
            checks['cvd'] = cvd_dir_match and price_ok and freshness_ok
        else:
            # No CVD data yet — allow pass (fail-open while warming up)
            checks['cvd'] = True

        if not checks['cvd']:
            return False, checks

        # ── Layer 3: Lead-Lag ─────────────────────────────────────────────────
        # BTC should have fired in the entry direction within the 2s window
        fire = self.lead_lag.get_fire_event()
        if fire is not None and not self.lead_lag.is_expired():
            lead_dir_match = (fire.fire_direction == direction)
            # Check lag repricing
            lag_fire_price = self._lag_prices_at_fire.get(symbol, 0.0)
            lag_curr_price = self._symbol_prices.get(symbol, 0.0)
            if lag_fire_price > 0 and lag_curr_price > 0:
                move_bps = abs(lag_curr_price - lag_fire_price) / lag_fire_price * 10000
                not_repriced = move_bps < 3.0
            else:
                not_repriced = True   # can't measure, allow
            checks['lead'] = lead_dir_match and not_repriced
        else:
            # No active lead-lag signal — this layer FAILS (it's a required gate)
            # Per spec: "lead instrument fired in entry direction within lag window (2s)"
            # However, when BTC is the symbol itself, this check doesn't apply
            if symbol == _LEAD_SYMBOL:
                checks['lead'] = True   # BTC doesn't need a BTC lead-lag signal
            else:
                checks['lead'] = False

        if not checks['lead']:
            return False, checks

        # ── Layer 4: 15m Structure ────────────────────────────────────────────
        checks['structure'] = self._check_15m_structure(df, direction)

        # All 4 layers must pass
        all_pass = all(checks.values())
        return all_pass, checks

    def _check_15m_structure(self, df: pd.DataFrame, direction: int) -> bool:
        """
        Check 15-minute structure: approximate by sampling every 15 1m bars.

        For longs (direction=1):  last 3 sampled lows must each be higher
        For shorts (direction=-1): last 3 sampled highs must each be lower

        Returns True if structure aligns with direction, or if insufficient data.
        """
        if len(df) < _STRUCTURE_SAMPLE * _STRUCTURE_LOWS_N + _STRUCTURE_SAMPLE:
            # Not enough bars — be permissive (fail-open)
            return True

        try:
            # Sample every 15 bars to approximate 15m candles from 1m data
            sampled = df.iloc[::_STRUCTURE_SAMPLE]
            if len(sampled) < _STRUCTURE_LOWS_N + 1:
                return True

            recent = sampled.iloc[-(  _STRUCTURE_LOWS_N + 1):]

            if direction > 0:
                # Long: each successive 15m low should be higher than the previous
                lows = list(recent['low'])
                return all(lows[i] > lows[i - 1] for i in range(1, len(lows)))
            else:
                # Short: each successive 15m high should be lower than the previous
                highs = list(recent['high'])
                return all(highs[i] < highs[i - 1] for i in range(1, len(highs)))

        except Exception as e:
            logger.debug(f"[MICRO] Structure check failed: {e}")
            return True   # fail-open on error

    # ── Scoring helpers ───────────────────────────────────────────────────────

    def _score_ofi(self, ofi_state: Optional[OFIState], is_buy: bool) -> float:
        """Score OFI contribution (0-30 pts)."""
        if ofi_state is None:
            return 8.0   # neutral-positive (fail-open)

        norm = ofi_state.ofi_norm
        direction_match = (is_buy and norm > 0) or (not is_buy and norm < 0)
        abs_norm = abs(norm)

        if direction_match:
            if abs_norm >= 0.55:   return 30.0
            if abs_norm >= 0.45:   return 25.0
            if abs_norm >= 0.35:   return 20.0
            if abs_norm >= 0.25:   return 12.0
            return 5.0
        else:
            if abs_norm >= 0.35:   return -15.0
            if abs_norm >= 0.25:   return -8.0
            return -3.0

    def _score_cvd(self, cvd_state: Optional[CVDState], direction: int) -> float:
        """Score CVD contribution (0-20 pts)."""
        if cvd_state is None:
            return 10.0   # no data — neutral

        dir_match = (cvd_state.cvd_direction == direction)
        responding = cvd_state.price_responding

        if dir_match and responding:
            # Scale by slope magnitude (proxy for conviction)
            slope_pts = min(10.0, abs(cvd_state.cvd_slope) * 0.1)
            return 10.0 + slope_pts
        elif dir_match and not responding:
            return 5.0   # CVD aligned but absorbed
        elif not dir_match:
            return -5.0  # CVD opposing
        return 0.0

    def _score_lead_lag(self, lead_dir: int, direction: int) -> float:
        """Score lead-lag contribution (0-20 pts)."""
        if lead_dir == 0:
            return 0.0   # no signal

        if lead_dir == direction:
            # Active lead-lag in our direction
            remaining_ms = self.lead_lag.time_remaining_ms()
            # More time remaining = stronger (closer to the firing = fresher)
            freshness = remaining_ms / 2000.0   # 0-1 scale over 2s window
            return 10.0 + freshness * 10.0   # 10-20 pts

        else:
            return -10.0   # lead fired opposite direction — penalty

    def _score_regime(self, regime: str, is_buy: bool, regime_conf: float) -> float:
        """Score regime alignment (0-10 pts)."""
        scores = {
            'TRENDING_UP':    10.0 if is_buy  else 0.0,
            'TRENDING_DOWN':  10.0 if not is_buy else 0.0,
            'RANGING':        5.0,
            'VOLATILE':       2.0,
            'CRASH':          0.0,
            'UNKNOWN':        4.0,
        }
        base = scores.get(regime, 4.0)
        return base * (0.7 + 0.3 * regime_conf)

    # ── Spread estimation ─────────────────────────────────────────────────────

    def _estimate_spread(self, df: pd.DataFrame, symbol: str, atr: float) -> float:
        """
        Estimate bid-ask spread from kill filter history or ATR proxy.
        Prefer actual spread from book history.
        """
        spread_median = self.kill_state.get_spread_median(symbol)
        if spread_median > 0:
            return spread_median

        # ATR-based proxy: spread ≈ 5% of ATR for liquid crypto
        if atr > 0:
            return atr * 0.05

        # Last resort: 0.01% of price
        price = float(df['close'].iloc[-1]) if len(df) > 0 else 1.0
        return price * 0.0001

    # ── Fallback: scientific strategy style scoring ───────────────────────────

    def _fallback_signal(self, df, symbol, price, ofi_calc, lead_lag_old,
                          regime, regime_conf, funding_rate,
                          ema9_v, ema21_v, rsi_v, atr_v, adx_v, vol_ratio, legacy_ofi
                          ) -> Optional[MicrostructureSignal]:
        """
        Confidence-scored fallback when OFI v2 is not yet warmed up.
        Uses a simplified version of ScientificStrategy scoring to return
        a valid MicrostructureSignal.
        """
        logger.debug(f"[MICRO] {symbol} using fallback signal (OFI v2 warming up)")

        close    = df['close']
        ema9_ser = ta.ema(close, length=9)
        ema21_ser = ta.ema(close, length=21)

        ema_cross_up = (
            ema9_ser is not None and len(ema9_ser) >= 2 and
            float(ema9_ser.iloc[-1]) > float(ema21_ser.iloc[-1]) and
            float(ema9_ser.iloc[-2]) <= float(ema21_ser.iloc[-2])
        ) if ema21_ser is not None else False

        ema_cross_down = (
            ema9_ser is not None and len(ema9_ser) >= 2 and
            float(ema9_ser.iloc[-1]) < float(ema21_ser.iloc[-1]) and
            float(ema9_ser.iloc[-2]) >= float(ema21_ser.iloc[-2])
        ) if ema21_ser is not None else False

        ofi_dir = 'NEUTRAL'
        if legacy_ofi is not None:
            if legacy_ofi > 0.15:   ofi_dir = 'BULLISH'
            elif legacy_ofi < -0.15: ofi_dir = 'BEARISH'

        has_buy = (ofi_dir == 'BULLISH' or ema_cross_up or rsi_v < 32)
        has_sell = (ofi_dir == 'BEARISH' or ema_cross_down or rsi_v > 68)

        if not has_buy and not has_sell:
            return _hold_micro(price, legacy_ofi, regime, funding_rate)

        if has_buy and not has_sell:
            is_buy = True
        elif has_sell and not has_buy:
            is_buy = False
        elif ofi_dir == 'BULLISH':
            is_buy = True
        elif ofi_dir == 'BEARISH':
            is_buy = False
        else:
            return _hold_micro(price, legacy_ofi, regime, funding_rate)

        if is_buy and regime == 'CRASH':
            return _hold_micro(price, legacy_ofi, regime, funding_rate)

        # Simple confidence
        confidence = 50.0
        if ofi_dir != 'NEUTRAL':
            confidence += 15.0
        if ema_cross_up and is_buy:
            confidence += 10.0
        if ema_cross_down and not is_buy:
            confidence += 10.0
        if regime in ('TRENDING_UP',) and is_buy:
            confidence += 10.0
        if regime in ('TRENDING_DOWN',) and not is_buy:
            confidence += 10.0
        confidence = min(75.0, confidence)   # cap at 75% for fallback

        size_mult = _size_multiplier(confidence)

        lead_dir_str = None
        if lead_lag_old:
            lead_dir_str = lead_lag_old.get_signal(symbol)

        sig_enum = Signal.BUY if is_buy else Signal.SELL
        spread = self._estimate_spread(df, symbol, atr_v)

        return MicrostructureSignal(
            signal=sig_enum, confidence=confidence, size_mult=size_mult,
            ofi_score=15.0 if ofi_dir != 'NEUTRAL' else 0.0,
            lead_lag_score=0.0, regime_score=10.0,
            rsi_score=0.0, technical_score=0.0, funding_score=0.0,
            ofi=legacy_ofi, lead_lag_dir=lead_dir_str, regime=regime,
            rsi=rsi_v, adx=adx_v, atr=atr_v, close=price,
            ema_fast=ema9_v, ema_slow=ema21_v, volume_ratio=vol_ratio,
            funding_rate=funding_rate,
            spread_at_entry=spread,
            ofi_norm_at_entry=legacy_ofi or 0.0,
            entry_time=time.time(),
            kill_reason='',
        )
