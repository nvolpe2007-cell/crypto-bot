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
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import logging

import pandas as pd
import pandas_ta as _pta

from .indicators import Signal, prepare_ohlcv_dataframe
from .scientific_strategy import ScientificStrategy, ScientificSignal, compute_position_size, _size_multiplier as _get_size_mult
from .microstructure_strategy import MicrostructureStrategy, MicrostructureSignal
from .exchange import ExchangeConnection
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS
from .regime_detector import RegimeDetector
from .portfolio_optimizer import PortfolioOptimizer
from .crypto_vol import CryptoVolMonitor
from .order_flow import OrderFlowImbalance
from .lead_lag_detector import LeadLagDetector
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .state import write_state, read_state
from .ml_scorer import MLScorer
from .multi_timeframe import MultiTimeframeFilter
from .mean_reversion_strategy import MeanReversionStrategy, MRSignal

logger = logging.getLogger(__name__)

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
    'min_confidence':     38.0,   # starts permissive, tightens adaptively after losses
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
        os.makedirs('logs', exist_ok=True)
        _adapt['updated_at'] = datetime.now(timezone.utc).isoformat()
        with open(_ADAPT_FILE, 'w') as f:
            json.dump(_adapt, f, indent=2)
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

    # After 3 losses in a row, raise the bar
    if _adapt['loss_streak'] == 3 and _adapt['min_confidence'] < 68:
        _adapt['min_confidence'] = min(68.0, _adapt['min_confidence'] + 4.0)
        changes.append(f"min confidence raised to {_adapt['min_confidence']:.0f}")

    # After 5 wins in a row, can slightly relax (bot has edge in current conditions)
    if _adapt['win_streak'] == 5 and _adapt['min_confidence'] > 60:
        _adapt['min_confidence'] = max(60.0, _adapt['min_confidence'] - 2.0)
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


