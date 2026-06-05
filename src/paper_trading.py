""" 
Paper Trading Engine
Uses ScientificStrategy (OFI + BTC Lead-Lag primary) with confidence-scaled sizing.
All other strategies (EMA, BB, regime, funding) contribute to the confidence score.

Tick-driven: evaluates every 2 seconds per symbol using live WebSocket price
injected into a cached OHLCV DataFrame.  REST API only called on candle close
(once per minute) — eliminates rate-limit pressure and gives near-real-time signals.
"""

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Deque
from dataclasses import dataclass, field
import logging

import pandas as pd
import pandas_ta as _pta

from .indicators import Signal, prepare_ohlcv_dataframe
from .scientific_strategy import ScientificStrategy, ScientificSignal, compute_position_size, _size_multiplier as _get_size_mult
from .microstructure_strategy import (
    MicrostructureStrategy, MicrostructureSignal,
    _ENTRY_GRACE_SECS as _MICRO_ENTRY_GRACE_SECS,
)
from .exchange import ExchangeConnection, CircuitBreakerOpen
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS
from .regime_detector import RegimeDetector
from .portfolio_optimizer import PortfolioOptimizer
from .crypto_vol import CryptoVolMonitor
from .order_flow import OrderFlowImbalance
from .wick_analyzer import detect_rejection, detect_stop_hunt
from .lead_lag_detector import LeadLagDetector
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .state import write_state, read_state
from .ml_scorer import MLScorer
from .multi_timeframe import MultiTimeframeFilter
from .mean_reversion_strategy import MeanReversionStrategy, MRSignal
from .probability_gate import ProbabilityGate, ENABLED as PROB_GATE_ENABLED, PROB_MODEL_VERSION
from .expectancy_gate import ExpectancyGate
from .macro_data import MacroDataProvider, alt_beta
from .daily_circuit import DailyCircuitBreaker
from .trailing_stop import update_trailing_stop
from .entry_checklist import (
    CheckContext,
    SpreadTracker,
    build_long_checklist,
    build_short_checklist,
)
from .task_supervisor import supervised

logger = logging.getLogger(__name__)

# Kraken Futures maintenance margin rate.  A position is liquidated when the
# unrealized loss consumes (1 - MAINT_MARGIN) of the initial margin.
# Long liq_price  = entry × (1 − (1−MAINT) / leverage)
# Short liq_price = entry × (1 + (1−MAINT) / leverage)
_PERP_MAINT_MARGIN = float(os.getenv("PERP_MAINT_MARGIN", "0.02"))

# ── Funding rate helper ────────────────────────────────────────────────────────
_SYMBOL_TO_FUNDING = {
    'BTC/USD': 'BTCUSDT',
    'ETH/USD': 'ETHUSDT',
    'SOL/USD': 'SOLUSDT',
}

def _get_funding_rate(symbol: str) -> Optional[float]:
    try:
        state = read_state()
        opps  = state.get('funding_opportunities', [])
        usdt  = _SYMBOL_TO_FUNDING.get(symbol, '')
        for o in opps:
            if o.get('symbol') == usdt:
                return o.get('rate_8h', 0) / 100
    except Exception as e:
        logger.debug(f"[FUNDING] state read failed for {symbol}: {e}")
    return None


# ── Adaptation state ───────────────────────────────────────────────────────────
_ADAPT_FILE = 'logs/strategy_adaptations.json'

_adapt: Dict = {
    'min_confidence':     35.0,   # AGGRESSIVE: low entry bar, adapts after streaks
    'loss_streak':        0,
    'win_streak':         0,
    'total_trades':       0,
    'total_wins':         0,
}

def _load_adaptations():
    try:
        if os.path.exists(_ADAPT_FILE):
            with open(_ADAPT_FILE) as f:
                _adapt.update(json.load(f))
            logger.info(f"[ADAPT] Loaded: min_conf={_adapt['min_confidence']:.0f}")
    except Exception as e:
        logger.warning(f"[ADAPT] Failed to load adaptations, using defaults: {e}")

def _save_adaptations():
    try:
        dir_path = os.path.dirname(_ADAPT_FILE) or '.'
        os.makedirs(dir_path, exist_ok=True)
        _adapt['updated_at'] = datetime.now(timezone.utc).isoformat()
        tmp_path = _ADAPT_FILE + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(_adapt, f, indent=2)
        os.replace(tmp_path, _ADAPT_FILE)  # atomic on POSIX — no partial-write corruption
    except Exception as e:
        logger.error(f"[ADAPT] Save failed: {e}")

def _update_streaks_and_adapt(won: bool, notifier):
    """Update win/loss streaks and tighten min_confidence if losing."""
    if won:
        _adapt['win_streak']  += 1
        _adapt['loss_streak']  = 0
        _adapt['total_wins']  += 1
    else:
        _adapt['loss_streak'] += 1
        _adapt['win_streak']   = 0
    _adapt['total_trades'] += 1

    changes = []

    # AGGRESSIVE: cap how high min_confidence can climb after losses so the bot
    # doesn't filter itself out of the market. After 3 losses, bump to a soft
    # ceiling of 45 (was 68). On 5 wins, can relax back to floor of 35.
    if _adapt['loss_streak'] == 3 and _adapt['min_confidence'] < 45:
        _adapt['min_confidence'] = min(45.0, _adapt['min_confidence'] + 3.0)
        changes.append(f"min confidence raised to {_adapt['min_confidence']:.0f}")

    if _adapt['win_streak'] == 5 and _adapt['min_confidence'] > 35:
        _adapt['min_confidence'] = max(35.0, _adapt['min_confidence'] - 2.0)
        changes.append(f"min confidence relaxed to {_adapt['min_confidence']:.0f}")

    _save_adaptations()
    return changes


# ── Post-trade analysis ────────────────────────────────────────────────────────

def _diagnose(side: str, pnl: float, exit_reason: str, holding_min: float,
              sig: ScientificSignal) -> tuple:
    """Returns (issues, positives) based on signal context."""
    issues, positives = [], []

    # OFI
    if sig.ofi is not None:
        ofi_correct = (side == 'buy' and sig.ofi > 0.15) or (side in ('short','sell') and sig.ofi < -0.15)
        ofi_wrong   = (side == 'buy' and sig.ofi < -0.15) or (side in ('short','sell') and sig.ofi > 0.15)
        if ofi_correct:
            positives.append(f"OFI {sig.ofi:+.2f} confirmed direction at entry")
        elif ofi_wrong:
            issues.append(f"OFI {sig.ofi:+.2f} was against direction — order flow warned us")
        else:
            issues.append(f"OFI {sig.ofi:+.2f} was weak (no clear conviction)")

    # Lead-lag
    if sig.lead_lag_dir:
        if sig.lead_lag_dir == ('BUY' if side == 'buy' else 'SELL'):
            positives.append(f"BTC lead confirmed {sig.lead_lag_dir}")
        else:
            issues.append(f"BTC lead was {sig.lead_lag_dir} — opposing this trade")

    # RSI
    if side == 'buy':
        if sig.rsi > 65:
            issues.append(f"RSI {sig.rsi:.0f} was overbought at entry")
        elif sig.rsi < 50:
            positives.append(f"RSI {sig.rsi:.0f} had room to run")
    else:
        if sig.rsi < 35:
            issues.append(f"RSI {sig.rsi:.0f} was oversold — risky short")
        elif sig.rsi > 55:
            positives.append(f"RSI {sig.rsi:.0f} confirmed bearish momentum")

    # Regime
    regime_good = (sig.regime == 'TRENDING_UP' and side == 'buy') or \
                  (sig.regime == 'TRENDING_DOWN' and side in ('short','sell'))
    if regime_good:
        positives.append(f"Regime {sig.regime} aligned with trade direction")
    elif sig.regime in ('VOLATILE', 'CRASH'):
        issues.append(f"Regime {sig.regime} — unpredictable conditions")

    # Confidence
    if sig.confidence >= 90:
        positives.append(f"High conviction entry ({sig.confidence:.0f}% confidence)")
    elif sig.confidence < 70:
        issues.append(f"Low confidence entry ({sig.confidence:.0f}%) — should have skipped")

    # Exit context
    if exit_reason == 'STOP_LOSS':
        issues.append("Stopped out — immediate rejection at entry level")
    elif exit_reason == 'TAKE_PROFIT':
        positives.append("Target reached as predicted")

    if holding_min < 2 and pnl < 0:
        issues.append(f"Held only {holding_min:.0f}min — false breakout")

    return issues, positives


def _record_to_journal(journal, trade, symbol, reason, sig, regime, pos=None):
    now = datetime.now(timezone.utc)
    entry_dt = trade.entry_time if isinstance(trade.entry_time, datetime) else now
    exit_dt  = trade.exit_time  if isinstance(trade.exit_time, datetime)  else now
    hold_sec = (exit_dt - entry_dt).total_seconds()

    # MFE / MAE from the position's peak tracking
    mfe_pct = mae_pct = 0.0
    entry_price_actual = trade.entry_price
    if pos is not None and pos.entry_price > 0:
        if pos.side == 'buy':
            mfe_pct = (pos.peak_favorable_price - pos.entry_price) / pos.entry_price * 100
            mae_pct = (pos.peak_adverse_price   - pos.entry_price) / pos.entry_price * 100
        else:   # short
            mfe_pct = (pos.entry_price - pos.peak_favorable_price) / pos.entry_price * 100
            mae_pct = (pos.entry_price - pos.peak_adverse_price)   / pos.entry_price * 100
        entry_price_actual = pos.entry_price

    # Direction: use position side when we have it; signal can be the exit signal flip
    direction = pos.side if pos else ('buy' if sig and sig.signal == Signal.BUY else 'short')
    if direction == 'sell': direction = 'short'

    # SL/TP from signal
    sl_price = tp_price = 0.0
    if sig is not None and hasattr(sig, 'close') and sig.close:
        if direction == 'buy':
            sl_price = sig.close * (1 - sig.stop_loss_pct() / 100)   if hasattr(sig, 'stop_loss_pct')   else 0.0
            tp_price = sig.close * (1 + sig.take_profit_pct() / 100) if hasattr(sig, 'take_profit_pct') else 0.0
        else:
            sl_price = sig.close * (1 + sig.stop_loss_pct() / 100)   if hasattr(sig, 'stop_loss_pct')   else 0.0
            tp_price = sig.close * (1 - sig.take_profit_pct() / 100) if hasattr(sig, 'take_profit_pct') else 0.0

    # Risk / fee metrics
    fees_paid = getattr(trade, 'fees', 0.0) or 0.0
    fees_pct_of_pnl = (fees_paid / abs(trade.pnl) * 100) if trade.pnl else 0.0

    # R-multiple: pnl / (planned risk on stop)
    r_multiple = 0.0
    if sig and hasattr(sig, 'stop_loss_pct') and sig.stop_loss_pct() > 0 and pos:
        planned_risk = pos.size_usd_target * (sig.stop_loss_pct() / 100)
        if planned_risk > 0:
            r_multiple = trade.pnl / planned_risk

    record = TradeRecord(
        trade_id    = f"{symbol}_{int(now.timestamp())}",
        symbol      = symbol,
        opened_at   = entry_dt.isoformat() if hasattr(entry_dt, 'isoformat') else str(entry_dt),
        closed_at   = exit_dt.isoformat()  if hasattr(exit_dt,  'isoformat') else str(exit_dt),
        rsi          = sig.rsi if sig else 50.0,
        adx          = sig.adx if sig else 20.0,
        volume_ratio = sig.volume_ratio if sig else 1.0,
        regime       = regime,
        atr_pct      = (sig.atr / sig.close * 100) if sig and sig.atr and sig.close else 1.0,
        ema100_gap   = 0.0,
        ema200_gap   = 0.0,
        hour_utc     = entry_dt.hour,
        day_of_week  = entry_dt.weekday(),
        pnl          = round(trade.pnl, 4),
        pnl_pct      = round(trade.pnl_pct, 2),
        won          = trade.pnl > 0,
        reason       = reason,
        # Extended ML features from signal context
        ofi               = float(sig.ofi or 0.0) if sig else 0.0,
        lead_lag_strength = max(0.0, sig.lead_lag_score) / 20.0 if sig else 0.0,
        lead_lag_aligned  = (sig.lead_lag_dir == ('BUY' if (sig and sig.signal == Signal.BUY) else 'SELL')) if sig and sig.lead_lag_dir else False,
        confidence        = float(sig.confidence) if sig else 0.0,
        ofi_score         = float(sig.ofi_score) if sig else 0.0,
        lead_lag_score    = float(sig.lead_lag_score) if sig else 0.0,
        regime_score      = float(sig.regime_score) if sig else 0.0,
        regime_confidence = 0.5,
        funding_rate      = float(sig.funding_rate or 0.0) if sig else 0.0,
        direction         = direction,
        # Post-mortem (excursion + exit context)
        mfe_pct            = round(mfe_pct, 3),
        mae_pct            = round(mae_pct, 3),
        time_in_trade_sec  = round(hold_sec, 1),
        regime_at_exit     = regime,
        rsi_at_exit        = sig.rsi if sig else 0.0,
        adx_at_exit        = sig.adx if sig else 0.0,
        exit_price         = round(trade.exit_price, 6),
        # Entry pathway + pricing
        entry_path         = getattr(pos, 'entry_path', 'main') if pos else 'main',
        entry_price        = round(entry_price_actual, 6),
        position_size_usd  = round(getattr(pos, 'size_usd_target', 0.0), 4) if pos else 0.0,
        stop_loss_price    = round(sl_price, 6),
        take_profit_price  = round(tp_price, 6),
        fees_paid          = round(fees_paid, 4),
        slippage_cost      = 0.0,   # captured via entry vs sig.close drift if needed later
        spread_at_entry    = round(getattr(pos, 'spread_at_entry', 0.0), 6) if pos else 0.0,
        # Sub-scores
        rsi_score          = float(getattr(sig, 'rsi_score', 0.0))       if sig else 0.0,
        technical_score    = float(getattr(sig, 'technical_score', 0.0)) if sig else 0.0,
        funding_score      = float(getattr(sig, 'funding_score', 0.0))   if sig else 0.0,
        # Indicator snapshot
        ema_fast           = float(getattr(sig, 'ema_fast', 0.0)) if sig else 0.0,
        ema_slow           = float(getattr(sig, 'ema_slow', 0.0)) if sig else 0.0,
        atr_at_entry       = float(getattr(sig, 'atr', 0.0))      if sig else 0.0,
        # Market context
        sentiment_fng      = getattr(pos, 'sentiment_fng', None) if pos else None,
        sentiment_btc_dom  = getattr(pos, 'sentiment_btc_dom', None) if pos else None,
        # Realized metrics
        r_multiple         = round(r_multiple, 3),
        fees_pct_of_pnl    = round(fees_pct_of_pnl, 2),
        # Probability gate
        prob_win           = round(float(getattr(pos, 'prob_win', 0.0)), 4) if pos else 0.0,
        edges_used         = ",".join(getattr(pos, 'edges_used', []) or []) if pos else '',
        prob_model_version = int(getattr(pos, 'prob_model_version', 0)) if pos else 0,
    )
    journal.add(record)
    return record


# ── PaperPosition / PaperAccount / PaperTrader ────────────────────────────────

@dataclass
class PaperPosition:
    entry_time:      datetime
    entry_price:     float
    size:            float
    side:            str
    entry_fee:       float = 0.0
    unrealized_pnl:  float = 0.0
    entry_signal:    Optional[ScientificSignal] = None   # full signal context
    # Excursion tracking (updated each tick)
    peak_favorable_price: float = 0.0    # best price for the position
    peak_adverse_price:   float = 0.0    # worst price for the position
    # Entry pathway: 'main' / 'mr' / 'mr-extreme' / 'fast-track'
    entry_path:      str = 'main'
    # Pre-trade context snapshot
    size_usd_target: float = 0.0
    spread_at_entry: float = 0.0
    sentiment_fng:   Optional[int]   = None
    sentiment_btc_dom: Optional[float] = None
    # Probability gate output
    prob_win:        float = 0.0
    edges_used:      List[str] = field(default_factory=list)
    # Conviction tier + trailing stop state (set on entry, updated each tick)
    tier:                str   = 'scalp'
    intended_hold_min:   int   = 0
    trail_style:         str   = 'atr_stop'
    trail_stop_price:    float = 0.0
    target_usd_at_entry: float = 0.0
    # Perp-only state (zero in spot mode)
    is_perp:             bool  = False
    leverage:            float = 1.0
    margin_locked:       float = 0.0    # USD locked as margin (= notional / leverage)
    funding_accrued:     float = 0.0    # cumulative funding paid (long) or collected (short)
    last_funding_ts:     Optional[datetime] = None
    liquidation_price:   float = 0.0    # price at which the exchange force-closes (0 = no liq)