def _record_to_journal(journal, trade, symbol, reason, sig, regime):
    now = datetime.now(timezone.utc)
    record = TradeRecord(
        trade_id    = f"{symbol}_{int(now.timestamp())}",
        symbol      = symbol,
        opened_at   = trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
        closed_at   = now.isoformat(),
        rsi          = sig.rsi if sig else 50.0,
        adx          = sig.adx if sig else 20.0,
        volume_ratio = sig.volume_ratio if sig else 1.0,
        regime       = regime,
        atr_pct      = (sig.atr / sig.close * 100) if sig and sig.atr and sig.close else 1.0,
        ema100_gap   = 0.0,
        ema200_gap   = 0.0,
        hour_utc     = now.hour,
        day_of_week  = now.weekday(),
        pnl          = round(trade.pnl, 4),
        pnl_pct      = round(trade.pnl_pct, 2),
        won          = trade.pnl > 0,
        reason       = reason,
        # Extended ML features from signal context
        ofi               = float(sig.ofi or 0.0) if sig else 0.0,
        # Normalized 0-1: positive score means lead-lag confirmed direction
        lead_lag_strength = max(0.0, sig.lead_lag_score) / 20.0 if sig else 0.0,
        # True when BTC lead direction matched the entry direction
        lead_lag_aligned  = (sig.lead_lag_dir == ('BUY' if (sig and sig.signal == Signal.BUY) else 'SELL')) if sig and sig.lead_lag_dir else False,
        confidence        = float(sig.confidence) if sig else 0.0,
        ofi_score         = float(sig.ofi_score) if sig else 0.0,
        lead_lag_score    = float(sig.lead_lag_score) if sig else 0.0,
        regime_score      = float(sig.regime_score) if sig else 0.0,
        regime_confidence = 0.5,
        funding_rate      = float(sig.funding_rate or 0.0) if sig else 0.0,
        # Entry direction — derived from signal, not the close-trade action
        direction         = 'buy' if sig and sig.signal == Signal.BUY else 'short',
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
                 fee_pct: float = 0.26,
                 slippage_pct: float = 0.1,
                 stop_loss_pct: float = 2.0,
                 take_profit_pct: float = 3.0):
        self.initial_capital  = initial_capital
        self.account          = PaperAccount(initial_capital=initial_capital, cash=initial_capital)
        self.position_size    = position_size
        self.fee_pct          = fee_pct / 100
        self.slippage_pct     = slippage_pct / 100
        self.stop_loss_pct    = stop_loss_pct / 100
        self.take_profit_pct  = take_profit_pct / 100
        self.running          = False
        self._started_at: Optional[str] = None

    def execute_buy(self, symbol: str, price: float, timestamp: datetime,
                    size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        size       = size_usd / price
        exec_price = price * (1 + self.slippage_pct)
        fee        = exec_price * size * self.fee_pct
        total_cost = exec_price * size + fee

        if total_cost > self.account.cash:
            size       = (self.account.cash * 0.98) / (exec_price * (1 + self.fee_pct))
            fee        = exec_price * size * self.fee_pct
            total_cost = exec_price * size + fee

        if size <= 0 or total_cost > self.account.cash:
            return None

        self.account.cash -= total_cost
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='buy', entry_fee=fee, entry_signal=signal)
        self.account.positions[symbol] = pos
        logger.info(f"[BUY]  {symbol} @ ${exec_price:,.2f}  ${size_usd:.2f}  conf={signal.confidence:.0f}%" if signal else f"[BUY]  {symbol} @ ${exec_price:,.2f}")
        return pos

    def execute_sell(self, symbol: str, price: float, timestamp: datetime,
                     reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos        = self.account.positions[symbol]
        exec_price = price * (1 - self.slippage_pct)
        exit_fee   = exec_price * pos.size * self.fee_pct
        total_fees = exit_fee + pos.entry_fee
        pnl        = (exec_price - pos.entry_price) * pos.size - total_fees
        cost_basis = pos.entry_price * pos.size + pos.entry_fee
        pnl_pct    = pnl / cost_basis * 100
        self.account.cash      += exec_price * pos.size - exit_fee
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='sell', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        logger.info(f"[SELL] {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)  {reason}")
        return trade

    def execute_short(self, symbol: str, price: float, timestamp: datetime,
                      size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        size       = size_usd / price
        exec_price = price * (1 - self.slippage_pct)
        fee        = exec_price * size * self.fee_pct
        margin     = exec_price * size + fee
        if margin > self.account.cash:
            size   = (self.account.cash * 0.98) / (exec_price * (1 + self.fee_pct))
            fee    = exec_price * size * self.fee_pct
            margin = exec_price * size + fee
        if size <= 0 or margin > self.account.cash:
            return None
        self.account.cash -= margin
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='short', entry_fee=fee, entry_signal=signal)
        self.account.positions[symbol] = pos
        logger.info(f"[SHORT] {symbol} @ ${exec_price:,.2f}  ${size_usd:.2f}  conf={signal.confidence:.0f}%" if signal else f"[SHORT] {symbol} @ ${exec_price:,.2f}")
        return pos

    def execute_cover(self, symbol: str, price: float, timestamp: datetime,
                      reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'short':
            return self.execute_sell(symbol, price, timestamp, reason)
        exec_price  = price * (1 + self.slippage_pct)
        exit_fee    = exec_price * pos.size * self.fee_pct
        total_fees  = exit_fee + pos.entry_fee
        pnl         = (pos.entry_price - exec_price) * pos.size - total_fees
        cost_basis  = pos.entry_price * pos.size + pos.entry_fee
        pnl_pct     = pnl / cost_basis * 100
        returned    = pos.entry_price * pos.size + pos.entry_fee
        self.account.cash      += returned + pnl
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='cover', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        logger.info(f"[COVER] {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)  {reason}")
        return trade

    def update_unrealized_pnl(self, prices: Dict[str, float]):
        for sym, pos in self.account.positions.items():
            if sym in prices:
                p = prices[sym]
                pos.unrealized_pnl = (p - pos.entry_price) * pos.size if pos.side == 'buy' else (pos.entry_price - p) * pos.size

    def get_account_summary(self) -> Dict:
        pos_val  = sum(p.entry_price * p.size + p.unrealized_pnl for p in self.account.positions.values())
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
                                     vol_monitor: Optional[CryptoVolMonitor] = None):

    trader.running    = True
    trader._started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"Starting paper trading session for {symbols}")

    _load_adaptations()

    # ── Subsystems ─────────────────────────────────────────────────────────────
    strategy        = MicrostructureStrategy()
    regime_detector = RegimeDetector()
    ofi_calc        = OrderFlowImbalance(exchange, symbols)
    lead_lag        = LeadLagDetector(lead_symbol='BTC/USD')
    journal         = TradeJournal()
    learner         = Learner(journal)
    portfolio_opt   = PortfolioOptimizer()
    symbol_returns: Dict[str, List[float]] = {s: [] for s in symbols}
    regime_cache:   Dict[str, dict] = {}

    # ML + multi-timeframe + mean reversion
    ml_scorer   = MLScorer(journal)
    htf_filter  = MultiTimeframeFilter(exchange)
    mr_strategy = MeanReversionStrategy()
    strategy.ml_scorer = ml_scorer
    # Attempt initial load/train if journal already has data
    if ml_scorer.should_retrain():
        ml_scorer.train()

    max_daily_loss      = float(os.getenv('MAX_DAILY_LOSS', 10))
    session_start_equity = trader.initial_capital

    iteration = 0
    prices:   Dict[str, float] = {}
    indicators:       Dict[str, dict]   = {}
    recent_trades:    List[dict]        = []
    equity_curve:     List[dict]        = []

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
            s = trader.get_account_summary()
            stats = journal.stats()
            if notifier:
                notifier.send_status(
                    capital=s['total_equity'], pnl=s['total_pnl'],
                    pnl_pct=s['pnl_pct'], open_positions=s['open_positions'],
                    trades_today=stats.get('total', 0),
                )
            await asyncio.sleep(3600)

    asyncio.create_task(_hourly_digest())

    # ── Daily P&L summary (fires at midnight UTC) ──────────────────────────────
    day_start_equity = trader.initial_capital

    async def _daily_summary():
        nonlocal day_start_equity
        while trader.running:
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

    asyncio.create_task(_daily_summary())

    # ── SL/TP watcher ──────────────────────────────────────────────────────────
    async def _sltp_watcher():
        while trader.running:
            await asyncio.sleep(1)
            if not trader.account.positions:
                continue
            ws_prices = public_ws.get_prices() if public_ws else {}
            for sym in list(trader.account.positions.keys()):
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

                exit_reason = None
                if pnl_pct / 100 <= -sl_pct:
                    exit_reason = 'STOP_LOSS'
                elif pnl_pct / 100 >= tp_pct:
                    exit_reason = 'TAKE_PROFIT'

                if exit_reason:
                    trade = close_fn(sym, price, now, reason=exit_reason)
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, sym, exit_reason))
                        summary = trader.get_account_summary()
                        equity_curve.append({'t': now.strftime('%Y-%m-%d %H:%M'), 'v': round(summary['total_equity'], 2)})
                        _on_trade_closed(sym, pos, trade, exit_reason, summary['total_equity'],
                                         notifier, journal, ml_scorer)

    asyncio.create_task(_sltp_watcher())

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

    asyncio.create_task(_ofi_prefetcher())

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

    asyncio.create_task(_htf_fetcher())

    # ── OHLCV cache — one DataFrame per symbol, refreshed on candle close ──────
    ohlcv_cache:       Dict[str, pd.DataFrame] = {}
    last_eval_time:    Dict[str, float]        = {}   # throttle per symbol
    EVAL_INTERVAL = 2.0   # seconds between strategy evaluations per symbol

    async def _seed_cache():
        """Populate cache before the tick loop starts."""
        for sym in symbols:
            try:
                ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                if ohlcv:
                    ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                    logger.info(f"[CACHE] {sym} seeded with {len(ohlcv_cache[sym])} bars")
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
                                    if len(symbol_returns[sym]) > 500:
                                        symbol_returns[sym].pop(0)
                    except Exception as e:
                        logger.debug(f"[CACHE] refresh failed for {sym}: {e}")
            except asyncio.TimeoutError:
                # Fallback: refresh all
                for sym in symbols:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                    except Exception as e:
                        logger.warning(f"[CACHE] fallback refresh failed for {sym}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] refresher error: {e}")

    asyncio.create_task(_candle_refresher())

    # ── Main tick loop — evaluates every EVAL_INTERVAL seconds ─────────────────
    while trader.running:
        try:
            await asyncio.sleep(EVAL_INTERVAL)
            iteration += 1

            # Daily loss circuit breaker
            current_equity = trader.get_account_summary()['total_equity']
            daily_loss = session_start_equity - current_equity
            if daily_loss >= max_daily_loss:
                logger.warning(f"[RISK] Daily loss limit ${daily_loss:.2f} hit")
                if notifier:
                    notifier.send_message(
                        f"🛑 <b>Daily loss limit hit</b>\n"
                        f"Lost ${daily_loss:.2f} today — bot stopped for the day"
                    )
                trader.running = False
                break

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
                current_price = ws_prices.get(symbol) or prices.get(symbol)
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
                # Fetch order book and feed it into OFI v2 + kill filters
                try:
                    ob = await ofi_calc._exchange.exchange.fetch_order_book(symbol, limit=20)
                    if ob:
                        bids_raw = ob.get('bids', [])
                        asks_raw = ob.get('asks', [])
                        strategy.update_book(symbol, bids_raw, asks_raw, time.time())
                        # Update volume SMA for whale filter
                        vol_sma20 = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else 0.0
                        strategy.update_volume_sma(symbol, vol_sma20)
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
                            # T1 partial: close 50% of position
                            partial_size = pos_check.size * 0.5
                            if pos_check.side == 'buy':
                                exec_price = current_price * (1 - trader.slippage_pct)
                                pnl_partial = (exec_price - pos_check.entry_price) * partial_size
                                pos_check.size -= partial_size
                                trader.account.cash += exec_price * partial_size - exec_price * partial_size * trader.fee_pct
                                trader.account.total_pnl += pnl_partial
                            else:
                                exec_price = current_price * (1 + trader.slippage_pct)
                                pnl_partial = (pos_check.entry_price - exec_price) * partial_size
                                pos_check.size -= partial_size
                                trader.account.cash += pos_check.entry_price * partial_size + pnl_partial
                                trader.account.total_pnl += pnl_partial
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
                                                 summary['total_equity'], notifier, journal, ml_scorer)
                        continue   # skip entry logic this tick after an exit

                # ── Strategy evaluation ────────────────────────────────────────
                sig = strategy.evaluate(df, symbol, ofi_calc, lead_lag,
                                        regime_name, regime_conf, funding_rate)

                if sig is None:
                    continue

                # ── Multi-timeframe alignment adjustment ───────────────────────
                if sig.signal != Signal.HOLD:
                    mtf_adj = htf_filter.alignment_score(symbol, is_buy=sig.is_buy)
                    if mtf_adj != 0.0:
                        sig.confidence = max(0.0, min(100.0, sig.confidence + mtf_adj))
                        sig.size_mult  = _get_size_mult(sig.confidence)

                # ── Mean-reversion fallback for RANGING regime ─────────────────
                if sig.signal == Signal.HOLD and regime_name == 'RANGING':
                    mr_sig = mr_strategy.get_latest_signal(df)
                    if mr_sig and mr_sig.signal != Signal.HOLD:
                        mr_conf = 65.0
                        sig = ScientificSignal(
                            signal          = mr_sig.signal,
                            confidence      = mr_conf,
                            size_mult       = _get_size_mult(mr_conf),
                            ofi_score       = 0.0,
                            lead_lag_score  = 0.0,
                            regime_score    = 12.0,   # RANGING regime score
                            rsi_score       = 15.0 if (mr_sig.is_buy and mr_sig.rsi < 35) or (mr_sig.is_sell and mr_sig.rsi > 65) else 8.0,
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
                        logger.info(f"[MR] {symbol} RANGING fallback: {mr_sig.signal.value} conf={mr_conf:.0f}")

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

                # ── Entry: minimum confidence gate ─────────────────────────────
                min_conf = _adapt['min_confidence']

                # ── LONG ENTRY ──────────────────────────────────────────────────
                if sig.is_buy and pos is None:
                    if sig.confidence < min_conf:
                        logger.debug(f"[SKIP BUY] {symbol} conf={sig.confidence:.0f} < {min_conf:.0f}")
                        continue
                    if sentiment_monitor and not sentiment_monitor.allows_long(symbol):
                        logger.info(f"[SKIP BUY] {symbol} — sentiment blocked (F&G extreme fear)")
                        continue

                    # Position size from confidence + equity
                    size_usd = compute_position_size(sig.confidence, current_equity)
                    if size_usd < 1.50:
                        continue

                    if vol_monitor:
                        size_usd *= vol_monitor.get_size_multiplier(symbol)

                    position = trader.execute_buy(symbol, current_price, current_time,
                                                   size_usd=size_usd, signal=sig)
                    if position and notifier:
                        notifier.send_trade_alert(
                            action="BUY", symbol=symbol, price=current_price,
                            size=size_usd, signal=sig,
                        )

                # ── SHORT ENTRY ─────────────────────────────────────────────────
                elif sig.is_sell and pos is None:
                    if sig.confidence < min_conf:
                        logger.debug(f"[SKIP SELL] {symbol} conf={sig.confidence:.0f} < {min_conf:.0f}")
                        continue
                    if regime_name == 'TRENDING_UP':
                        logger.info(f"[SKIP SHORT] {symbol} — TRENDING_UP blocks shorts")
                        continue

                    size_usd = compute_position_size(sig.confidence, current_equity)
                    if size_usd < 1.50:
                        continue
                    if vol_monitor:
                        size_usd *= vol_monitor.get_size_multiplier(symbol)

                    position = trader.execute_short(symbol, current_price, current_time,
                                                     size_usd=size_usd, signal=sig)
                    if position and notifier:
                        notifier.send_trade_alert(
                            action="SELL", symbol=symbol, price=current_price,
                            size=size_usd, signal=sig,
                        )

                # ── EXIT LONG ───────────────────────────────────────────────────
                elif sig.signal == Signal.SELL and pos_side == 'buy':
                    # Exit when signal flips or OFI strongly reverses
                    trade = trader.execute_sell(symbol, current_price, current_time, reason="SIGNAL")
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))
                        summary = trader.get_account_summary()
                        equity_curve.append({'t': _ts(current_time), 'v': round(summary['total_equity'], 2)})
                        _on_trade_closed(symbol, pos, trade, "SIGNAL", summary['total_equity'],
                                         notifier, journal, ml_scorer)

                # ── EXIT SHORT ──────────────────────────────────────────────────
                elif pos_side == 'short' and (
                    sig.signal == Signal.BUY or regime_name == 'TRENDING_UP'
                ):
                    trade = trader.execute_cover(symbol, current_price, current_time, reason="SIGNAL")
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))
                        summary = trader.get_account_summary()
                        equity_curve.append({'t': _ts(current_time), 'v': round(summary['total_equity'], 2)})
                        _on_trade_closed(symbol, pos, trade, "SIGNAL", summary['total_equity'],
                                         notifier, journal, ml_scorer)

            # Refresh CVaR weights
            if iteration % 50 == 0 and any(len(v) >= 20 for v in symbol_returns.values()):
                portfolio_opt.optimize(symbol_returns)

            # Update prices from WS
            if public_ws:
                for sym, p in public_ws.get_prices().items():
                    if sym in symbols:
                        prices[sym] = p
                        lead_lag.update_price(sym, p)
            trader.update_unrealized_pnl(prices)

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
                'recent_trades': recent_trades[-50:],
                'equity_curve':  equity_curve[-200:],
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
        except Exception as e:
            logger.error(f"Error in paper trading loop: {e}", exc_info=True)
            await asyncio.sleep(5)

    trader.running = False
    logger.info("Paper trading session ended")
    learner.log_summary()