@dataclass
class PaperAccount:
    initial_capital: float
    cash:            float
    positions:       Dict[str, PaperPosition] = field(default_factory=dict)
    closed_trades:   List[Trade]              = field(default_factory=list)
    total_pnl:       float = 0.0


class PaperTrader:
    def __init__(self, initial_capital: float = 100.0,
                 position_size: float = 50.0,     # kept for compat; scientific uses equity %
                 fee_pct: float = 0.40,
                 slippage_pct: float = 0.1,
                 stop_loss_pct: float = 2.0,
                 take_profit_pct: float = 3.0,
                 perp_mode: bool = False,
                 leverage: float = 1.0,
                 allow_spot_shorts: bool = True):
        self.initial_capital  = initial_capital
        self.account          = PaperAccount(initial_capital=initial_capital, cash=initial_capital)
        self.position_size    = position_size
        self.fee_pct          = fee_pct / 100
        self.slippage_pct     = slippage_pct / 100   # floor / fallback
        self.stop_loss_pct    = stop_loss_pct / 100
        self.take_profit_pct  = take_profit_pct / 100
        self.running          = False
        self._started_at: Optional[str] = None
        # Live spread cache populated by paper_trading main loop; used for realistic slippage
        self.live_spreads: Dict[str, float] = {}   # symbol → current spread in price units
        # ── Perp mode state ───────────────────────────────────────────────────
        self.perp_mode        = perp_mode
        self.leverage         = max(1.0, float(leverage)) if perp_mode else 1.0
        # Fee defaults match Kraken Pro lowest tier ($0–10K 30d vol): 0.40% spot
        # taker, 0.05% futures taker. Spot tier is what a $500 account actually
        # pays; futures tier is what live perps would cost (US retail can't
        # access Kraken Futures, but the funding-arb paper sim still uses it).
        if self.perp_mode:
            self.fee_pct = min(self.fee_pct, float(os.getenv('PERP_TAKER_FEE_PCT', '0.05')) / 100)
        # US Kraken spot has no shorting/margin for retail. When this flag is
        # False, execute_short refuses in spot mode so paper P&L reflects what
        # the user could actually replicate on a Kraken Pro spot account.
        self.allow_spot_shorts = bool(allow_spot_shorts)
        # Symbol → current 8h funding rate (fraction, e.g. 0.0001). Caller updates.
        self._funding_rates: Dict[str, float] = {}
        if perp_mode:
            logger.info(f"[PaperTrader] PERP mode ON  leverage={self.leverage:.1f}x")

    # ── Perp funding helpers ──────────────────────────────────────────────────

    def set_funding_rate(self, symbol: str, rate_8h_fraction: float) -> None:
        """Update the current 8h funding rate (as a fraction, e.g. 0.0001 = 0.01%)."""
        self._funding_rates[symbol] = float(rate_8h_fraction)

    def accrue_funding(self, now: datetime) -> None:
        """
        Accrue funding for all open perp positions across any 8h cycles
        elapsed since the last accrual. Long pays positive funding, short collects.
        Called each tick from the main loop; no-op outside perp mode.
        """
        if not self.perp_mode:
            return
        for symbol, pos in self.account.positions.items():
            if not pos.is_perp:
                continue
            rate = self._funding_rates.get(symbol)
            if rate is None:
                continue
            last_ts = pos.last_funding_ts or pos.entry_time
            hours = (now - last_ts).total_seconds() / 3600.0
            cycles = int(hours // 8)
            if cycles <= 0:
                continue
            notional = pos.entry_price * pos.size
            # Long pays positive funding → -rate*notional per cycle
            # Short collects positive funding → +rate*notional per cycle
            sign = -1.0 if pos.side == 'buy' else 1.0
            delta = sign * rate * notional * cycles
            pos.funding_accrued += delta
            pos.last_funding_ts = last_ts + timedelta(hours=cycles * 8)

    def _liquidate(self, symbol: str, liq_price: float, timestamp: datetime) -> Optional['Trade']:
        """Force-close a perp position at exactly liq_price (no additional slippage).

        The exchange marks the position at the maintenance-margin boundary; the
        trader loses almost all margin.  Entry fee was already deducted from cash
        at open, so we add it back here to avoid double-counting (same pattern as
        execute_sell / execute_cover).
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if not pos.is_perp:
            return None
        self.accrue_funding(timestamp)
        exit_fee   = liq_price * pos.size * self.fee_pct
        total_fees = exit_fee + pos.entry_fee
        if pos.side == 'buy':
            pnl = (liq_price - pos.entry_price) * pos.size - total_fees + pos.funding_accrued
        else:
            pnl = (pos.entry_price - liq_price) * pos.size - total_fees + pos.funding_accrued
        cost_basis = pos.margin_locked + pos.entry_fee
        self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        pnl_pct = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=liq_price,
                      size=pos.size, side='liquidation', pnl=pnl,
                      pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        logger.warning(
            f"[LIQUIDATED] {symbol} @ ${liq_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)"
            f"  margin_lost=${pos.margin_locked:.2f}"
            f"  funding=${pos.funding_accrued:+.4f}"
        )
        return trade

    def _slippage_pct_for(self, symbol: str, price: float) -> float:
        """
        Realistic slippage = max(floor, 0.5 × spread_pct).
        On a market order you cross half the spread on entry, half on exit; thin pairs / wide spreads
        give more slippage. Falls back to flat self.slippage_pct when no spread data.
        """
        spread = self.live_spreads.get(symbol, 0.0)
        if spread > 0 and price > 0:
            spread_pct = spread / price
            # Cap slippage to avoid pathological book reads (max 0.5%)
            return max(self.slippage_pct, min(0.005, spread_pct * 0.5))
        return self.slippage_pct

    def execute_buy(self, symbol: str, price: float, timestamp: datetime,
                    size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        size       = size_usd / price
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 + slip)
        fee        = exec_price * size * self.fee_pct
        notional   = exec_price * size
        margin_req = notional / self.leverage if self.perp_mode else notional
        total_cost = margin_req + fee

        if total_cost > self.account.cash:
            # Scale down to fit available cash
            available  = self.account.cash * 0.98
            # cash >= notional/lev + notional*fee_pct  →  notional <= cash / (1/lev + fee_pct)
            denom      = (1.0 / self.leverage) + self.fee_pct if self.perp_mode else (1.0 + self.fee_pct)
            notional   = available / denom
            size       = notional / exec_price
            fee        = notional * self.fee_pct
            margin_req = notional / self.leverage if self.perp_mode else notional
            total_cost = margin_req + fee

        if size <= 0 or total_cost > self.account.cash:
            return None

        liq_price = (
            exec_price * (1.0 - (1.0 - _PERP_MAINT_MARGIN) / self.leverage)
            if self.perp_mode else 0.0
        )
        self.account.cash -= total_cost
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='buy', entry_fee=fee, entry_signal=signal,
                            peak_favorable_price=exec_price,
                            peak_adverse_price=exec_price,
                            size_usd_target=size_usd,
                            is_perp=self.perp_mode,
                            leverage=self.leverage,
                            margin_locked=margin_req,
                            last_funding_ts=timestamp,
                            liquidation_price=liq_price)
        self.account.positions[symbol] = pos
        tag = "[LONG-PERP]" if self.perp_mode else "[BUY]"
        liq_note = f"  liq=${liq_price:,.2f}" if self.perp_mode else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  notional=${notional:.2f} margin=${margin_req:.2f}{liq_note}  conf={signal.confidence:.0f}%" if signal else f"{tag} {symbol} @ ${exec_price:,.2f}{liq_note}")
        return pos

    def execute_sell(self, symbol: str, price: float, timestamp: datetime,
                     reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos        = self.account.positions[symbol]
        # Final funding accrual on the position before closing it
        if pos.is_perp:
            self.accrue_funding(timestamp)
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        exit_fee   = exec_price * pos.size * self.fee_pct
        total_fees = exit_fee + pos.entry_fee
        pnl        = (exec_price - pos.entry_price) * pos.size - total_fees + pos.funding_accrued
        if pos.is_perp:
            cost_basis = pos.margin_locked + pos.entry_fee
            # Return margin + entry_fee (already deducted at open) plus net pnl.
            # pnl already deducts entry_fee via total_fees, so we add it back here
            # to avoid double-counting it against cash.
            self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        else:
            cost_basis = pos.entry_price * pos.size + pos.entry_fee
            self.account.cash += exec_price * pos.size - exit_fee
        pnl_pct    = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='sell', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        tag = "[CLOSE-LONG]" if pos.is_perp else "[SELL]"
        funding_note = f" funding=${pos.funding_accrued:+.4f}" if pos.is_perp else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%){funding_note}  {reason}")
        return trade

    def execute_short(self, symbol: str, price: float, timestamp: datetime,
                      size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        if not self.perp_mode and not self.allow_spot_shorts:
            logger.info(f"[SKIP SHORT] {symbol} — Kraken Pro spot has no retail shorting")
            return None
        size       = size_usd / price
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        fee        = exec_price * size * self.fee_pct
        notional   = exec_price * size
        margin_req = notional / self.leverage if self.perp_mode else notional
        total_cost = margin_req + fee

        if total_cost > self.account.cash:
            available  = self.account.cash * 0.98
            denom      = (1.0 / self.leverage) + self.fee_pct if self.perp_mode else (1.0 + self.fee_pct)
            notional   = available / denom
            size       = notional / exec_price
            fee        = notional * self.fee_pct
            margin_req = notional / self.leverage if self.perp_mode else notional
            total_cost = margin_req + fee

        if size <= 0 or total_cost > self.account.cash:
            return None

        liq_price = (
            exec_price * (1.0 + (1.0 - _PERP_MAINT_MARGIN) / self.leverage)
            if self.perp_mode else 0.0
        )
        self.account.cash -= total_cost
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='short', entry_fee=fee, entry_signal=signal,
                            peak_favorable_price=exec_price,
                            peak_adverse_price=exec_price,
                            size_usd_target=size_usd,
                            is_perp=self.perp_mode,
                            leverage=self.leverage,
                            margin_locked=margin_req,
                            last_funding_ts=timestamp,
                            liquidation_price=liq_price)
        self.account.positions[symbol] = pos
        tag = "[SHORT-PERP]" if self.perp_mode else "[SHORT]"
        liq_note = f"  liq=${liq_price:,.2f}" if self.perp_mode else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  notional=${notional:.2f} margin=${margin_req:.2f}{liq_note}  conf={signal.confidence:.0f}%" if signal else f"{tag} {symbol} @ ${exec_price:,.2f}{liq_note}")
        return pos

    def execute_cover(self, symbol: str, price: float, timestamp: datetime,
                      reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'short':
            return self.execute_sell(symbol, price, timestamp, reason)
        if pos.is_perp:
            self.accrue_funding(timestamp)
        slip        = self._slippage_pct_for(symbol, price)
        exec_price  = price * (1 + slip)
        exit_fee    = exec_price * pos.size * self.fee_pct
        total_fees  = exit_fee + pos.entry_fee
        pnl         = (pos.entry_price - exec_price) * pos.size - total_fees + pos.funding_accrued
        if pos.is_perp:
            cost_basis = pos.margin_locked + pos.entry_fee
            self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        else:
            cost_basis = pos.entry_price * pos.size + pos.entry_fee
            returned   = pos.entry_price * pos.size + pos.entry_fee
            self.account.cash += returned + pnl
        pnl_pct = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='cover', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        tag = "[CLOSE-SHORT]" if pos.is_perp else "[COVER]"
        funding_note = f" funding=${pos.funding_accrued:+.4f}" if pos.is_perp else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%){funding_note}  {reason}")
        return trade

    def execute_partial_sell(self, symbol: str, price: float, timestamp: datetime,
                             fraction: float = 0.5) -> Optional[float]:
        """Close `fraction` of a long position.

        Returns pnl_partial (net of exit fee) or None when no matching position.
        Cash is credited with proceeds minus the exit fee, matching execute_sell
        semantics so that partial + final close produce identical accounting to a
        single full close at the same prices.
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'buy':
            return None
        partial_size = pos.size * fraction
        slip = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        exit_fee = exec_price * partial_size * self.fee_pct
        pnl_partial = (exec_price - pos.entry_price) * partial_size - exit_fee
        pos.size -= partial_size
        self.account.cash += exec_price * partial_size - exit_fee
        self.account.total_pnl += pnl_partial
        logger.info(f"[PARTIAL SELL] {symbol} @ ${exec_price:,.2f}  "
                    f"size={partial_size:.6f}  pnl=${pnl_partial:+.4f}")
        return pnl_partial

    def execute_partial_cover(self, symbol: str, price: float, timestamp: datetime,
                              fraction: float = 0.5) -> Optional[float]:
        """Close `fraction` of a short position.

        Returns pnl_partial (net of exit fee) or None when no matching position.
        Cash formula mirrors execute_cover: releases entry_price × partial_size of
        the locked collateral and adds the net P&L (which includes the exit fee),
        so that partial + final cover produce identical accounting to a single full
        cover at the same prices.
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'short':
            return None
        partial_size = pos.size * fraction
        slip = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 + slip)
        exit_fee = exec_price * partial_size * self.fee_pct
        pnl_partial = (pos.entry_price - exec_price) * partial_size - exit_fee
        pos.size -= partial_size
        # Release proportional collateral and return the net P&L for the covered
        # portion.  Equivalent to (2×entry - exec) × partial - exit_fee, which
        # mirrors execute_cover's "returned + pnl" formula on a pro-rated basis.
        self.account.cash += pos.entry_price * partial_size + pnl_partial
        self.account.total_pnl += pnl_partial
        logger.info(f"[PARTIAL COVER] {symbol} @ ${exec_price:,.2f}  "
                    f"size={partial_size:.6f}  pnl=${pnl_partial:+.4f}")
        return pnl_partial

    def update_unrealized_pnl(self, prices: Dict[str, float]) -> List[str]:
        """Update unrealized PnL and excursion stats for all open positions.

        Returns a list of symbols that were liquidated this tick (perp only).
        Callers may use this to send alerts; existing callers that ignore the
        return value are unaffected.
        """
        to_liquidate: List[str] = []
        for sym, pos in self.account.positions.items():
            if sym not in prices:
                continue
            p = prices[sym]
            raw = (p - pos.entry_price) * pos.size if pos.side == 'buy' else (pos.entry_price - p) * pos.size
            # Perp positions accrue funding continuously; include it so that
            # get_account_summary() and the daily circuit breaker see the true
            # equity (funding paid by longs reduces equity, collected by shorts
            # increases it) before the position is closed.
            pos.unrealized_pnl = raw + pos.funding_accrued if pos.is_perp else raw
            # Track excursions (favorable = direction we want, adverse = against us)
            if pos.side == 'buy':
                if p > pos.peak_favorable_price: pos.peak_favorable_price = p
                if p < pos.peak_adverse_price:   pos.peak_adverse_price   = p
            else:   # short
                if p < pos.peak_favorable_price: pos.peak_favorable_price = p
                if p > pos.peak_adverse_price:   pos.peak_adverse_price   = p
            # Liquidation check — only for perp positions that have a computed boundary
            if pos.is_perp and pos.liquidation_price > 0:
                if pos.side == 'buy' and p <= pos.liquidation_price:
                    to_liquidate.append(sym)
                elif pos.side == 'short' and p >= pos.liquidation_price:
                    to_liquidate.append(sym)

        liquidated: List[str] = []
        for sym in to_liquidate:
            if sym in self.account.positions:
                liq_price = self.account.positions[sym].liquidation_price
                self._liquidate(sym, liq_price, datetime.now(timezone.utc))
                liquidated.append(sym)
        return liquidated

    def get_account_summary(self) -> Dict:
        # Perp positions: only margin_locked is the actual capital at risk, not full notional.
        # Spot positions: full notional (entry_price * size) is the capital deployed.
        pos_val  = sum(
            (p.margin_locked if p.is_perp else p.entry_price * p.size) + p.unrealized_pnl
            for p in self.account.positions.values()
        )
        equity   = self.account.cash + pos_val
        closed   = self.account.closed_trades
        return {
            'cash':           self.account.cash,
            'total_equity':   equity,
            'total_pnl':      self.account.total_pnl,
            'pnl_pct':        (self.account.total_pnl / self.initial_capital) * 100,
            'open_positions': len(self.account.positions),
            'closed_trades':  len(closed),
            'winning_trades': len([t for t in closed if t.pnl > 0]),
            'losing_trades':  len([t for t in closed if t.pnl <= 0]),
        }

    def print_summary(self):
        s = self.get_account_summary()
        print("\n" + "=" * 50)
        print("PAPER TRADING ACCOUNT")
        print("=" * 50)
        print(f"Cash:           ${s['cash']:.2f}")
        print(f"Total Equity:   ${s['total_equity']:.2f}")
        print(f"Total PnL:      ${s['total_pnl']:.2f} ({s['pnl_pct']:.2f}%)")
        print(f"Trades:         {s['closed_trades']}  ({s['winning_trades']}W / {s['losing_trades']}L)")
        print("=" * 50)


# ── Entry-funnel instrumentation ────────────────────────────────────────────────

class _FunnelStats:
    """Exception-safe counter for the entry funnel.

    Tracks where actionable signals die between strategy.evaluate() and order
    execution, so we can see whether the bot is starved at the source (signals
    are all HOLD) or killed by a specific downstream gate. Counts are per
    heartbeat interval (reset each heartbeat). Adds no trading behavior — it only
    counts — so it is safe to leave on permanently.
    """

    def __init__(self):
        self._c: Dict[str, int] = {}

    def bump(self, key: str, n: int = 1):
        self._c[key] = self._c.get(key, 0) + n

    def render(self) -> str:
        seen  = self._c.get('signals_seen', 0)
        hold  = self._c.get('hold', 0)
        act   = self._c.get('actionable', 0)
        execd = self._c.get('exec:long', 0) + self._c.get('exec:short', 0)
        skips = {k.split(':', 1)[1]: v for k, v in sorted(self._c.items())
                 if k.startswith('skip:')}
        skip_str = ' '.join(f"{k}={v}" for k, v in skips.items()) or 'none'
        return (f"seen={seen} hold={hold} actionable={act} "
                f"executed={execd} | skips: {skip_str}")

    def reset(self):
        self._c.clear()


# ── Main paper trading session ─────────────────────────────────────────────────

async def run_paper_trading_session(exchange: ExchangeConnection,
                                     trader: PaperTrader,
                                     symbols: List[str],
                                     timeframe: str = '1m',
                                     lookback: int = 100,
                                     mode: str = 'paper',
                                     notifier: Optional[TelegramNotifier] = None,
                                     sentiment_monitor: Optional[SentimentMonitor] = None,
                                     public_ws: Optional[KrakenPublicWS] = None,
                                     vol_monitor: Optional[CryptoVolMonitor] = None,
                                     book_feed=None,
                                     trade_feed=None,
                                     risk_cfg: Optional[dict] = None):

    risk_cfg = risk_cfg or {}
    trader.running    = True
    trader._started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"Starting paper trading session for {symbols}")

    _load_adaptations()
    restored = _load_open_positions(trader)
    if restored:
        logger.info(f"[RESUME] Restored {restored} open position(s) from disk")

    # ── Subsystems ─────────────────────────────────────────────────────────────
    strategy        = MicrostructureStrategy()
    regime_detector = RegimeDetector()
    ofi_calc        = OrderFlowImbalance(exchange, symbols)
    lead_lag        = LeadLagDetector(lead_symbol='BTC/USD')
    journal         = TradeJournal()
    learner         = Learner(journal)
    portfolio_opt   = PortfolioOptimizer()
    symbol_returns: Dict[str, Deque[float]] = {s: deque(maxlen=500) for s in symbols}
    regime_cache:   Dict[str, dict] = {}

    # ML + multi-timeframe + mean reversion
    ml_scorer   = MLScorer(journal)
    htf_filter  = MultiTimeframeFilter(exchange)
    mr_strategy = MeanReversionStrategy()
    # Probability calibrator: maps the gate's raw stacked P(win) to the empirical
    # win rate from the journal. Stays identity until ~40 resolved trades exist.
    calibrator = None
    if PROB_GATE_ENABLED:
        try:
            from .calibration import ProbabilityCalibrator
            calibrator = ProbabilityCalibrator()
            _calib_report = calibrator.fit_from_journal(journal)
            logger.info("[CALIB] %s", _calib_report.render().splitlines()[0])
        except Exception as e:
            logger.warning(f"[CALIB] disabled ({e}); gate will use raw stacked P")
            calibrator = None
    prob_gate   = ProbabilityGate(calibrator=calibrator) if PROB_GATE_ENABLED else None
    # Expectancy gate: caps each entry path to a small probe size until it proves
    # positive GROSS expectancy over a meaningful sample. See [[directional_cost_bleed_fix]].
    expectancy_gate = ExpectancyGate()
    expectancy_gate.update(journal, force=True)
    logger.info(f"[EXPECTANCY] {expectancy_gate.status()}")
    long_checklist  = build_long_checklist()
    short_checklist = build_short_checklist()
    spread_tracker  = SpreadTracker()
    # VPIN toxic-flow monitor — fed by the WS v2 trade tape (taker side per
    # Kraken docs). Hooked below if trade_feed is provided.
    from .vpin_monitor import VPINMonitor, TOXIC_THRESHOLD as _VPIN_THRESH
    vpin_monitor = VPINMonitor()
    if trade_feed is not None and getattr(trade_feed, "_on_trade", None) is None:
        trade_feed._on_trade = vpin_monitor.on_trade

    # Triangular arbitrage scanner — observe-only paper sim of single-venue
    # cycle opportunities (USD→A→B→USD). Logs every qualifying opportunity
    # so we build a record of whether real edges exist before committing to
    # execution code. Disabled when book_feed isn't wired.
    _triarb_scanner = None
    _triarb_task = None
    if book_feed is not None and os.getenv("TRIARB_ENABLED", "1") == "1":
        try:
            from .triangular_arb import TriangularArbScanner, DEFAULT_CYCLES
            _triarb_scanner = TriangularArbScanner(
                cycles=DEFAULT_CYCLES,
                get_book=lambda s: book_feed.get_top(s, depth=10),
                # Void cycles whose quotes are stale — the thin cross pairs
                # (ETH/BTC, SOL/BTC) lag the USD legs and manufacture phantom
                # edges. book_feed.staleness() returns seconds since last tick.
                get_staleness=book_feed.staleness,
            )
            async def _triarb_loop():
                interval = float(os.getenv("TRIARB_SCAN_INTERVAL_SEC", "30"))
                while True:
                    await asyncio.sleep(interval)
                    try:
                        opps = _triarb_scanner.scan_once()
                        if opps:
                            logger.info(_triarb_scanner.format_log(opps))
                    except Exception as e:
                        logger.debug(f"[TRIARB] scan failed: {e}")
            _triarb_task = asyncio.create_task(supervised('triarb', _triarb_loop, notifier=notifier))
            logger.info(f"[TRIARB] scanner started, cycles={len(DEFAULT_CYCLES)}")
        except Exception as e:
            logger.warning(f"[TRIARB] disabled: {e}")
    macro_provider = MacroDataProvider()
    macro_provider.start()

    # ── Funding-rate arbitrage (market-neutral, runs alongside the scalper) ──────
    # Scans Binance/Bybit funding every minute → state.json (this also feeds the
    # microstructure funding edge + kill filters, which are otherwise starved in
    # this entry point) and paper-trades a cost-aware delta-neutral cash-and-carry
    # sim. Best-effort: any failure here is isolated and never touches the main
    # trading loop. Disable with FUNDING_ARB_ENABLED=0.
    _funding_tasks: List[asyncio.Task] = []
    # Bound up front so the heartbeat can read arm P&L directly from these
    # in-memory objects (race-free) regardless of whether the block below runs.
    _funding_arb_sim = _funding_arb_majors = _funding_arb_kraken = None
    if os.getenv('FUNDING_ARB_ENABLED', '1') == '1':
        try:
            from pathlib import Path as _Path
            from arbitrage.funding_scanner import FundingScanner
            from arbitrage.funding_arb_paper import FundingArbPaperSim, MAJOR_SYMBOLS
            from arbitrage.funding_history import FundingHistory
            from .state import read_state as _read_state, write_state as _write_state

            _funding_scanner = FundingScanner(notifier=None)

            # Shared funding-history tracker → feeds the Kraken arm's persistence
            # gate. Records every scanner snapshot; survives restarts (data/).
            _funding_history = FundingHistory()

            # Arm 1 — aggressive/experimental: all symbols, taker cost. Trades
            # often. NOW positive-funding-only by default: a live-ledger audit
            # showed its entire apparent +$16 came from short-spot (negative-
            # funding) legs whose biggest winners were never charged spot-borrow
            # carry (they closed before the borrow model deployed). Charging the
            # borrow they owed flips that side from +$19 booked to −$11 — i.e. the
            # negative funding ≈ the borrow cost, the legs cancel, and there is no
            # edge there. The clean long-spot/short-perp side needs no borrow and
            # is honestly executable, so the arm is confined to it. Set
            # FUNDING_ARB_AGGR_POSITIVE_ONLY=0 to restore the old both-sides
            # research baseline (its short legs are now correctly carry-charged).
            _aggr_positive_only = os.getenv('FUNDING_ARB_AGGR_POSITIVE_ONLY', '1') == '1'
            _funding_arb_sim = FundingArbPaperSim(
                scanner=_funding_scanner, notifier=notifier,
                positive_funding_only=_aggr_positive_only,
            )

            # Arm 2 — conservative/"honest": liquid majors only, positive funding
            # only (long spot / short perp → no borrow needed). Separate ledger +
            # alert label so the two arms can be compared side by side.
            # REALISM FIX: the arm previously sourced ALL venues and booked a
            # 0.08% cost. But for a US-restricted account only Kraken Futures is
            # executable (funding_scanner docstring: Binance/Bybit are research
            # baselines), and the long-spot/short-perp round-trip on Kraken really
            # costs ~0.54% (Kraken Pro spot maker 0.25% + perp maker 0.02%, ×2
            # sides) — the same figure the Kraken arm uses for the identical trade.
            # Booking 0.08% on non-executable venues flattered the arm two ways.
            # Now confined to Kraken Futures at realistic cost so its P&L means
            # something; the aggressive arm (all venues) remains the research
            # baseline. This raises the breakeven APY floor (~9% → ~59%), so the
            # arm trades only when funding genuinely clears the real cost.
            _maj_cost = float(os.getenv('FUNDING_ARB_MAJORS_COST_FRAC', '0.0054'))
            # Persistence gate (was OFF) — the live ledger showed the majors arm
            # repeating the cycle-0 flip bleed the Kraken arm was fixed for: it
            # opened OP/NEAR/ETC at 28-43% APY and every loss closed
            # reason=funding_flipped cycles=0, eating the entry cost having
            # collected nothing. Verifying funding actually held positive for N
            # cycles (FundingHistory) before entry structurally removes those
            # traps. Shares the same history tracker as the Kraken arm.
            _maj_min_persist = float(
                os.getenv('FUNDING_ARB_MAJORS_MIN_PERSISTENCE_CYCLES', '2')
            )
            _maj_max_flips = int(os.getenv('FUNDING_ARB_MAJORS_MAX_FLIPS', '6'))
            _maj_flip_cooldown = float(
                os.getenv('FUNDING_ARB_MAJORS_FLIP_COOLDOWN_HOURS', '48')
            )
            # Size bump (user-requested): with the persistence gate now filtering
            # entries down to verified-stable funding, more notional lands on the
            # surviving (higher-quality) trades rather than the flip traps.
            # Dedicated knobs so this arm scales independently and is reversible.
            _maj_min_size = float(os.getenv('FUNDING_ARB_MAJORS_MIN_SIZE', '375'))
            _maj_max_size = float(os.getenv('FUNDING_ARB_MAJORS_MAX_SIZE', '1500'))
            _maj_max_total = float(os.getenv('FUNDING_ARB_MAJORS_MAX_TOTAL', '4500'))
            _funding_arb_majors = FundingArbPaperSim(
                scanner=_funding_scanner, notifier=notifier,
                positive_funding_only=True,
                symbol_allowlist=MAJOR_SYMBOLS,
                source_allowlist={'Kraken Futures'},
                cost_frac=_maj_cost,
                min_position_usd=_maj_min_size,
                max_position_usd=_maj_max_size,
                max_total_notional=_maj_max_total,
                history=_funding_history,
                min_persistence_cycles=_maj_min_persist,
                max_flips=_maj_max_flips,
                flip_cooldown_hours=_maj_flip_cooldown,
                state_file=_Path('data/funding_arb_majors_state.json'),
                label="Funding Arb (majors)",
            )

            # Arm 3 — Kraken-only, AGGRESSIVE maker-only config (user-requested).
            # Opportunities sourced from Kraken Futures ONLY (the only venue an
            # account this side of the geo-block can actually trade), positive-
            # funding-only (no spot-borrow risk). Aggressive posture:
            #   • maker-only cost (~0.54% round-trip: maker spot 0.25% + maker
            #     perp 0.02% per side × 2 sides, ~0 slippage as a passive maker —
            #     the spot maker fee is the floor; perps are near-free).
            #   • ALL-IN: one position at a time (max_positions=1), full
            #     allocation per trade (min==max==total), no conviction scaling.
            #   • gate relaxed to ~6-cycle breakeven (≈2 days persistence) and
            #     APY cap raised to 300% — still only enters when funding is
            #     expected to clear the (lower) maker cost, so it passes on the
            #     cycle-0 flip traps that bled ~$29 (memory funding_arb_kraken_bleed)
            #     while trading the genuinely rich opportunities.
            # CAVEAT (paper): maker fills + all-in on illiquid microcaps are not
            # realistically executable live; the sim assumes fills.
            _kraken_cost = float(os.getenv('FUNDING_ARB_KRAKEN_COST_FRAC', '0.0054'))
            _kraken_max_be = float(
                os.getenv('FUNDING_ARB_KRAKEN_MAX_BREAKEVEN_CYCLES', '6')
            )
            _kraken_cap = float(os.getenv('FUNDING_ARB_KRAKEN_MAX_APY', '300'))
            _kraken_alloc = float(os.getenv('FUNDING_ARB_KRAKEN_ALLOC', '100'))
            # Persistence gate: only enter symbols whose funding has actually
            # held positive for N cycles and isn't a serial flipper. This is the
            # evidence-based fix for the cycle-0 flip bleed (the breakeven gate
            # only assumes persistence; this verifies it). Needs ~min_cycles×8h
            # of accumulated history per symbol before it'll pass anything —
            # a deliberate warm-up, not a bug. Set MIN_PERSISTENCE_CYCLES=0 to disable.
            # Default raised 2 → 3: the live ledger showed losers collected 0-1
            # cycles while every winner persisted 7-12, so requiring 3 cycles
            # (~24h) of verified prior stability cleanly separates carry from spikes.
            _kraken_min_persist = float(
                os.getenv('FUNDING_ARB_KRAKEN_MIN_PERSISTENCE_CYCLES', '3')
            )
            _kraken_max_flips = int(os.getenv('FUNDING_ARB_KRAKEN_MAX_FLIPS', '6'))
            # Post-loss re-entry cooldown (hours). After a net-loss close the arm
            # refuses to re-buy that symbol for this long — the realized-outcome
            # feedback guard that stops the re-flip bleed (e.g. DEXE flipped, was
            # re-entered, flipped again). Default 48h; 0 disables.
            _kraken_flip_cooldown = float(
                os.getenv('FUNDING_ARB_KRAKEN_FLIP_COOLDOWN_HOURS', '48')
            )
            # Restrict the Kraken arm to liquid majors. The Kraken funding universe
            # is almost entirely extreme microcap perps (PF_* alts with 300-600%+
            # APY that flip funding at cycle 0) — that was the source of the -$27
            # bleed: with no symbol filter the arm was forced to pick the "least
            # insane" microcap. Confine it to the same MAJOR_SYMBOLS the majors arm
            # uses; combined with the scanner's major-preserving truncation this
            # gives it real, capturable Kraken-majors to trade. Override with
            # FUNDING_ARB_KRAKEN_SYMBOLS="BTC,ETH,..." (comma-separated bases).
            _env_kraken_syms = os.getenv('FUNDING_ARB_KRAKEN_SYMBOLS', '').strip()
            _kraken_allowlist = ({s.strip().upper() for s in _env_kraken_syms.split(',') if s.strip()}
                                 if _env_kraken_syms else MAJOR_SYMBOLS)
            _funding_arb_kraken = FundingArbPaperSim(
                scanner=_funding_scanner, notifier=notifier,
                positive_funding_only=True,
                source_allowlist={'Kraken Futures'},
                symbol_allowlist=_kraken_allowlist,
                cost_frac=_kraken_cost,
                max_breakeven_cycles=_kraken_max_be,
                max_entry_apy=_kraken_cap,
                max_positions=1,
                min_position_usd=_kraken_alloc,
                max_position_usd=_kraken_alloc,
                max_total_notional=_kraken_alloc,
                history=_funding_history,
                min_persistence_cycles=_kraken_min_persist,
                max_flips=_kraken_max_flips,
                flip_cooldown_hours=_kraken_flip_cooldown,
                state_file=_Path('data/funding_arb_kraken_state.json'),
                label="Funding Arb (Kraken)",
            )

            async def _merge_funding_state():
                while True:
                    await asyncio.sleep(65)
                    try:
                        st = _read_state()
                        st['funding_opportunities'] = _funding_scanner.get_state()
                        st['funding_arb'] = _funding_arb_sim.get_summary()
                        st['funding_arb_majors'] = _funding_arb_majors.get_summary()
                        st['funding_arb_kraken'] = _funding_arb_kraken.get_summary()
                        _write_state(st)
                    except Exception as _exc:
                        logger.warning(f"[FUNDING] state merge failed: {_exc}")

            _funding_tasks = [
                asyncio.create_task(supervised('funding_scanner',     _funding_scanner.start,     notifier=notifier)),
                asyncio.create_task(supervised('funding_arb_sim',     _funding_arb_sim.start,     notifier=notifier)),
                asyncio.create_task(supervised('funding_arb_majors',  _funding_arb_majors.start,  notifier=notifier)),
                asyncio.create_task(supervised('funding_arb_kraken',  _funding_arb_kraken.start,  notifier=notifier)),
                asyncio.create_task(supervised('funding_merge_state', _merge_funding_state,        notifier=notifier)),
            ]
            logger.info("[FUNDING] scanner + 3 delta-neutral arb arms "
                        "(aggressive + majors-honest + kraken-executable) started")
        except Exception as _exc:
            logger.warning(f"[FUNDING] disabled ({_exc})")

    circuit_breaker = DailyCircuitBreaker()
    cb_status = circuit_breaker.status()
    logger.info(f"[CIRCUIT] daily: {cb_status['wins']}W/{cb_status['losses']}L "
                f"(max {cb_status['max_losses']} losses, halted={cb_status['halted']})")
    if prob_gate:
        _cal_on = bool(getattr(calibrator, "is_active", False))
        logger.info(f"[PROB-GATE] Enabled (min_p={prob_gate.min_p:.2f}, kelly_ref={prob_gate.kelly_ref:.3f}, "
                    f"calibration={'active' if _cal_on else 'identity (collecting trades)'})")
    strategy.ml_scorer = ml_scorer
    # Attempt initial load/train if journal already has data
    if ml_scorer.should_retrain():
        ml_scorer.train()

    # Risk limits: config.yaml is authoritative, env var overrides for ops.
    max_daily_loss      = float(os.getenv('MAX_DAILY_LOSS',
                                          risk_cfg.get('max_daily_loss', 10)))
    session_start_equity = trader.initial_capital
    # UTC-day-rolling baseline for the daily-loss circuit (reset at midnight).
    risk_day             = datetime.now(timezone.utc).date()
    risk_day_baseline    = trader.initial_capital
    daily_entries_halted = False

    iteration = 0
    funnel = _FunnelStats()   # entry-funnel diagnostics, logged each heartbeat
    prices:   Dict[str, float] = {}
    indicators:       Dict[str, dict]   = {}
    recent_trades:    Deque[dict]        = deque(maxlen=50)
    equity_curve:     Deque[dict]        = deque(maxlen=200)

    if notifier:
        notifier.send_message(
            f"<b>Bot started</b>\n"
            f"Trading: {', '.join(s.split('/')[0] for s in symbols)}\n"
            f"Account: <b>${trader.initial_capital:.2f}</b>"
        )

    # ── Hourly digest ──────────────────────────────────────────────────────────
    async def _hourly_digest():
        await asyncio.sleep(3600)
        while trader.running:
            try:
                s = trader.get_account_summary()
                stats = journal.stats()
                if notifier:
                    notifier.send_status(
                        capital=s['total_equity'], pnl=s['total_pnl'],
                        pnl_pct=s['pnl_pct'], open_positions=s['open_positions'],
                        trades_today=stats.get('total', 0),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DIGEST] hourly digest failed: {e}")
            await asyncio.sleep(3600)

    asyncio.create_task(supervised('hourly_digest', _hourly_digest, notifier=notifier))

    # ── Daily P&L summary (fires at midnight UTC) ──────────────────────────────
    day_start_equity = trader.initial_capital

    async def _daily_summary():
        nonlocal day_start_equity
        while trader.running:
            try:
                # Sleep until next midnight UTC
                now = datetime.now(timezone.utc)
                midnight = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                await asyncio.sleep((midnight - now).total_seconds())

                if not trader.running:
                    break

                s      = trader.get_account_summary()
                closed = trader.account.closed_trades
                # Only count today's trades (since last midnight)
                today  = [t for t in closed if hasattr(t, 'exit_time') and
                          t.exit_time and
                          (datetime.now(timezone.utc) - (t.exit_time if t.exit_time.tzinfo
                           else t.exit_time.replace(tzinfo=timezone.utc))).days == 0]
                wins   = [t for t in today if t.pnl > 0]
                losses = [t for t in today if t.pnl <= 0]
                best   = max((t.pnl for t in today), default=0.0)
                worst  = min((t.pnl for t in today), default=0.0)

                if notifier:
                    notifier.send_daily_summary(
                        total_equity=s['total_equity'],
                        start_equity=day_start_equity,
                        trades=len(today),
                        wins=len(wins),
                        losses=len(losses),
                        best_trade=best,
                        worst_trade=worst,
                    )
                day_start_equity = s['total_equity']   # reset for next day
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DIGEST] daily summary failed: {e}")

    asyncio.create_task(supervised('daily_summary', _daily_summary, notifier=notifier))

    # ── SL/TP watcher ──────────────────────────────────────────────────────────
    async def _sltp_watcher():
        while trader.running:
            try:
                await asyncio.sleep(1)
                if not trader.account.positions:
                    continue
                ws_prices = public_ws.get_prices() if public_ws else {}
                for sym in list(trader.account.positions.keys()):
                    try:
                        price = ws_prices.get(sym) or prices.get(sym)
                        if not price:
                            continue
                        pos = trader.account.positions.get(sym)
                        if not pos:
                            continue
                        now = datetime.now(timezone.utc)
                        sig = pos.entry_signal

                        # Use signal-derived SL/TP if available, else fallback
                        sl_pct = sig.stop_loss_pct()    / 100 if sig else trader.stop_loss_pct
                        tp_pct = sig.take_profit_pct()  / 100 if sig else trader.take_profit_pct

                        if pos.side == 'short':
                            pnl_pct = (pos.entry_price - price) / pos.entry_price * 100
                            close_fn = trader.execute_cover
                        else:
                            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                            close_fn = trader.execute_sell

                        # Post-entry grace: a fresh taker entry sits ~spread underwater,
                        # so suppress the fixed stop for the first few seconds to avoid
                        # the ~2s noise flush seen in the journal audit. Trailing stop and
                        # take-profit are unaffected.
                        entry_dt = pos.entry_time if isinstance(pos.entry_time, datetime) else now
                        secs_open = (now - entry_dt).total_seconds()

                        exit_reason = None
                        # Tier-based trailing stop / max-hold (skipped for legacy atr_stop)
                        trail_reason = update_trailing_stop(pos, price)
                        if trail_reason:
                            exit_reason = trail_reason
                        elif pnl_pct / 100 <= -sl_pct and secs_open >= _MICRO_ENTRY_GRACE_SECS:
                            exit_reason = 'STOP_LOSS'
                        elif pnl_pct / 100 >= tp_pct:
                            # All tiers honor fixed TP — was previously gated to atr_stop only;
                            # losers had MFE 0.23% so they never hit TP and bled out instead.
                            exit_reason = 'TAKE_PROFIT'

                        if exit_reason:
                            trade = close_fn(sym, price, now, reason=exit_reason)
                            if trade:
                                recent_trades.append(_trade_to_dict(trade, sym, exit_reason))
                                summary = trader.get_account_summary()
                                equity_curve.append({'t': now.strftime('%Y-%m-%d %H:%M'), 'v': round(summary['total_equity'], 2)})
                                _on_trade_closed(sym, pos, trade, exit_reason, summary['total_equity'],
                                                 notifier, journal, ml_scorer, calibrator)
                                _record_exit(sym, exit_reason)
                                just_halted, halt_msg = circuit_breaker.record_outcome(
                                    won=(trade.pnl > 0), pnl=trade.pnl, symbol=sym)
                                if just_halted and notifier:
                                    notifier.send_message(halt_msg)
                    except Exception as e:
                        logger.error(f"[SLTP] error processing {sym}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[SLTP] watcher iteration error: {e}")

    asyncio.create_task(supervised('sltp_watcher', _sltp_watcher, notifier=notifier, max_restarts=10))

    # ── OFI prefetch (runs every 30s in background) ────────────────────────────
    async def _ofi_prefetcher():
        consecutive_failures = 0
        while trader.running:
            for sym in symbols:
                try:
                    await ofi_calc.fetch(sym)
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(f"[OFI] fetch failed for {sym} (#{consecutive_failures}): {e}")
                    if consecutive_failures == 10 and notifier:
                        notifier.send_message(
                            f"⚠️ <b>OFI subsystem degraded</b>\n"
                            f"Failed {consecutive_failures} times in a row — "
                            f"trading without live order flow data"
                        )
                await asyncio.sleep(2)
            await asyncio.sleep(20)   # full cycle every ~26s

    asyncio.create_task(supervised('ofi_prefetcher', _ofi_prefetcher, notifier=notifier))

    # ── 5-minute HTF cache (refreshed every 60s in background) ────────────────
    async def _htf_fetcher():
        consecutive_failures = 0
        while trader.running:
            for sym in symbols:
                try:
                    await htf_filter.fetch(sym)
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(f"[HTF] fetch failed for {sym} (#{consecutive_failures}): {e}")
                    if consecutive_failures == 10 and notifier:
                        notifier.send_message(
                            f"⚠️ <b>HTF filter degraded</b>\n"
                            f"Failed {consecutive_failures} times in a row — "
                            f"multi-timeframe alignment unavailable"
                        )
                await asyncio.sleep(3)
            await asyncio.sleep(45)   # full cycle every ~54s

    asyncio.create_task(supervised('htf_fetcher', _htf_fetcher, notifier=notifier))

    # ── OHLCV cache — one DataFrame per symbol, refreshed on candle close ──────
    ohlcv_cache:       Dict[str, pd.DataFrame] = {}
    last_eval_time:    Dict[str, float]        = {}   # throttle per symbol
    EVAL_INTERVAL = 2.0   # seconds between strategy evaluations per symbol

    # ── Risk-control state ───────────────────────────────────────────────────────
    # Cooldowns prevent re-entering the same losing setup. Bar-dedup prevents
    # multiple entries per bar from intra-bar signal flicker (live-price injection
    # mutates the in-progress candle every tick, so EMA cross can briefly fire).
    last_exit_time:     Dict[str, float] = {}   # unix-ts of last exit per symbol
    last_exit_reason:   Dict[str, str]   = {}   # exit type → drives cooldown length
    last_entry_bar:     Dict[str, float] = {}   # bar-ts of last entry attempt
    last_ws_price_time: Dict[str, float] = {}   # when WS price last refreshed
    last_heartbeat:     float            = 0.0  # for periodic status log
    last_account_report: float           = 0.0  # for periodic Telegram P&L report
    # Backtests showed 25/33 signal-flip exits hit a 4% win rate — a single
    # opposing bar is mostly noise. Require N consecutive flips before exiting.
    opposing_streak:    Dict[str, int]   = {}
    SIGNAL_EXIT_STREAK: int              = 2

    # Tunables
    COOLDOWN_AFTER_STOP_SEC   = 300.0   # 5 min after STOP_LOSS / SL / signal_stop
    COOLDOWN_AFTER_SIGNAL_SEC = 60.0    # 1 min after signal-driven exits
    MAX_OPEN_POSITIONS        = int(os.getenv('MAX_OPEN_POSITIONS',
                                              risk_cfg.get('max_open_positions', 4)))  # config-authoritative
    WS_PRICE_STALENESS_SEC    = 12.0    # skip entry if WS price older than this
    CORRELATED_GROUPS         = [{'BTC/USD', 'ETH/USD', 'SOL/USD'}]
    HEARTBEAT_INTERVAL_SEC    = 60.0
    # Periodic account report to Telegram (made/lost + total money). Default hourly.
    ACCOUNT_REPORT_INTERVAL_SEC = float(os.getenv('ACCOUNT_REPORT_HOURS', '1.0')) * 3600
    # Push skipped/rejected-signal reasoning to Telegram? Default off → log only.
    # (This was the source of the "only tells me when it skips" noise.)
    NOTIFY_SKIPS              = os.getenv('NOTIFY_SKIPS', '0') == '1'

    # Daily summary scheduler — fires once when UTC date rolls over
    session_start_utc_date    = datetime.now(timezone.utc).date()
    last_daily_summary_date   = session_start_utc_date   # don't fire one immediately on startup

    def _cooldown_for(reason: str) -> float:
        r = (reason or '').upper()
        if 'STOP' in r:           return COOLDOWN_AFTER_STOP_SEC
        return COOLDOWN_AFTER_SIGNAL_SEC

    def _has_correlated_position(sym: str, side: str) -> bool:
        for group in CORRELATED_GROUPS:
            if sym not in group:
                continue
            for other_sym, other_pos in trader.account.positions.items():
                if other_sym == sym or other_sym not in group:
                    continue
                if other_pos.side == side:
                    return True
        return False

    def _record_exit(sym: str, reason: str):
        last_exit_time[sym]   = time.time()
        last_exit_reason[sym] = reason or ''

    def _kill_filter_skip(sym: str, df: pd.DataFrame, side: str = 'buy') -> Optional[str]:
        """Quick pre-entry check for funding-extreme, whale-print, hostile book
        imbalance, and CVD-vs-price divergence.
        WS-stale, daily-loss, max-positions, correlation are handled elsewhere.
        Returns reason string if should skip, else None."""
        # Funding rate extreme — when paying >0.1% per 8h, longs are very expensive
        fr = _get_funding_rate(sym)
        if fr is not None and abs(fr) > 0.001:
            return f"FUNDING_EXTREME ({fr:.4f}/8h)"
        # Whale print — current candle volume > 10× SMA20 suggests a market mover
        try:
            if len(df) >= 21:
                cur_vol = float(df['volume'].iloc[-1])
                sma20   = float(df['volume'].rolling(20).mean().iloc[-2])  # exclude current
                if sma20 > 0 and cur_vol > sma20 * 10.0:
                    return f"WHALE_PRINT ({cur_vol/sma20:.1f}× SMA20)"
        except Exception as e:
            logger.debug(f"[KILL-FILTER] whale-print check failed for {sym}: {e}")
        # Book imbalance — opposing side stacked → cascade/squeeze risk
        try:
            book_reason = ofi_calc.book_imbalance_blocks(sym, side)
            if book_reason:
                return book_reason
        except Exception as e:
            logger.debug(f"[KILL-FILTER] book-imbalance check failed for {sym}: {e}")
        # CVD divergence — price and order flow disagreeing
        try:
            tracker = strategy._cvd_trackers.get(sym) if strategy else None
            if tracker is not None:
                div_reason = tracker.divergence_blocks(side)
                if div_reason:
                    return div_reason
        except Exception as e:
            logger.debug(f"[KILL-FILTER] CVD-divergence check failed for {sym}: {e}")
        # Microprice unfair — paying a markup over depth-weighted fair value
        try:
            last_price = float(df['close'].iloc[-1]) if len(df) else 0.0
            if last_price > 0:
                mp_reason = ofi_calc.microprice_blocks(sym, side, last_price)
                if mp_reason:
                    return mp_reason
        except Exception as e:
            logger.debug(f"[KILL-FILTER] microprice check failed for {sym}: {e}")
        # Wick rejection — clustered rejections forming a ceiling (long) or floor (short)
        try:
            rej_reason = detect_rejection(df, side=side)
            if rej_reason:
                return rej_reason
        except Exception as e:
            logger.debug(f"[KILL-FILTER] wick-rejection check failed for {sym}: {e}")
        return None

    async def _seed_cache():
        """Populate cache before the tick loop starts."""
        for sym in symbols:
            try:
                ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                if ohlcv:
                    ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                    logger.info(f"[CACHE] {sym} seeded with {len(ohlcv_cache[sym])} bars")
                    # Compute initial regime so trades evaluated before the first WS
                    # candle event don't all get logged as regime=UNKNOWN.
                    result = regime_detector.detect(ohlcv_cache[sym])
                    if result:
                        regime_cache[sym] = result.to_dict()
                        logger.info(f"[REGIME] {sym} seeded: {result.regime} "
                                    f"conf={result.confidence:.2f} adx={result.adx:.1f}")
                    else:
                        logger.warning(f"[REGIME] {sym} seed detect() returned None "
                                       f"(bars={len(ohlcv_cache[sym])}); regime will be UNKNOWN "
                                       f"until a WS candle arrives")
            except CircuitBreakerOpen as e:
                wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                logger.error(f"[CACHE] Exchange circuit breaker open during seed — stopping early ({wait:.0f}s remaining): {e}")
                if notifier:
                    try:
                        notifier.send_message(
                            f"⚠️ <b>Exchange circuit breaker open</b>\n"
                            f"Data seed interrupted — cooldown {wait:.0f}s.\n"
                            f"Bot will trade on stale/incomplete cache until exchange recovers."
                        )
                    except Exception:
                        pass
                break  # no point seeding other symbols while the circuit is open
            except Exception as e:
                logger.warning(f"[CACHE] seed failed for {sym}: {e}")

    await _seed_cache()

    async def _candle_refresher():
        """Refresh OHLCV cache when a confirmed candle closes (WS event).
        Falls back to refreshing all symbols every 90 s if no event."""
        while trader.running:
            try:
                if public_ws:
                    candle = await asyncio.wait_for(
                        public_ws.candle_queue.get(), timeout=90
                    )
                    refresh_syms = [candle.symbol] if candle.symbol in symbols else symbols
                    # Feed closed candle into microstructure CVD tracker
                    if candle.symbol in symbols:
                        try:
                            strategy.update_candle(candle.symbol, candle)
                        except Exception as e:
                            logger.debug(f"[WS] candle update failed for {candle.symbol}: {e}")
                else:
                    await asyncio.sleep(60)
                    refresh_syms = symbols

                for sym in refresh_syms:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                            # Refresh regime on new candle data
                            result = regime_detector.detect(ohlcv_cache[sym])
                            if result:
                                regime_cache[sym] = result.to_dict()
                                logger.debug(f"[REGIME] {sym}: {result.regime} conf={result.confidence:.2f} adx={result.adx:.1f}")
                            else:
                                logger.debug(f"[REGIME] {sym}: detect() returned None (bars={len(ohlcv_cache[sym])})")
                            # Feed latest candle into CVD tracker (fallback when WS unavailable)
                            if not public_ws:
                                df_feed = ohlcv_cache[sym]
                                if len(df_feed) >= 1:
                                    last_row = df_feed.iloc[-1]
                                    candle_dict = {
                                        'open':      float(last_row['open']),
                                        'high':      float(last_row['high']),
                                        'low':       float(last_row['low']),
                                        'close':     float(last_row['close']),
                                        'volume':    float(last_row['volume']),
                                        'timestamp': float(last_row.name.timestamp())
                                                     if hasattr(last_row.name, 'timestamp')
                                                     else time.time(),
                                    }
                                    try:
                                        strategy.update_candle(sym, candle_dict)
                                    except Exception as e:
                                        logger.debug(f"[CACHE] candle update failed for {sym}: {e}")
                            # Track returns for CVaR
                            df_tmp = ohlcv_cache[sym]
                            if len(df_tmp) >= 2:
                                ret = float(df_tmp['close'].pct_change().iloc[-1])
                                if not pd.isna(ret):
                                    symbol_returns[sym].append(ret)
                    except CircuitBreakerOpen as e:
                        wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                        logger.warning(
                            f"[CACHE] Exchange circuit breaker open — pausing refresher "
                            f"{wait:.0f}s (symbol={sym})"
                        )
                        if notifier:
                            try:
                                notifier.send_message(
                                    f"⚠️ <b>Exchange circuit breaker open</b>\n"
                                    f"OHLCV refresh paused {wait:.0f}s — trading on stale data."
                                )
                            except Exception:
                                pass
                        await asyncio.sleep(wait)
                        break  # skip remaining symbols; refresher will retry next cycle
                    except Exception as e:
                        logger.debug(f"[CACHE] refresh failed for {sym}: {e}")
            except asyncio.TimeoutError:
                # Fallback: refresh all (no WS candles seen in 90s — WS may be silent).
                # Refresh regime here too so the cache doesn't go permanently stale /
                # empty when the WS event path isn't delivering candles.
                for sym in symbols:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                            result = regime_detector.detect(ohlcv_cache[sym])
                            if result:
                                regime_cache[sym] = result.to_dict()
                    except CircuitBreakerOpen as e:
                        wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                        logger.warning(f"[CACHE] Circuit open (fallback path) — sleeping {wait:.0f}s")
                        await asyncio.sleep(wait)
                        break  # skip remaining symbols; will resume next cycle
                    except Exception as e:
                        logger.warning(f"[CACHE] fallback refresh failed for {sym}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] refresher error: {e}")

    asyncio.create_task(supervised('candle_refresher', _candle_refresher, notifier=notifier, max_restarts=10))

    # ── Startup notification (so we know systemd restarts after a crash) ──────
    if notifier:
        try:
            stats_pre = journal.stats()
            wr_pre = stats_pre.get('win_rate', 0.0)
            total_pre = stats_pre.get('total', 0)
            open_at_start = len(trader.account.positions)
            startup_lines = [
                f"🟢 <b>Bot online</b>",
                f"Mode: <b>{mode}</b>   Pairs: {', '.join(symbols)}",
                f"Equity: <b>${trader.get_account_summary()['total_equity']:.2f}</b>",
                f"Min conf: <b>{_adapt['min_confidence']:.0f}</b>   Max positions: <b>{MAX_OPEN_POSITIONS}</b>",
                f"Journal: {total_pre} prior trades, WR {wr_pre:.0f}%",
            ]
            if restored:
                startup_lines.append(f"⚠️ Resumed {restored} open position(s) from disk")
            elif open_at_start:
                startup_lines.append(f"Open: {open_at_start} position(s)")
            notifier.send_message("\n".join(startup_lines))
        except Exception as e:
            logger.error(f"[STARTUP] notify failed: {e}")

    # ── Main tick loop — evaluates every EVAL_INTERVAL seconds ─────────────────
    while trader.running:
        try:
            await asyncio.sleep(EVAL_INTERVAL)
            iteration += 1

            # Daily loss circuit breaker — UTC-day-rolling baseline. Halts NEW
            # entries for the rest of the day (exits + funding arms keep running)
            # and resets at midnight UTC, instead of permanently killing the loop.
            current_equity = trader.get_account_summary()['total_equity']
            _utc_today = datetime.now(timezone.utc).date()
            if _utc_today != risk_day:
                risk_day = _utc_today
                risk_day_baseline = current_equity
                if daily_entries_halted:
                    logger.info("[RISK] New UTC day — daily-loss halt cleared")
                daily_entries_halted = False
            daily_loss = risk_day_baseline - current_equity
            # Expose today's P&L fraction so the microstructure kill filter's
            # DAILY_LOSS gate (Filter 7) can actually fire.
            strategy._daily_pnl_pct = (current_equity - risk_day_baseline) / max(risk_day_baseline, 1e-9)
            if daily_loss >= max_daily_loss and not daily_entries_halted:
                logger.warning(f"[RISK] Daily loss limit ${daily_loss:.2f} hit — "
                               f"halting new entries until 00:00 UTC")
                if notifier:
                    notifier.send_message(
                        f"🛑 <b>Daily loss limit hit</b>\n"
                        f"Lost ${daily_loss:.2f} today — new entries halted until "
                        f"00:00 UTC (exits and funding arms continue)"
                    )
                daily_entries_halted = True

            # Refresh CVaR every 50 iterations
            if iteration % 50 == 0 and any(len(v) >= 20 for v in symbol_returns.values()):
                portfolio_opt.optimize(symbol_returns)

            ws_prices = public_ws.get_prices() if public_ws else {}

            for symbol in symbols:
                # Throttle per symbol
                now_ts = time.time()
                if now_ts - last_eval_time.get(symbol, 0) < EVAL_INTERVAL:
                    continue
                last_eval_time[symbol] = now_ts

                # Skip if no cache yet
                if symbol not in ohlcv_cache:
                    continue

                # Live price from WebSocket; fall back to last candle close
                ws_price = ws_prices.get(symbol)
                if ws_price:
                    current_price = ws_price
                    last_ws_price_time[symbol] = now_ts
                else:
                    current_price = prices.get(symbol)
                if not current_price:
                    continue

                prices[symbol] = current_price
                current_time   = datetime.now(timezone.utc)

                # Inject live price into cached df (synthetic current candle)
                df = _inject_live_price(ohlcv_cache[symbol], current_price)

                # Feed lead-lag (legacy)
                lead_lag.update_price(symbol, current_price)

                # Feed microstructure strategy with live price
                strategy.update_price(symbol, current_price)

                # Regime from cache (updated by candle refresher)
                cached_regime = regime_cache.get(symbol, {})
                regime_name   = cached_regime.get('regime', 'UNKNOWN')
                regime_conf   = cached_regime.get('confidence', 0.5)

                # Funding rate
                funding_rate = _get_funding_rate(symbol)

                # ── Microstructure OFI v2 book update ─────────────────────────
                # Prefer streaming WS book (sub-second updates); fall back to REST
                # only when WS has no fresh data (<3s old). This wakes the
                # microstructure scalper from REST-snapshot dormancy.
                try:
                    bids_raw: List = []
                    asks_raw: List = []
                    if book_feed is not None and book_feed.staleness(symbol) < 3.0:
                        bids_raw, asks_raw = book_feed.get_top(symbol, depth=10)
                    if not (bids_raw and asks_raw):
                        ob = await ofi_calc._exchange.exchange.fetch_order_book(symbol, limit=20)
                        if ob:
                            bids_raw = ob.get('bids', [])
                            asks_raw = ob.get('asks', [])
                    if bids_raw and asks_raw:
                        strategy.update_book(symbol, bids_raw, asks_raw, time.time())
                        # Update volume SMA for whale filter
                        vol_sma20 = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else 0.0
                        strategy.update_volume_sma(symbol, vol_sma20)
                        # Feed live spread into trader for realistic slippage
                        if bids_raw and asks_raw and len(bids_raw[0]) >= 1 and len(asks_raw[0]) >= 1:
                            try:
                                best_bid = float(bids_raw[0][0])
                                best_ask = float(asks_raw[0][0])
                                if best_ask > best_bid > 0:
                                    spread_abs = best_ask - best_bid
                                    trader.live_spreads[symbol] = spread_abs
                                    mid = (best_ask + best_bid) / 2.0
                                    if mid > 0:
                                        spread_tracker.push(symbol, spread_abs / mid)
                            except Exception as e:
                                logger.debug(f"[OFI] spread parse failed for {symbol}: {e}")
                except CircuitBreakerOpen as e:
                    wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                    logger.warning(
                        f"[TICK] Exchange circuit breaker open during book fetch "
                        f"— skipping remaining symbols, sleeping {wait:.0f}s"
                    )
                    await asyncio.sleep(wait)
                    break  # skip remaining symbols this tick; circuit may half-open by next tick
                except Exception as e:
                    logger.debug(f"[OFI] order book fetch failed for {symbol}: {e}")

                # ── Microstructure exit checks for open positions ─────────────
                pos_check = trader.account.positions.get(symbol)
                if pos_check is not None:
                    ofi_state_check = strategy.ofi_states.get(symbol)
                    curr_ofi_norm   = ofi_state_check.ofi_norm if ofi_state_check else 0.0
                    entry_dt = pos_check.entry_time if isinstance(pos_check.entry_time, datetime) else datetime.now(timezone.utc)
                    time_open_secs  = (datetime.now(timezone.utc) - entry_dt).total_seconds()

                    exit_reason, exit_type = strategy.check_exit(
                        symbol, pos_check, current_price, curr_ofi_norm, time_open_secs
                    )

                    if exit_reason is not None:
                        if exit_type == 'PARTIAL':
                            # T1 partial: close 50% of position via accounting-correct helpers
                            if pos_check.side == 'buy':
                                pnl_partial = trader.execute_partial_sell(
                                    symbol, current_price, datetime.now(timezone.utc))
                            else:
                                pnl_partial = trader.execute_partial_cover(
                                    symbol, current_price, datetime.now(timezone.utc))
                            if pnl_partial is not None:
                                logger.info(f"[MICRO T1] {symbol} partial exit @ ${current_price:,.2f}  pnl=${pnl_partial:+.4f}")
                                if notifier:
                                    notifier.send_message(
                                        f"📊 <b>Half closed</b> — {symbol.split('/')[0]}\n"
                                        f"Locked in partial profit @ ${current_price:,.2f}"
                                    )
                        else:
                            # Full exit
                            if pos_check.side == 'buy':
                                trade = trader.execute_sell(symbol, current_price, datetime.now(timezone.utc), reason=exit_reason)
                            else:
                                trade = trader.execute_cover(symbol, current_price, datetime.now(timezone.utc), reason=exit_reason)
                            if trade:
                                recent_trades.append(_trade_to_dict(trade, symbol, exit_reason))
                                summary = trader.get_account_summary()
                                equity_curve.append({'t': _ts(datetime.now(timezone.utc)), 'v': round(summary['total_equity'], 2)})
                                _on_trade_closed(symbol, pos_check, trade, exit_reason,
                                                 summary['total_equity'], notifier, journal, ml_scorer, calibrator)
                                _record_exit(symbol, exit_reason)
                                just_halted, halt_msg = circuit_breaker.record_outcome(
                                    won=(trade.pnl > 0), pnl=trade.pnl, symbol=symbol)
                                if just_halted and notifier:
                                    notifier.send_message(halt_msg)
                        continue   # skip entry logic this tick after an exit

                # ── Strategy evaluation ────────────────────────────────────────
                sig = strategy.evaluate(df, symbol, ofi_calc, lead_lag,
                                        regime_name, regime_conf, funding_rate)

                if sig is None:
                    continue

                # Track which pathway produced this signal — set by each branch below
                _entry_path_tag = 'main'

                # ── Multi-timeframe alignment adjustment ───────────────────────
                if sig.signal != Signal.HOLD:
                    mtf_adj = htf_filter.alignment_score(symbol, is_buy=sig.is_buy)
                    if mtf_adj != 0.0:
                        sig.confidence = max(0.0, min(100.0, sig.confidence + mtf_adj))
                        sig.size_mult  = _get_size_mult(sig.confidence)

                # ── Stop-hunt boost: +6 confidence when a recent flush aligns ──
                # A wick that pierced a 60-bar swing and was reclaimed within 2
                # bars is a forced-liquidation event; trading WITH the reversal
                # historically prints a high win rate, so nudge confidence up.
                if sig.signal != Signal.HOLD:
                    try:
                        hunt_side = 'buy' if sig.is_buy else 'sell'
                        hunt = detect_stop_hunt(df, side=hunt_side)
                        if hunt is not None:
                            sig.confidence = min(100.0, sig.confidence + 6.0)
                            sig.size_mult  = _get_size_mult(sig.confidence)
                            logger.info(
                                f"[STOP-HUNT] {symbol} {hunt_side.upper()} pierce="
                                f"{hunt['pierce_price']:.2f} swing={hunt['swing_level']:.2f} "
                                f"reclaim_lag={hunt['reclaim_lag']} → conf+6"
                            )
                    except Exception:
                        pass

                # ── Mean-reversion: RANGING regime only (mr-extreme disabled) ──
                # mr-extreme was 28/32 trades with 7.1% WR — disabled after live data.
                _mr_ok_regime = regime_name in ('RANGING', 'VOLATILE', 'UNKNOWN')
                if sig.signal == Signal.HOLD and _mr_ok_regime:
                    mr_sig = mr_strategy.get_latest_signal(df)
                    _rsi_extreme = False  # mr-extreme path disabled
                    if mr_sig and mr_sig.signal != Signal.HOLD and regime_name == 'RANGING':
                        # Higher confidence for the extreme-RSI variant (higher edge)
                        mr_conf = 70.0 if _rsi_extreme else 62.0
                        regime_score_mr = 12.0 if regime_name == 'RANGING' else 8.0
                        sig = ScientificSignal(
                            signal          = mr_sig.signal,
                            confidence      = mr_conf,
                            size_mult       = _get_size_mult(mr_conf),
                            ofi_score       = 0.0,
                            lead_lag_score  = 0.0,
                            regime_score    = regime_score_mr,
                            rsi_score       = 15.0 if _rsi_extreme else 8.0,
                            technical_score = 8.0,
                            funding_score   = 0.0,
                            ofi             = ofi_calc.get_smoothed(symbol),
                            lead_lag_dir    = lead_lag.get_signal(symbol),
                            regime          = regime_name,
                            rsi             = mr_sig.rsi,
                            adx             = mr_sig.adx,
                            atr             = mr_sig.atr,
                            close           = mr_sig.close,
                            ema_fast        = mr_sig.close,
                            ema_slow        = mr_sig.close,
                            volume_ratio    = 1.0,
                            funding_rate    = funding_rate,
                        )
                        _entry_path_tag = 'mr-extreme' if _rsi_extreme else 'mr'
                        tag = "RSI-extreme" if _rsi_extreme else regime_name
                        logger.info(f"[MR] {symbol} {tag}: {mr_sig.signal.value} conf={mr_conf:.0f} rsi={mr_sig.rsi:.1f}")

                # ── OFI + Lead-Lag fast-track: documented short-term edge ──────
                # When OFI is strongly directional AND BTC just led same direction,
                # this is a high-edge setup that bypasses the conf=55 gate but
                # still requires conf ≥ 50 and matching directional signals.
                if sig.signal == Signal.HOLD:
                    ofi_now = ofi_calc.get_smoothed(symbol) if ofi_calc else None
                    lead_dir_now = lead_lag.get_signal(symbol)
                    lead_strength_now = lead_lag.get_strength(symbol)
                    if (ofi_now is not None and abs(ofi_now) >= 0.30
                        and lead_dir_now is not None
                        and lead_strength_now > 0.4):
                        ofi_dir = 'BUY' if ofi_now > 0 else 'SELL'
                        if ofi_dir == lead_dir_now and regime_name != 'CRASH':
                            ft_signal = Signal.BUY if ofi_dir == 'BUY' else Signal.SELL
                            # Don't short into a strong uptrend
                            if not (ft_signal == Signal.SELL and regime_name == 'TRENDING_UP'):
                                ft_conf = 55.0 + min(15.0, (abs(ofi_now) - 0.30) * 50)  # 55-70 range
                                sig = ScientificSignal(
                                    signal          = ft_signal,
                                    confidence      = ft_conf,
                                    size_mult       = _get_size_mult(ft_conf),
                                    ofi_score       = 20.0,
                                    lead_lag_score  = 15.0,
                                    regime_score    = 10.0,
                                    rsi_score       = 5.0,
                                    technical_score = 5.0,
                                    funding_score   = 0.0,
                                    ofi             = ofi_now,
                                    lead_lag_dir    = lead_dir_now,
                                    regime          = regime_name,
                                    rsi             = 50.0,
                                    adx             = 20.0,
                                    atr             = float(df['close'].iloc[-1]) * 0.005,
                                    close           = float(df['close'].iloc[-1]),
                                    ema_fast        = float(df['close'].iloc[-1]),
                                    ema_slow        = float(df['close'].iloc[-1]),
                                    volume_ratio    = 1.0,
                                    funding_rate    = funding_rate,
                                )
                                _entry_path_tag = 'fast-track'
                                logger.info(f"[FAST-TRACK] {symbol} OFI={ofi_now:+.2f} Lead={lead_dir_now} → {ft_signal.value} conf={ft_conf:.0f}")

                # Update indicators dashboard
                indicators[symbol] = {
                    'signal':     sig.signal.value,
                    'confidence': round(sig.confidence, 1),
                    'rsi':        round(sig.rsi, 2),
                    'adx':        round(sig.adx, 2),
                    'atr':        round(sig.atr, 4),
                    'ofi':        round(sig.ofi, 3) if sig.ofi is not None else None,
                    'lead_lag':   sig.lead_lag_dir,
                    'regime':     sig.regime,
                    'size_mult':  sig.size_mult,
                    'scores': {
                        'ofi':      round(sig.ofi_score, 1),
                        'lead_lag': round(sig.lead_lag_score, 1),
                        'regime':   round(sig.regime_score, 1),
                        'rsi':      round(sig.rsi_score, 1),
                        'tech':     round(sig.technical_score, 1),
                        'funding':  round(sig.funding_score, 1),
                    }
                }

                pos      = trader.account.positions.get(symbol)
                pos_side = pos.side if pos else None

                # ── Funnel: classify this signal (source-level) ────────────────
                funnel.bump('signals_seen')
                if sig.signal == Signal.HOLD:
                    funnel.bump('hold')
                elif pos is None:
                    funnel.bump('actionable')   # tradeable entry signal, no position

                # ── Entry: minimum confidence gate ─────────────────────────────
                min_conf = _adapt['min_confidence']

                # ── Dual-direction probe ───────────────────────────────────────
                # The signal generator picks ONE direction per tick based on
                # indicator alignment. Some setups have edges on both sides; the
                # generator may pick the weaker one. Here we re-evaluate the
                # OPPOSITE direction through the same prob gate. Three outcomes:
                #   - opposite leads by ≥ DUAL_MARGIN  → flip the signal
                #   - both within margin              → reject both (noisy tape)
                #   - original leads or single-sided  → leave unchanged
                # Cost: one extra prob_gate.evaluate call per tradeable tick.
                if (prob_gate and pos is None and (sig.is_buy or sig.is_sell)
                        and os.getenv("DUAL_DIRECTION_ENABLED", "1") == "1"):
                    _dual_margin = float(os.getenv("DUAL_DIRECTION_MARGIN", "0.05"))
                    _orig_buy = bool(sig.is_buy)
                    try:
                        _orig = prob_gate.evaluate(
                            sig, is_buy=_orig_buy, entry_path=_entry_path_tag,
                            lead_strength=lead_lag.get_strength(symbol) or 0.0,
                            htf_alignment=htf_filter.alignment_score(symbol, is_buy=_orig_buy),
                            macro_state=macro_provider.current(),
                            symbol=symbol,
                        )
                        _opp = prob_gate.evaluate(
                            sig, is_buy=(not _orig_buy), entry_path=_entry_path_tag,
                            lead_strength=lead_lag.get_strength(symbol) or 0.0,
                            htf_alignment=htf_filter.alignment_score(symbol, is_buy=(not _orig_buy)),
                            macro_state=macro_provider.current(),
                            symbol=symbol,
                        )
                        _delta = _opp.calibrated_p - _orig.calibrated_p
                        if _delta > _dual_margin and not _opp.rejected:
                            logger.info(
                                f"[DUAL-FLIP] {symbol} "
                                f"{'BUY' if _orig_buy else 'SELL'}→"
                                f"{'SELL' if _orig_buy else 'BUY'} "
                                f"orig P={_orig.calibrated_p:.2f} opp P={_opp.calibrated_p:.2f}"
                            )
                            sig.is_buy  = (not _orig_buy)
                            sig.is_sell = _orig_buy
                        elif (not _orig.rejected and not _opp.rejected
                              and abs(_delta) <= _dual_margin):
                            # Both directions clear the gate within margin →
                            # contradictory edges → reject the whole bar.
                            logger.info(
                                f"[DUAL-REJECT] {symbol} both directions pass within "
                                f"±{_dual_margin:.2f} (long P={_orig.calibrated_p if _orig_buy else _opp.calibrated_p:.2f}, "
                                f"short P={_opp.calibrated_p if _orig_buy else _orig.calibrated_p:.2f}) — noisy tape"
                            )
                            funnel.bump('skip:dual_noisy')
                            sig.is_buy = False
                            sig.is_sell = False
                    except Exception as _e:
                        logger.debug(f"[DUAL] probe failed for {symbol}: {_e}")

                # ── LONG ENTRY ──────────────────────────────────────────────────
                if sig.is_buy and pos is None and not daily_entries_halted:
                    try:
                        bar_ts = float(df.index[-1].timestamp())
                    except Exception:
                        bar_ts = now_ts
                    cb_ok, cb_reason = circuit_breaker.can_enter()
                    long_ctx = CheckContext(
                        symbol=symbol, side='buy', sig=sig,
                        regime_name=regime_name, min_confidence=min_conf,
                        now_ts=now_ts, bar_ts=bar_ts,
                        last_exit_reason=last_exit_reason.get(symbol, ''),
                        last_exit_time=last_exit_time.get(symbol, 0),
                        last_entry_bar_ts=last_entry_bar.get(symbol),
                        cooldown_for=_cooldown_for,
                        last_ws_price_time=last_ws_price_time.get(symbol, 0),
                        ws_staleness_sec=WS_PRICE_STALENESS_SEC,
                        open_positions_count=len(trader.account.positions),
                        max_open_positions=MAX_OPEN_POSITIONS,
                        sentiment_allows=(sentiment_monitor.allows_long(symbol)
                                          if sentiment_monitor else True),
                        kill_filter_reason=_kill_filter_skip(symbol, df, side='buy'),
                        circuit_breaker_reason=None if cb_ok else cb_reason,
                        current_spread_pct=spread_tracker.current(symbol),
                        median_spread_pct=spread_tracker.median(symbol),
                        vpin=vpin_monitor.current(symbol),
                        vpin_threshold=_VPIN_THRESH,
                    )
                    cl_result = long_checklist.run(long_ctx)
                    if not cl_result.passed:
                        funnel.bump('skip:checklist')
                        logger.info(f"[SKIP BUY] {symbol} — {cl_result.reason_summary()}")
                        logger.debug(f"[CHECKLIST BUY] {symbol} {cl_result.trace()}")
                        continue
                    logger.info(f"[CHECKLIST BUY] {symbol} score={cl_result.score:.2f} "
                                f"{cl_result.short_trace()}")
                    # Anti-correlation: handled after the prob-gate (macro override below)
                    _corr_blocked_long = _has_correlated_position(symbol, 'buy')
                    last_entry_bar[symbol] = bar_ts

                    # Position size from confidence + equity (fallback if gate disabled)
                    size_usd = compute_position_size(sig.confidence, current_equity)
                    if size_usd < 1.50:
                        funnel.bump('skip:size')
                        continue

                    if vol_monitor:
                        size_usd *= vol_monitor.get_size_multiplier(symbol)

                    # When prob gate is off, checklist soft-score modulates size.
                    # Prob gate's tier sizing fully replaces this below if enabled.
                    if not prob_gate:
                        size_usd *= cl_result.score

                    # ── Probability gate (tier-based sizing overrides confidence sizing) ─
                    reasoning = None
                    if prob_gate:
                        reasoning = prob_gate.evaluate(
                            sig, is_buy=True, entry_path=_entry_path_tag,
                            lead_strength=lead_lag.get_strength(symbol) or 0.0,
                            htf_alignment=htf_filter.alignment_score(symbol, is_buy=True),
                            macro_state=macro_provider.current(),
                            symbol=symbol,
                        )
                        _praw = f" (raw {reasoning.combined_p:.2f})" if reasoning.calibration_active else ""
                        logger.info(
                            f"[PROB-GATE] {symbol} LONG  P={reasoning.calibrated_p:.2f}{_praw} "
                            f"scale={reasoning.size_scale:.2f} edges={len(reasoning.present_edges)} "
                            f"macro={reasoning.is_macro_driven}"
                        )
                        if reasoning.rejected:
                            funnel.bump('skip:probgate')
                            logger.info(f"[SKIP BUY] {symbol} — prob gate: {reasoning.rejection_reason}")
                            if notifier and NOTIFY_SKIPS:
                                notifier.send_trade_reasoning(
                                    symbol, "LONG", current_price, reasoning, size_usd, _entry_path_tag
                                )
                            continue
                        # Tier-based sizing: target USD from the conviction tier,
                        # scaled by Kelly. Capped by available equity.
                        size_usd = reasoning.target_usd * reasoning.size_scale
                        if reasoning.is_macro_driven and not symbol.startswith("BTC"):
                            size_usd *= alt_beta(symbol)
                            logger.info(f"[MACRO-CONTAGION] {symbol} size × β={alt_beta(symbol):.2f}")
                        size_usd = min(size_usd, current_equity * 0.95)  # never exceed cash
                        logger.info(f"[TIER] {symbol} {reasoning.tier} target=${reasoning.target_usd:.0f} "
                                    f"final=${size_usd:.2f} hold={reasoning.hold_minutes}min trail={reasoning.trail_style}")
                        if size_usd < 1.50:
                            funnel.bump('skip:size')
                            logger.info(f"[SKIP BUY] {symbol} — tier-scaled size below floor")
                            continue
                    # Apply correlation block — but allow macro-driven same-direction trades
                    if _corr_blocked_long and not (reasoning and reasoning.is_macro_driven):
                        funnel.bump('skip:corr')
                        logger.info(f"[SKIP BUY] {symbol} — correlated long already open")
                        continue
                    elif _corr_blocked_long and reasoning and reasoning.is_macro_driven:
                        logger.info(f"[CORR-OVERRIDE] {symbol} — macro-driven long, allowing concurrent")

                    # Expectancy gate: cap size until this path proves positive gross expectancy
                    _exp_cap = expectancy_gate.cap_for(_entry_path_tag)
                    if _exp_cap is not None and size_usd > _exp_cap:
                        logger.info(f"[EXPECTANCY] {symbol} {_entry_path_tag} unproven → "
                                    f"size ${size_usd:.2f}→${_exp_cap:.2f} (probe)")
                        size_usd = _exp_cap
                        if size_usd < 1.50:
                            funnel.bump('skip:size')
                            continue

                    position = trader.execute_buy(symbol, current_price, current_time,
                                                   size_usd=size_usd, signal=sig)
                    if position:
                        funnel.bump('exec:long')
                        # Tag entry pathway and snapshot market context for the journal
                        position.entry_path = _entry_path_tag
                        if reasoning:
                            position.prob_win = reasoning.combined_p
                            position.edges_used = [e.name for e in reasoning.present_edges]
                            position.tier = reasoning.tier
                            position.intended_hold_min = reasoning.hold_minutes
                            position.trail_style = reasoning.trail_style
                            position.target_usd_at_entry = reasoning.target_usd
                            position.prob_model_version = PROB_MODEL_VERSION
                        _annotate_position_context(position, symbol, sentiment_monitor, strategy)
                        if notifier:
                            if reasoning:
                                notifier.send_trade_reasoning(
                                    symbol, "LONG", current_price, reasoning, size_usd, _entry_path_tag
                                )
                            notifier.send_trade_alert(
                                action="BUY", symbol=symbol, price=current_price,
                                size=size_usd, signal=sig, entry_path=_entry_path_tag,
                            )

                # ── SHORT ENTRY ─────────────────────────────────────────────────
                elif sig.is_sell and pos is None and not daily_entries_halted:
                    try:
                        bar_ts = float(df.index[-1].timestamp())
                    except Exception:
                        bar_ts = now_ts
                    cb_ok, cb_reason = circuit_breaker.can_enter()
                    short_ctx = CheckContext(
                        symbol=symbol, side='sell', sig=sig,
                        regime_name=regime_name, min_confidence=min_conf,
                        now_ts=now_ts, bar_ts=bar_ts,
                        last_exit_reason=last_exit_reason.get(symbol, ''),
                        last_exit_time=last_exit_time.get(symbol, 0),
                        last_entry_bar_ts=last_entry_bar.get(symbol),
                        cooldown_for=_cooldown_for,
                        last_ws_price_time=last_ws_price_time.get(symbol, 0),
                        ws_staleness_sec=WS_PRICE_STALENESS_SEC,
                        open_positions_count=len(trader.account.positions),
                        max_open_positions=MAX_OPEN_POSITIONS,
                        sentiment_allows=True,   # no sentiment gate for shorts
                        kill_filter_reason=_kill_filter_skip(symbol, df, side='sell'),
                        circuit_breaker_reason=None if cb_ok else cb_reason,
                        current_spread_pct=spread_tracker.current(symbol),
                        median_spread_pct=spread_tracker.median(symbol),
                        vpin=vpin_monitor.current(symbol),
                        vpin_threshold=_VPIN_THRESH,
                    )
                    cl_result = short_checklist.run(short_ctx)
                    if not cl_result.passed:
                        funnel.bump('skip:checklist')
                        logger.info(f"[SKIP SELL] {symbol} — {cl_result.reason_summary()}")
                        logger.debug(f"[CHECKLIST SELL] {symbol} {cl_result.trace()}")
                        continue
                    logger.info(f"[CHECKLIST SELL] {symbol} score={cl_result.score:.2f} "
                                f"{cl_result.short_trace()}")
                    _corr_blocked_short = _has_correlated_position(symbol, 'short')
                    last_entry_bar[symbol] = bar_ts

                    size_usd = compute_position_size(sig.confidence, current_equity)
                    if size_usd < 1.50:
                        funnel.bump('skip:size')
                        continue
                    if vol_monitor:
                        size_usd *= vol_monitor.get_size_multiplier(symbol)
                    if not prob_gate:
                        size_usd *= cl_result.score

                    # ── Probability gate ─────────────────────────────────────────
                    reasoning = None
                    if prob_gate:
                        reasoning = prob_gate.evaluate(
                            sig, is_buy=False, entry_path=_entry_path_tag,
                            lead_strength=lead_lag.get_strength(symbol) or 0.0,
                            htf_alignment=htf_filter.alignment_score(symbol, is_buy=False),
                            macro_state=macro_provider.current(),
                            symbol=symbol,
                        )
                        _praw = f" (raw {reasoning.combined_p:.2f})" if reasoning.calibration_active else ""
                        logger.info(
                            f"[PROB-GATE] {symbol} SHORT P={reasoning.calibrated_p:.2f}{_praw} "
                            f"scale={reasoning.size_scale:.2f} edges={len(reasoning.present_edges)} "
                            f"macro={reasoning.is_macro_driven}"
                        )
                        if reasoning.rejected:
                            funnel.bump('skip:probgate')
                            logger.info(f"[SKIP SELL] {symbol} — prob gate: {reasoning.rejection_reason}")
                            if notifier and NOTIFY_SKIPS:
                                notifier.send_trade_reasoning(
                                    symbol, "SHORT", current_price, reasoning, size_usd, _entry_path_tag
                                )
                            continue
                        size_usd = reasoning.target_usd * reasoning.size_scale
                        if reasoning.is_macro_driven and not symbol.startswith("BTC"):
                            size_usd *= alt_beta(symbol)
                            logger.info(f"[MACRO-CONTAGION] {symbol} size × β={alt_beta(symbol):.2f}")
                        size_usd = min(size_usd, current_equity * 0.95)
                        logger.info(f"[TIER] {symbol} {reasoning.tier} target=${reasoning.target_usd:.0f} "
                                    f"final=${size_usd:.2f} hold={reasoning.hold_minutes}min trail={reasoning.trail_style}")
                        if size_usd < 1.50:
                            funnel.bump('skip:size')
                            logger.info(f"[SKIP SELL] {symbol} — tier-scaled size below floor")
                            continue
                    if _corr_blocked_short and not (reasoning and reasoning.is_macro_driven):
                        funnel.bump('skip:corr')
                        logger.info(f"[SKIP SELL] {symbol} — correlated short already open")
                        continue
                    elif _corr_blocked_short and reasoning and reasoning.is_macro_driven:
                        logger.info(f"[CORR-OVERRIDE] {symbol} — macro-driven short, allowing concurrent")

                    # Expectancy gate: cap size until this path proves positive gross expectancy
                    _exp_cap = expectancy_gate.cap_for(_entry_path_tag)
                    if _exp_cap is not None and size_usd > _exp_cap:
                        logger.info(f"[EXPECTANCY] {symbol} {_entry_path_tag} unproven → "
                                    f"size ${size_usd:.2f}→${_exp_cap:.2f} (probe)")
                        size_usd = _exp_cap
                        if size_usd < 1.50:
                            funnel.bump('skip:size')
                            continue

                    position = trader.execute_short(symbol, current_price, current_time,
                                                     size_usd=size_usd, signal=sig)
                    if position:
                        funnel.bump('exec:short')
                        position.entry_path = _entry_path_tag
                        if reasoning:
                            position.prob_win = reasoning.combined_p
                            position.edges_used = [e.name for e in reasoning.present_edges]
                            position.tier = reasoning.tier
                            position.intended_hold_min = reasoning.hold_minutes
                            position.trail_style = reasoning.trail_style
                            position.target_usd_at_entry = reasoning.target_usd
                            position.prob_model_version = PROB_MODEL_VERSION
                        _annotate_position_context(position, symbol, sentiment_monitor, strategy)
                        if notifier:
                            if reasoning:
                                notifier.send_trade_reasoning(
                                    symbol, "SHORT", current_price, reasoning, size_usd, _entry_path_tag
                                )
                            notifier.send_trade_alert(
                                action="SELL", symbol=symbol, price=current_price,
                                size=size_usd, signal=sig, entry_path=_entry_path_tag,
                            )

                # ── EXIT LONG ───────────────────────────────────────────────────
                elif sig.signal == Signal.SELL and pos_side == 'buy':
                    opposing_streak[symbol] = opposing_streak.get(symbol, 0) + 1
                    if opposing_streak[symbol] < SIGNAL_EXIT_STREAK:
                        logger.debug(f"[EXIT-DEBOUNCE] {symbol} long: opposing "
                                     f"{opposing_streak[symbol]}/{SIGNAL_EXIT_STREAK}")
                    else:
                        trade = trader.execute_sell(symbol, current_price, current_time, reason="SIGNAL")
                        if trade:
                            recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))
                            summary = trader.get_account_summary()
                            equity_curve.append({'t': _ts(current_time), 'v': round(summary['total_equity'], 2)})
                            _on_trade_closed(symbol, pos, trade, "SIGNAL", summary['total_equity'],
                                             notifier, journal, ml_scorer, calibrator)
                            _record_exit(symbol, "SIGNAL")
                            opposing_streak.pop(symbol, None)
                            just_halted, halt_msg = circuit_breaker.record_outcome(
                                won=(trade.pnl > 0), pnl=trade.pnl, symbol=symbol)
                            if just_halted and notifier:
                                notifier.send_message(halt_msg)

                # ── EXIT SHORT ──────────────────────────────────────────────────
                elif pos_side == 'short' and (
                    sig.signal == Signal.BUY or regime_name == 'TRENDING_UP'
                ):
                    # Regime flip to TRENDING_UP is a fast-exit (no debounce);
                    # signal flip requires the streak.
                    fast_exit = regime_name == 'TRENDING_UP'
                    if not fast_exit:
                        opposing_streak[symbol] = opposing_streak.get(symbol, 0) + 1
                    if not fast_exit and opposing_streak[symbol] < SIGNAL_EXIT_STREAK:
                        logger.debug(f"[EXIT-DEBOUNCE] {symbol} short: opposing "
                                     f"{opposing_streak[symbol]}/{SIGNAL_EXIT_STREAK}")
                    else:
                        trade = trader.execute_cover(symbol, current_price, current_time, reason="SIGNAL")
                        if trade:
                            recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))
                            summary = trader.get_account_summary()
                            equity_curve.append({'t': _ts(current_time), 'v': round(summary['total_equity'], 2)})
                            _on_trade_closed(symbol, pos, trade, "SIGNAL", summary['total_equity'],
                                             notifier, journal, ml_scorer, calibrator)
                            _record_exit(symbol, "SIGNAL")
                            opposing_streak.pop(symbol, None)
                            just_halted, halt_msg = circuit_breaker.record_outcome(
                                won=(trade.pnl > 0), pnl=trade.pnl, symbol=symbol)
                            if just_halted and notifier:
                                notifier.send_message(halt_msg)
                else:
                    # Any tick without an opposing signal resets the streak
                    if pos_side and opposing_streak.get(symbol, 0):
                        opposing_streak[symbol] = 0

            # Refresh CVaR weights
            if iteration % 50 == 0 and any(len(v) >= 20 for v in symbol_returns.values()):
                portfolio_opt.optimize(symbol_returns)

            # Refresh perp funding rates ~ every 5 min when in perp mode
            if trader.perp_mode and iteration % 150 == 0 and hasattr(exchange, 'fetch_funding_rate'):
                for sym in symbols:
                    try:
                        rate = await exchange.fetch_funding_rate(sym)
                        if rate is not None:
                            trader.set_funding_rate(sym, float(rate))
                    except Exception as fr_exc:
                        logger.debug(f"[PERPS] funding fetch failed for {sym}: {fr_exc}")

            # Update prices from WS
            if public_ws:
                for sym, p in public_ws.get_prices().items():
                    if sym in symbols:
                        prices[sym] = p
                        last_ws_price_time[sym] = time.time()
                        lead_lag.update_price(sym, p)
            liquidated_syms = trader.update_unrealized_pnl(prices)
            for liq_sym in liquidated_syms:
                liq_msg = f"⚠️ LIQUIDATED {liq_sym} — full margin lost (paper mode)"
                logger.warning(liq_msg)
                if notifier:
                    try:
                        notifier.send_message(liq_msg)
                    except Exception as _liq_err:
                        logger.debug(f"[NOTIFY] liquidation alert failed: {_liq_err}")
            if trader.perp_mode:
                trader.accrue_funding(datetime.now(timezone.utc))

            # Heartbeat — log alive status once a minute so we know it's running
            now_hb = time.time()
            if now_hb - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                last_heartbeat = now_hb
                s_hb = trader.get_account_summary()
                wr_hb = (s_hb['winning_trades'] / s_hb['closed_trades'] * 100) if s_hb['closed_trades'] else 0.0
                open_syms = ','.join(trader.account.positions.keys()) or 'none'
                # Fold in the funding-arb arms for running combined totals. Read
                # net P&L straight from the in-memory arm objects — NOT from the
                # shared state.json, which the main loop rewrites every tick and
                # clobbers the funding_arb* keys the merge task writes only every
                # 65s (the state-flush race that made this read +0.00 ~always).
                # Three arms: aggressive (Binance/Bybit, fantasy baseline), majors
                # (Binance/Bybit majors-only, also fantasy), kraken (the only arm
                # whose +$X represents actually-capturable edge).
                def _arm_pnl(_arm):
                    try:
                        return float(_arm.get_summary()['total_pnl']) if _arm else 0.0
                    except Exception:
                        return 0.0
                # Rolling 7d window for the Kraken arm — its lifetime total is
                # dominated by dead pre-whitelist legacy losses, so the 7d figure
                # is what actually reflects the CURRENT config's edge.
                def _arm_pnl_7d(_arm):
                    try:
                        return float(_arm.get_summary().get('pnl_7d', 0.0)) if _arm else 0.0
                    except Exception:
                        return 0.0
                # Borrow-corrected lifetime: charges every short-spot leg the carry
                # it owes. For the aggressive arm this strips the unpaid-borrow
                # illusion (booked +$16 vs honest ≈ −$14).
                def _arm_pnl_adj(_arm):
                    try:
                        return float(_arm.get_summary().get('borrow_corrected_pnl', 0.0)) if _arm else 0.0
                    except Exception:
                        return 0.0
                _fa_hb  = _arm_pnl(_funding_arb_sim)
                _fa_adj = _arm_pnl_adj(_funding_arb_sim)
                _fam_hb = _arm_pnl(_funding_arb_majors)
                _fak_hb = _arm_pnl(_funding_arb_kraken)
                _fak_7d = _arm_pnl_7d(_funding_arb_kraken)
                _combined_hb = s_hb['total_pnl'] + _fa_hb + _fam_hb + _fak_hb
                _total_money_hb = s_hb['total_equity'] + _fa_hb + _fam_hb + _fak_hb
                logger.info(
                    f"[HEARTBEAT] equity=${s_hb['total_equity']:.2f} "
                    f"pnl=${s_hb['total_pnl']:+.2f} ({s_hb['pnl_pct']:+.2f}%) "
                    f"trades={s_hb['closed_trades']}({s_hb['winning_trades']}W/{s_hb['losing_trades']}L WR={wr_hb:.0f}%) "
                    f"open={open_syms} min_conf={_adapt['min_confidence']:.0f} "
                    f"| TOTAL=${_total_money_hb:.2f} netPnL=${_combined_hb:+.2f} "
                    f"(arb kraken={_fak_hb:+.2f}[7d {_fak_7d:+.2f}] maj={_fam_hb:+.2f} "
                    f"aggr={_fa_hb:+.2f}[borrow-adj {_fa_adj:+.2f}])"
                )
                # Entry funnel — where signals died since the last heartbeat
                logger.info(f"[FUNNEL] {funnel.render()}")
                funnel.reset()
                # Refresh expectancy-gate stats (growth-gated → cheap until trades close)
                if expectancy_gate.update(journal):
                    logger.info(f"[EXPECTANCY] {expectancy_gate.status()}")
                # Persist open positions so a crash/restart can resume
                _save_open_positions(trader)

            # Periodic account report to Telegram — made/lost + total money.
            # Combines the directional paper account with all three funding-arb
            # arms, read straight from the in-memory arm objects (race-free —
            # see the heartbeat note above on the state.json clobber race).
            # Fires on its own cadence (default hourly).
            if notifier and (now_hb - last_account_report >= ACCOUNT_REPORT_INTERVAL_SEC):
                last_account_report = now_hb
                try:
                    s_rep = trader.get_account_summary()
                    wr_rep = (s_rep['winning_trades'] / s_rep['closed_trades'] * 100) \
                        if s_rep['closed_trades'] else 0.0
                    def _arm_pnl_rep(_arm):
                        try:
                            return float(_arm.get_summary()['total_pnl']) if _arm else 0.0
                        except Exception:
                            return 0.0
                    fa_pnl  = _arm_pnl_rep(_funding_arb_sim)
                    fam_pnl = _arm_pnl_rep(_funding_arb_majors)
                    fak_pnl = _arm_pnl_rep(_funding_arb_kraken)
                    dir_pnl, dir_eq = s_rep['total_pnl'], s_rep['total_equity']
                    combined = dir_pnl + fa_pnl + fam_pnl + fak_pnl
                    total_money = dir_eq + fa_pnl + fam_pnl + fak_pnl
                    open_syms = ', '.join(trader.account.positions.keys()) or 'none'
                    mark = '🟢' if combined >= 0 else '🔴'
                    verb = 'up' if combined >= 0 else 'down'
                    notifier.send_message(
                        f"📊 <b>Account Report</b> (paper)\n"
                        f"💰 Total money: <b>${total_money:,.2f}</b>\n"
                        f"{mark} Net P&amp;L: <b>${combined:+,.2f}</b> ({verb} since start)\n\n"
                        f"• Directional book: ${dir_eq:,.2f} equity "
                        f"(P&amp;L ${dir_pnl:+,.2f} / {s_rep['pnl_pct']:+.2f}%, "
                        f"{s_rep['closed_trades']} trades, {wr_rep:.0f}% WR)\n"
                        f"• Funding Arb (Kraken, executable): ${fak_pnl:+,.2f}\n"
                        f"• Funding Arb (majors, baseline): ${fam_pnl:+,.2f}\n"
                        f"• Funding Arb (aggressive, baseline): ${fa_pnl:+,.2f}\n"
                        f"• Open positions: {open_syms}"
                    )
                    logger.info(f"[ACCOUNT REPORT] sent — total=${total_money:.2f} "
                                f"netPnL=${combined:+.2f} (dir={dir_pnl:+.2f} "
                                f"kraken={fak_pnl:+.2f} majors={fam_pnl:+.2f} aggr={fa_pnl:+.2f})")
                except Exception as e:
                    logger.warning(f"[ACCOUNT REPORT] failed: {e}")

            # Daily summary — fires once when UTC date rolls over
            today_utc = datetime.now(timezone.utc).date()
            if today_utc != last_daily_summary_date and notifier:
                try:
                    yesterday = last_daily_summary_date
                    # Trades that closed on the previous UTC day
                    day_trades = [
                        r for r in journal.records
                        if r.closed_at[:10] == yesterday.isoformat()
                    ]
                    n = len(day_trades)
                    wins = sum(1 for r in day_trades if r.won)
                    losses = n - wins
                    best  = max((r.pnl for r in day_trades), default=0.0)
                    worst = min((r.pnl for r in day_trades), default=0.0)

                    # Build path + regime breakdowns
                    def _group(items, key_fn):
                        out = {}
                        for r in items:
                            k = key_fn(r)
                            g = out.setdefault(k, {'n': 0, 'wins': 0, 'pnl': 0.0})
                            g['n'] += 1
                            if r.won: g['wins'] += 1
                            g['pnl'] += r.pnl
                        return {
                            k: {'n': v['n'],
                                'win_rate': (v['wins']/v['n']*100) if v['n'] else 0.0,
                                'total_pnl': v['pnl']}
                            for k, v in out.items()
                        }

                    path_stats   = _group(day_trades, lambda r: r.entry_path) if n else None
                    regime_stats = _group(day_trades, lambda r: r.regime)     if n else None

                    s_sum = trader.get_account_summary()
                    notifier.send_daily_summary(
                        total_equity   = s_sum['total_equity'],
                        start_equity   = session_start_equity,
                        trades         = n, wins = wins, losses = losses,
                        best_trade     = best, worst_trade = worst,
                        path_stats     = path_stats,
                        regime_stats   = regime_stats,
                        open_positions = len(trader.account.positions),
                    )
                except Exception as e:
                    logger.error(f"[DAILY] summary send failed: {e}")
                last_daily_summary_date = today_utc

            # State for dashboard
            summary = trader.get_account_summary()
            positions_data = {
                sym: {
                    'entry_time':    pos.entry_time.isoformat() if hasattr(pos.entry_time, 'isoformat') else str(pos.entry_time),
                    'entry_price':   pos.entry_price,
                    'size':          pos.size,
                    'side':          pos.side,
                    'unrealized_pnl': round(pos.unrealized_pnl, 4),
                    'confidence':    pos.entry_signal.confidence if pos.entry_signal else 0,
                    'regime':        pos.entry_signal.regime if pos.entry_signal else 'UNKNOWN',
                }
                for sym, pos in trader.account.positions.items()
            }
            sentiment_data = (
                sentiment_monitor.get_snapshot().to_dict()
                if sentiment_monitor and sentiment_monitor.get_snapshot() else None
            )
            write_state(_sanitize({
                'status':        'running',
                'mode':          mode,
                'started_at':    trader._started_at,
                'iteration':     iteration,
                'account':       summary,
                'positions':     positions_data,
                'prices':        {k: round(v, 2) for k, v in prices.items()},
                'indicators':    indicators,
                'recent_trades': list(recent_trades),
                'equity_curve':  list(equity_curve),
                'sentiment':     sentiment_data,
                'regime':        regime_cache.get(symbols[0]) if regime_cache else None,
                'regime_all':    regime_cache,
                'cvar':          portfolio_opt.to_dict(),
                'iv':            vol_monitor.to_dict() if vol_monitor else {},
                'ws_connected':  public_ws is not None,
                'journal':       journal.stats(),
                'adaptations':   {k: v for k, v in _adapt.items() if k != 'updated_at'},
                'lead_lag_signals': {s: lead_lag.get_signal(s) for s in symbols},
                'ofi':           {s: round(ofi_calc.get_smoothed(s), 3) if ofi_calc.get_smoothed(s) else None for s in symbols},
            }))

            if iteration % 30 == 0:   # every 60s (30 × 2s)
                s  = trader.get_account_summary()
                wr = round(s['winning_trades'] / s['closed_trades'] * 100, 1) if s['closed_trades'] else 0
                logger.info(
                    f"Tick {iteration}: equity=${s['total_equity']:,.2f}  "
                    f"pnl=${s['total_pnl']:+.2f}  "
                    f"trades={s['closed_trades']}(WR={wr}%)  "
                    f"min_conf={_adapt['min_confidence']:.0f}  "
                    f"streak=W{_adapt['win_streak']}L{_adapt['loss_streak']}"
                )

        except asyncio.CancelledError:
            break
        except CircuitBreakerOpen as e:
            wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
            logger.warning(
                f"[LOOP] Exchange circuit breaker open — pausing main loop {wait:.0f}s "
                f"({e})"
            )
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error(f"Error in paper trading loop: {e}", exc_info=True)
            await asyncio.sleep(5)

    trader.running = False
    logger.info("Paper trading session ended")
    learner.log_summary()

    # Stop funding-arb background tasks (best-effort)
    for _t in _funding_tasks:
        _t.cancel()

    # Shutdown notification — best-effort (systemd may have already SIGKILL'd us)
    if notifier:
        try:
            _save_open_positions(trader)   # persist before we go down
            s_end = trader.get_account_summary()
            session_pnl = s_end['total_equity'] - session_start_equity
            sign = '+' if session_pnl >= 0 else ''
            open_n = len(trader.account.positions)
            notifier.send_message(
                f"🔴 <b>Bot shutting down</b>\n"
                f"Session P&L: <b>{sign}${session_pnl:.2f}</b>\n"
                f"Equity: <b>${s_end['total_equity']:.2f}</b>\n"
                f"Trades: {s_end['closed_trades']} ({s_end['winning_trades']}W/{s_end['losing_trades']}L)"
                + (f"\n⚠️ {open_n} position(s) still open — will resume on restart" if open_n else "")
            )
        except Exception as e:
            logger.error(f"[SHUTDOWN] notify failed: {e}")


# ── Post-trade handler ─────────────────────────────────────────────────────────

def _on_trade_closed(symbol: str, pos: 'PaperPosition', trade: Trade,
                     exit_reason: str, total_equity: float,
                     notifier: Optional[TelegramNotifier], journal: TradeJournal,
                     ml_scorer: Optional['MLScorer'] = None,
                     calibrator=None):
    if pos is None:
        return

    sig = pos.entry_signal
    entry_dt    = pos.entry_time if isinstance(pos.entry_time, datetime) else datetime.now(timezone.utc)
    exit_dt     = trade.exit_time if isinstance(trade.exit_time, datetime) else datetime.now(timezone.utc)
    holding_min = (exit_dt - entry_dt).total_seconds() / 60

    issues, positives = _diagnose(pos.side, trade.pnl, exit_reason, holding_min, sig) if sig else ([], [])

    _record_to_journal(journal, trade, symbol, exit_reason, sig, sig.regime if sig else 'UNKNOWN', pos=pos)

    # Retrain ML model if enough new data has accumulated
    if ml_scorer is not None and ml_scorer.should_retrain():
        logger.info("[ML] Retraining triggered after new trades")
        ml_scorer.train()

    # Refit the probability calibrator on the freshly-updated journal (no-op
    # until it has grown by CALIB_REFIT_EVERY trades since the last fit).
    if calibrator is not None:
        try:
            if calibrator.maybe_refit(journal):
                logger.info("[CALIB] refit on %d trades (active=%s)",
                            calibrator._n_fit, calibrator.is_active)
        except Exception as e:
            logger.warning(f"[CALIB] refit skipped: {e}")

    adaptations = _update_streaks_and_adapt(trade.pnl > 0, notifier)

    # Compute MFE/MAE from the position's tracked peaks
    mfe_pct = mae_pct = 0.0
    if pos.entry_price > 0:
        if pos.side == 'buy':
            mfe_pct = (pos.peak_favorable_price - pos.entry_price) / pos.entry_price * 100
            mae_pct = (pos.peak_adverse_price   - pos.entry_price) / pos.entry_price * 100
        else:
            mfe_pct = (pos.entry_price - pos.peak_favorable_price) / pos.entry_price * 100
            mae_pct = (pos.entry_price - pos.peak_adverse_price)   / pos.entry_price * 100

    if notifier and sig:
        fr_apy = sig.funding_rate * 3 * 365 * 100 if sig.funding_rate else None
        notifier.send_trade_analysis(
            symbol          = symbol,
            side            = pos.side,
            pnl             = trade.pnl,
            pnl_pct         = trade.pnl_pct,
            entry_price     = pos.entry_price,
            exit_price      = trade.exit_price,
            total_equity    = total_equity,
            exit_reason     = exit_reason,
            holding_minutes = holding_min,
            regime          = sig.regime,
            regime_conf     = 0.0,
            rsi             = sig.rsi,
            adx             = sig.adx,
            volume_ratio    = sig.volume_ratio,
            ofi             = sig.ofi,
            funding_apy     = fr_apy,
            btc_lead        = sig.lead_lag_dir,
            issues          = issues,
            positives       = positives,
            loss_streak     = _adapt['loss_streak'],
            win_streak      = _adapt['win_streak'],
            adaptations     = adaptations if adaptations else None,
            entry_path      = getattr(pos, 'entry_path', 'main'),
            mfe_pct         = mfe_pct,
            mae_pct         = mae_pct,
        )
    elif notifier:
        fn = notifier.send_win if trade.pnl >= 0 else notifier.send_loss
        fn(symbol, trade.pnl, trade.pnl_pct, trade.exit_price, total_equity, reason=exit_reason)


# ── Utilities ──────────────────────────────────────────────────────────────────

_POSITIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'open_positions.json')


def _save_open_positions(trader):
    """Dump open positions to disk so a restart can resume them.

    Writes to a sibling .tmp file then atomically renames it into place so a
    crash mid-write never leaves a corrupted JSON file on disk.
    """
    def _iso(dt):
        return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)

    _tmp = _POSITIONS_FILE + '.tmp'
    try:
        os.makedirs(os.path.dirname(_POSITIONS_FILE), exist_ok=True)
        out = []
        for sym, pos in trader.account.positions.items():
            out.append({
                'symbol':           sym,
                'side':             pos.side,
                'entry_time':       _iso(pos.entry_time),
                'entry_price':      pos.entry_price,
                'size':             pos.size,
                'entry_fee':        pos.entry_fee,
                'peak_favorable':   pos.peak_favorable_price,
                'peak_adverse':     pos.peak_adverse_price,
                'entry_path':       pos.entry_path,
                'size_usd_target':  pos.size_usd_target,
                # Trailing stop / conviction tier
                'tier':             pos.tier,
                'intended_hold_min': pos.intended_hold_min,
                'trail_style':      pos.trail_style,
                'trail_stop_price': pos.trail_stop_price,
                'target_usd_at_entry': pos.target_usd_at_entry,
                # Probability gate context
                'prob_win':         pos.prob_win,
                'edges_used':       pos.edges_used,
                # Entry context snapshot
                'spread_at_entry':  pos.spread_at_entry,
                'sentiment_fng':    pos.sentiment_fng,
                'sentiment_btc_dom': pos.sentiment_btc_dom,
                # Perp-specific fields (zeros in spot mode, important for perp resume)
                'is_perp':          pos.is_perp,
                'leverage':         pos.leverage,
                'margin_locked':    pos.margin_locked,
                'funding_accrued':  pos.funding_accrued,
                'last_funding_ts':  _iso(pos.last_funding_ts) if pos.last_funding_ts else None,
            })
        payload = json.dumps({
            'saved_at': datetime.now(timezone.utc).isoformat(),
            'cash':     trader.account.cash,
            'total_pnl': trader.account.total_pnl,
            'positions': out,
        }, indent=2)
        # Write to tmp first, then rename — atomic on POSIX; prevents a
        # mid-write crash from leaving a truncated/corrupt positions file.
        with open(_tmp, 'w') as f:
            f.write(payload)
        os.replace(_tmp, _POSITIONS_FILE)
    except Exception as e:
        logger.error(f"[POSITIONS] Failed to save open positions to {_POSITIONS_FILE}: {e}")
        # Clean up the temp file if it was created before the failure.
        try:
            if os.path.exists(_tmp):
                os.remove(_tmp)
        except OSError:
            pass


def _load_open_positions(trader):
    """Restore positions from disk on startup. Returns number restored."""
    if not os.path.exists(_POSITIONS_FILE):
        return 0
    try:
        with open(_POSITIONS_FILE) as f:
            state = json.load(f)
        saved_at_raw = state.get('saved_at')
        if saved_at_raw:
            try:
                saved_at = datetime.fromisoformat(saved_at_raw)
                age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600.0
                if age_hours > 1.0:
                    logger.warning(
                        f"[POSITIONS] Restoring positions from a snapshot that is "
                        f"{age_hours:.1f}h old — entry prices may differ from current market"
                    )
            except Exception:
                pass
        restored = 0
        for p in state.get('positions', []):
            try:
                last_funding_raw = p.get('last_funding_ts')
                last_funding_ts  = datetime.fromisoformat(last_funding_raw) if last_funding_raw else None

                pos = PaperPosition(
                    entry_time      = datetime.fromisoformat(p['entry_time']),
                    entry_price     = float(p['entry_price']),
                    size            = float(p['size']),
                    side            = p['side'],
                    entry_fee       = float(p.get('entry_fee', 0.0)),
                    peak_favorable_price = float(p.get('peak_favorable', p['entry_price'])),
                    peak_adverse_price   = float(p.get('peak_adverse',   p['entry_price'])),
                    entry_path      = p.get('entry_path', 'main'),
                    size_usd_target = float(p.get('size_usd_target', 0.0)),
                    # Trailing stop / conviction tier
                    tier                 = p.get('tier', 'scalp'),
                    intended_hold_min    = int(p.get('intended_hold_min', 0)),
                    trail_style          = p.get('trail_style', 'atr_stop'),
                    trail_stop_price     = float(p.get('trail_stop_price', 0.0)),
                    target_usd_at_entry  = float(p.get('target_usd_at_entry', 0.0)),
                    # Probability gate context
                    prob_win             = float(p.get('prob_win', 0.0)),
                    edges_used           = list(p.get('edges_used', [])),
                    # Entry context snapshot
                    spread_at_entry      = float(p.get('spread_at_entry', 0.0)),
                    sentiment_fng        = p.get('sentiment_fng'),
                    sentiment_btc_dom    = p.get('sentiment_btc_dom'),
                    # Perp-specific fields
                    is_perp              = bool(p.get('is_perp', False)),
                    leverage             = float(p.get('leverage', 1.0)),
                    margin_locked        = float(p.get('margin_locked', 0.0)),
                    funding_accrued      = float(p.get('funding_accrued', 0.0)),
                    last_funding_ts      = last_funding_ts,
                )
                trader.account.positions[p['symbol']] = pos
                restored += 1
            except Exception as e:
                logger.warning(f"[POSITIONS] Could not restore position {p.get('symbol', '?')}: {e}")
                continue
        # Restore cash/PnL from the snapshot whenever positions were restored.
        # The snapshot's cash already has each restored position's margin/notional
        # deducted, so we MUST adopt it — keeping the fresh initial_capital while
        # re-adding positions double-counts equity (a winning session, where
        # saved cash > initial_capital, was previously skipped by the old guard).
        if restored and 'cash' in state:
            trader.account.cash = float(state['cash'])
            trader.account.total_pnl = float(state.get('total_pnl', trader.account.total_pnl))
        return restored
    except Exception as e:
        logger.error(f"[POSITIONS] Failed to load open positions from {_POSITIONS_FILE}: {e}")
        return 0


def _annotate_position_context(position, symbol, sentiment_monitor, strategy):
    """Snapshot market context onto a freshly-opened position for the journal."""
    try:
        # Spread from microstructure strategy's order book state
        if hasattr(strategy, 'ofi_states') and symbol in strategy.ofi_states:
            state = strategy.ofi_states.get(symbol)
            if state and hasattr(state, 'spread'):
                position.spread_at_entry = float(state.spread or 0.0)
    except Exception:
        pass
    try:
        if sentiment_monitor:
            snap = sentiment_monitor.get_snapshot()
            if snap:
                position.sentiment_fng     = getattr(snap, 'fear_greed', None)
                position.sentiment_btc_dom = getattr(snap, 'btc_dominance', None)
    except Exception:
        pass


def _inject_live_price(df: pd.DataFrame, live_price: float) -> pd.DataFrame:
    """Replace the last candle's close (and adjust high/low) with the live WebSocket price."""
    df = df.copy()
    idx = df.index[-1]
    df.at[idx, 'close'] = live_price
    df.at[idx, 'high']  = max(float(df.at[idx, 'high']),  live_price)
    df.at[idx, 'low']   = min(float(df.at[idx, 'low']),   live_price)
    return df


def _ts(t) -> str:
    return t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)


def _sanitize(obj):
    """Recursively replace NaN/Inf with None so JSON serialization never fails."""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


def _trade_to_dict(trade: Trade, symbol: str, reason: str) -> dict:
    return {
        'symbol':      symbol,
        'entry_time':  trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
        'exit_time':   trade.exit_time.isoformat()  if hasattr(trade.exit_time,  'isoformat') else str(trade.exit_time),
        'entry_price': round(trade.entry_price, 4),
        'exit_price':  round(trade.exit_price,  4),
        'size':        trade.size,
        'pnl':         round(trade.pnl, 4),
        'pnl_pct':     round(trade.pnl_pct, 2),
        'reason':      reason,
    }


if __name__ == '__main__':
    async def main():
        from .exchange import ExchangeConnection
        exchange = ExchangeConnection(sandbox=False)
        await exchange.connect()
        trader = PaperTrader(initial_capital=100)
        try:
            await asyncio.wait_for(
                run_paper_trading_session(exchange, trader, ['BTC/USD', 'ETH/USD'], '1m'),
                timeout=300,
            )
        except asyncio.TimeoutError:
            pass
        trader.print_summary()
        await exchange.disconnect()

    asyncio.run(main())