# ── Post-trade handler ─────────────────────────────────────────────────────────

def _on_trade_closed(symbol: str, pos: 'PaperPosition', trade: Trade,
                     exit_reason: str, total_equity: float,
                     notifier: Optional[TelegramNotifier], journal: TradeJournal,
                     ml_scorer: Optional['MLScorer'] = None):
    if pos is None:
        return

    sig = pos.entry_signal
    entry_dt    = pos.entry_time if isinstance(pos.entry_time, datetime) else datetime.now(timezone.utc)
    exit_dt     = trade.exit_time if isinstance(trade.exit_time, datetime) else datetime.now(timezone.utc)
    holding_min = (exit_dt - entry_dt).total_seconds() / 60

    issues, positives = _diagnose(pos.side, trade.pnl, exit_reason, holding_min, sig) if sig else ([], [])

    _record_to_journal(journal, trade, symbol, exit_reason, sig, sig.regime if sig else 'UNKNOWN')

    # Retrain ML model if enough new data has accumulated
    if ml_scorer is not None and ml_scorer.should_retrain():
        logger.info("[ML] Retraining triggered after new trades")
        ml_scorer.train()

    adaptations = _update_streaks_and_adapt(trade.pnl > 0, notifier)

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
        )
    elif notifier:
        fn = notifier.send_win if trade.pnl >= 0 else notifier.send_loss
        fn(symbol, trade.pnl, trade.pnl_pct, trade.exit_price, total_equity, reason=exit_reason)


# ── Utilities ──────────────────────────────────────────────────────────────────

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
