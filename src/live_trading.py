"""
Live Trading Engine — places real orders on Kraken.

Uses the same ScientificStrategy pipeline as paper_trading (OFI + BTC lead-lag +
regime + MTF + ML scorer).  Longs only on first deployment — shorts can be enabled
via ENABLE_SHORTS=true in .env once the strategy has a proven live track record.

Safety guarantees:
  - Startup reconciliation: syncs bot state with actual Kraken open positions
  - Order fill verification: position only recorded after confirmed fill
  - Fee tracking: actual fees pulled from order response
  - Daily loss circuit breaker: stops trading if loss exceeds MAX_DAILY_LOSS
  - Min confidence: 70 (higher bar than paper's 60)
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

import pandas as pd

from .indicators import Signal, prepare_ohlcv_dataframe
from .scientific_strategy import ScientificStrategy, ScientificSignal, compute_position_size, _size_multiplier as _get_size_mult
from .pairs_strategy import PairsStrategy, PairsSignal
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .exchange import ExchangeConnection
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS, KrakenPrivateWS
from .regime_detector import RegimeDetector
from .order_flow import OrderFlowImbalance
from .orderflow_ws import OrderFlowWS
from .ml_scorer import MLScorer
from .state import write_state, read_state

logger = logging.getLogger(__name__)

LIVE_MIN_CONFIDENCE = 70.0   # higher bar than paper (60) — real money
EVAL_INTERVAL       = 2.0    # seconds between signal evaluations per symbol
FEE_RATE            = 0.0026  # Kraken taker fee (0.26%) — overridden by actual order fee


@dataclass
class LivePosition:
    symbol:           str
    entry_time:       datetime
    entry_price:      float
    size:             float        # base currency (e.g. BTC amount)
    size_usd:         float        # USD value at entry
    order_id:         str
    stop_loss_price:  float
    take_profit_price: float
    side:             str = 'long'   # 'long' or 'short'
    entry_signal:     Optional[ScientificSignal] = None
    unrealized_pnl:   float = 0.0
    # ATR trailing stop state (chandelier exit)
    atr_at_entry:              float = 0.0
    highest_price_since_entry: float = 0.0   # for long chandelier
    lowest_price_since_entry:  float = 0.0   # for short chandelier


@dataclass
class LiveAccount:
    initial_capital:  float
    closed_trades:    List[Trade]  = field(default_factory=list)
    total_pnl:        float = 0.0
    total_fees:       float = 0.0


class LiveTrader:
    def __init__(self,
                 exchange:         ExchangeConnection,
                 symbols:          List[str],
                 notifier:         Optional[TelegramNotifier]   = None,
                 sentiment_monitor: Optional[SentimentMonitor]  = None,
                 public_ws:        Optional[KrakenPublicWS]     = None,
                 private_ws:       Optional[KrakenPrivateWS]    = None,
                 initial_capital:  float = 0.0):
        self.exchange          = exchange
        self.symbols           = symbols
        self.notifier          = notifier
        self.sentiment_monitor = sentiment_monitor
        self.public_ws         = public_ws
        self.private_ws        = private_ws
        self.running           = False

        # Trading subsystems (same as paper_trading)
        self.strategy        = ScientificStrategy()
        self.regime_detector = RegimeDetector()
        self.ofi_calc        = OrderFlowImbalance(exchange, symbols)  # REST fallback (dashboard only)
        self.ofw             = OrderFlowWS(symbols)                   # WS order flow (primary)
        self.journal         = TradeJournal()
        self.learner         = Learner(self.journal)
        self.ml_scorer       = MLScorer(self.journal)
        self.strategy.ml_scorer = self.ml_scorer
        if self.ml_scorer.should_retrain():
            self.ml_scorer.train()

        self.pairs_strategy = PairsStrategy(symbols)
        self._pairs_symbol: Optional[str] = None   # lagger symbol with an open pairs trade

        self.account   = LiveAccount(initial_capital=initial_capital)
        self.positions: Dict[str, LivePosition] = {}
        self._started_at = datetime.now(timezone.utc).isoformat()

    # ── Exchange helpers ───────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Fetch available USD balance from Kraken."""
        try:
            balance = await self.exchange.get_balance()
            return float(balance.get('USD', {}).get('free', 0))
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return 0.0

    async def reconcile_positions(self):
        """
        On startup, sync bot position state with actual Kraken open positions.
        Prevents ghost positions after a crash/restart.
        """
        try:
            exchange_positions = await self.exchange.exchange.fetch_positions(self.symbols)
            open_syms = set()
            for ep in (exchange_positions or []):
                sym    = ep.get('symbol', '')
                size   = float(ep.get('contracts', 0) or ep.get('size', 0) or 0)
                if size > 0 and sym in self.symbols:
                    open_syms.add(sym)
                    if sym not in self.positions:
                        # Exchange has a position we don't know about — record it
                        price = float(ep.get('entryPrice') or ep.get('markPrice') or 0)
                        if price > 0:
                            self.positions[sym] = LivePosition(
                                symbol=sym,
                                entry_time=datetime.now(timezone.utc),
                                entry_price=price,
                                size=size,
                                size_usd=size * price,
                                order_id='reconciled',
                                stop_loss_price=price * 0.98,   # 2% default SL
                                take_profit_price=price * 1.03,
                            )
                            logger.warning(f"[RECONCILE] Found untracked position: {sym} {size:.6f} @ ${price:.2f} — added with default SL/TP")

            # Remove positions bot thinks are open but exchange doesn't
            for sym in list(self.positions.keys()):
                if sym not in open_syms:
                    logger.warning(f"[RECONCILE] {sym} was in bot state but not on exchange — removing")
                    del self.positions[sym]

            if self.positions:
                logger.info(f"[RECONCILE] Active positions after sync: {list(self.positions.keys())}")
            else:
                logger.info("[RECONCILE] No open positions on exchange — clean start")
        except Exception as e:
            logger.warning(f"[RECONCILE] Could not reconcile positions: {e} — proceeding with empty state")

    # ── Order execution ────────────────────────────────────────────────────────

    async def open_long(self, symbol: str, price: float, size_usd: float,
                        signal: ScientificSignal) -> Optional[LivePosition]:
        """Place a real buy order. Only records the position after confirmed fill."""
        usd_balance = await self.get_balance()
        safe_usd    = min(size_usd, usd_balance * 0.95)

        if safe_usd < 5.0:
            logger.warning(f"[LIVE] Insufficient balance for {symbol}: ${usd_balance:.2f} available")
            return None

        size = safe_usd / price

        try:
            order = await self.exchange.create_order(
                symbol=symbol, order_type='market', side='buy', amount=size
            )
        except Exception as e:
            logger.error(f"[LIVE] Buy order FAILED for {symbol}: {e}")
            if self.notifier:
                self.notifier.send_error(f"Buy order failed for {symbol}: {e}")
            return None

        # Verify fill — don't record position if order didn't confirm
        status     = order.get('status', '')
        exec_price = float(order.get('average') or order.get('price') or price)
        order_id   = order.get('id', '')

        if status not in ('closed', 'filled', ''):
            logger.error(f"[LIVE] Order {order_id} status={status} — not confirmed filled, aborting position record")
            if self.notifier:
                self.notifier.send_error(f"{symbol} order {order_id} status '{status}' — check Kraken manually")
            return None

        sl_pct = signal.stop_loss_pct() / 100
        tp_pct = signal.take_profit_pct() / 100
        sl_price = exec_price * (1 - sl_pct)
        tp_price = exec_price * (1 + tp_pct)

        # Actual fee from order, fallback to known rate
        actual_fee = float(order.get('fee', {}).get('cost', 0) or safe_usd * FEE_RATE)
        self.account.total_fees += actual_fee

        atr = signal.atr if signal else 0.0
        pos = LivePosition(
            symbol=symbol,
            entry_time=datetime.now(timezone.utc),
            entry_price=exec_price,
            size=size,
            size_usd=safe_usd,
            order_id=order_id,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            entry_signal=signal,
            atr_at_entry=atr,
            highest_price_since_entry=exec_price,
            lowest_price_since_entry=exec_price,
        )
        self.positions[symbol] = pos
        logger.info(
            f"[LIVE BUY] {symbol} @ ${exec_price:.2f}  "
            f"size ${safe_usd:.2f}  SL ${sl_price:.2f}  TP ${tp_price:.2f}  "
            f"ATR={atr:.2f}  fee ${actual_fee:.3f}  order={order_id}"
        )
        return pos

    async def close_long(self, symbol: str, current_price: float,
                         reason: str) -> Optional[Trade]:
        """Place a real sell order to close a long position."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        try:
            order = await self.exchange.create_order(
                symbol=symbol, order_type='market', side='sell', amount=pos.size
            )
        except Exception as e:
            logger.error(f"[LIVE] Sell order FAILED for {symbol}: {e}")
            if self.notifier:
                self.notifier.send_error(f"SELL FAILED {symbol} @ ${current_price:.2f} — {e} — close manually on Kraken!")
            return None

        exec_price = float(order.get('average') or order.get('price') or current_price)
        exit_fee   = float(order.get('fee', {}).get('cost', 0) or pos.size_usd * FEE_RATE)
        self.account.total_fees += exit_fee

        pnl     = (exec_price - pos.entry_price) * pos.size - pos.size_usd * FEE_RATE - exit_fee
        pnl_pct = (exec_price - pos.entry_price) / pos.entry_price * 100

        trade = Trade(
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            entry_price=pos.entry_price,
            exit_price=exec_price,
            size=pos.size,
            side='sell',
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=exit_fee,
        )
        self.account.closed_trades.append(trade)
        self.account.total_pnl += pnl
        del self.positions[symbol]

        logger.info(
            f"[LIVE SELL] {symbol} @ ${exec_price:.2f}  "
            f"PnL ${pnl:.2f} ({pnl_pct:.2f}%)  reason={reason}"
        )

        # Journal + ML retrain check
        sig = pos.entry_signal
        _record_live_trade(self.journal, trade, symbol, reason, sig)
        if self.ml_scorer.should_retrain():
            logger.info("[ML] Retraining triggered")
            self.ml_scorer.train()

        # Notification
        total_equity = self.account.initial_capital + self.account.total_pnl
        if self.notifier and sig:
            issues, positives = _quick_diagnose(trade.pnl, reason, sig)
            self.notifier.send_trade_analysis(
                symbol=symbol, side='buy', pnl=pnl, pnl_pct=pnl_pct,
                entry_price=pos.entry_price, exit_price=exec_price,
                total_equity=total_equity, exit_reason=reason,
                holding_minutes=(trade.exit_time - trade.entry_time).total_seconds() / 60,
                regime=sig.regime, regime_conf=0.0,
                rsi=sig.rsi, adx=sig.adx, volume_ratio=sig.volume_ratio,
                ofi=sig.ofi, funding_apy=sig.funding_rate * 3 * 365 * 100 if sig.funding_rate else None,
                btc_lead=sig.lead_lag_dir,
                issues=issues, positives=positives,
                loss_streak=0, win_streak=0,
            )
        elif self.notifier:
            fn = self.notifier.send_win if pnl >= 0 else self.notifier.send_loss
            fn(symbol, pnl, pnl_pct, exec_price, total_equity, reason=reason)

        return trade

    async def open_short(self, symbol: str, price: float, size_usd: float,
                         signal: ScientificSignal) -> Optional[LivePosition]:
        """Place a real short-sell order.  Records position only after confirmed fill."""
        usd_balance = await self.get_balance()
        safe_usd    = min(size_usd, usd_balance * 0.95)

        if safe_usd < 5.0:
            logger.warning(f"[LIVE] Insufficient balance for short {symbol}: ${usd_balance:.2f} available")
            return None

        size = safe_usd / price

        try:
            order = await self.exchange.create_order(
                symbol=symbol, order_type='market', side='sell', amount=size,
                params={'reduceOnly': False}     # open a new short, not close a long
            )
        except Exception as e:
            logger.error(f"[LIVE] Short-sell order FAILED for {symbol}: {e}")
            if self.notifier:
                self.notifier.send_error(f"Short order failed for {symbol}: {e}")
            return None

        status     = order.get('status', '')
        exec_price = float(order.get('average') or order.get('price') or price)
        order_id   = order.get('id', '')

        if status not in ('closed', 'filled', ''):
            logger.error(f"[LIVE] Short order {order_id} status={status} — not confirmed filled")
            if self.notifier:
                self.notifier.send_error(f"{symbol} short order {order_id} status '{status}' — check Kraken manually")
            return None

        sl_pct = signal.stop_loss_pct() / 100
        tp_pct = signal.take_profit_pct() / 100
        # For shorts: SL is above entry, TP is below entry
        sl_price = exec_price * (1 + sl_pct)
        tp_price = exec_price * (1 - tp_pct)

        actual_fee = float(order.get('fee', {}).get('cost', 0) or safe_usd * FEE_RATE)
        self.account.total_fees += actual_fee

        atr = signal.atr if signal else 0.0
        pos = LivePosition(
            symbol=symbol,
            entry_time=datetime.now(timezone.utc),
            entry_price=exec_price,
            size=size,
            size_usd=safe_usd,
            order_id=order_id,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            side='short',
            entry_signal=signal,
            atr_at_entry=atr,
            highest_price_since_entry=exec_price,
            lowest_price_since_entry=exec_price,
        )
        self.positions[symbol] = pos
        logger.info(
            f"[LIVE SHORT] {symbol} @ ${exec_price:.2f}  "
            f"size ${safe_usd:.2f}  SL ${sl_price:.2f}  TP ${tp_price:.2f}  "
            f"ATR={atr:.2f}  fee ${actual_fee:.3f}  order={order_id}"
        )
        return pos

    async def close_short(self, symbol: str, current_price: float,
                          reason: str) -> Optional[Trade]:
        """Buy back to close a short position."""
        pos = self.positions.get(symbol)
        if not pos or pos.side != 'short':
            return None

        try:
            order = await self.exchange.create_order(
                symbol=symbol, order_type='market', side='buy', amount=pos.size,
                params={'reduceOnly': True}
            )
        except Exception as e:
            logger.error(f"[LIVE] Cover-short order FAILED for {symbol}: {e}")
            if self.notifier:
                self.notifier.send_error(f"COVER FAILED {symbol} @ ${current_price:.2f} — {e} — close manually on Kraken!")
            return None

        exec_price = float(order.get('average') or order.get('price') or current_price)
        exit_fee   = float(order.get('fee', {}).get('cost', 0) or pos.size_usd * FEE_RATE)
        self.account.total_fees += exit_fee

        # Short P&L: profit when price falls
        pnl     = (pos.entry_price - exec_price) * pos.size - pos.size_usd * FEE_RATE - exit_fee
        pnl_pct = (pos.entry_price - exec_price) / pos.entry_price * 100

        trade = Trade(
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            entry_price=pos.entry_price,
            exit_price=exec_price,
            size=pos.size,
            side='short',
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=exit_fee,
        )
        self.account.closed_trades.append(trade)
        self.account.total_pnl += pnl
        del self.positions[symbol]

        logger.info(
            f"[LIVE COVER] {symbol} @ ${exec_price:.2f}  "
            f"PnL ${pnl:.2f} ({pnl_pct:.2f}%)  reason={reason}"
        )

        sig = pos.entry_signal
        _record_live_trade(self.journal, trade, symbol, reason, sig, direction='short')
        if self.ml_scorer.should_retrain():
            logger.info("[ML] Retraining triggered")
            self.ml_scorer.train()

        total_equity = self.account.initial_capital + self.account.total_pnl
        if self.notifier:
            fn = self.notifier.send_win if pnl >= 0 else self.notifier.send_loss
            fn(symbol, pnl, pnl_pct, exec_price, total_equity, reason=f"SHORT/{reason}")

        return trade

    def update_unrealized(self, prices: Dict[str, float]):
        for sym, pos in self.positions.items():
            if sym in prices:
                if pos.side == 'short':
                    pos.unrealized_pnl = (pos.entry_price - prices[sym]) * pos.size
                else:
                    pos.unrealized_pnl = (prices[sym] - pos.entry_price) * pos.size

    def get_summary(self) -> dict:
        total_unr = sum(p.unrealized_pnl for p in self.positions.values())
        closed    = self.account.closed_trades
        return {
            'total_equity':   round(self.account.initial_capital + self.account.total_pnl + total_unr, 2),
            'total_pnl':      round(self.account.total_pnl, 4),
            'total_fees':     round(self.account.total_fees, 4),
            'pnl_pct':        round(self.account.total_pnl / max(1, self.account.initial_capital) * 100, 2),
            'open_positions': len(self.positions),
            'closed_trades':  len(closed),
            'winning_trades': sum(1 for t in closed if t.pnl > 0),
            'losing_trades':  sum(1 for t in closed if t.pnl <= 0),
        }


# ── Live session ───────────────────────────────────────────────────────────────

async def run_live_trading_session(exchange:          ExchangeConnection,
                                    trader:            LiveTrader,
                                    symbols:           List[str],
                                    timeframe:         str = '1m',
                                    lookback:          int = 250,
                                    notifier:          Optional[TelegramNotifier] = None,
                                    sentiment_monitor: Optional[SentimentMonitor] = None,
                                    public_ws:         Optional[KrakenPublicWS]   = None):

    trader.running = True
    logger.info(f"[LIVE] Session started — {symbols}  min_confidence={LIVE_MIN_CONFIDENCE}")

    # ── Startup: fetch real balance and reconcile open positions ───────────────
    real_balance = await trader.get_balance()
    trader.account.initial_capital = real_balance
    logger.info(f"[LIVE] Real USD balance: ${real_balance:.2f}")

    await trader.reconcile_positions()

    max_daily_loss    = float(os.getenv('MAX_DAILY_LOSS', 15))
    enable_shorts     = os.getenv('ENABLE_SHORTS', 'false').lower() == 'true'
    session_start_pnl = trader.account.total_pnl

    logger.info(f"[LIVE] Shorting {'ENABLED' if enable_shorts else 'DISABLED'} "
                f"(set ENABLE_SHORTS=true in .env to enable)")

    if notifier:
        notifier.send_message(
            f"<b>Bot started — LIVE</b>\n"
            f"Balance: <b>${real_balance:.2f}</b>\n"
            f"Trading: {', '.join(s.split('/')[0] for s in symbols)}"
        )

    # ── Background: WebSocket order flow (live CVD + OBI) ────────────────────
    asyncio.create_task(trader.ofw.start())
    logger.info("[LIVE] WebSocket order flow started (CVD + OBI)")

    # ── Background: OFI prefetch (REST fallback — used only when WS unavailable)
    async def _ofi_fetcher():
        while trader.running:
            for sym in symbols:
                try:
                    await trader.ofi_calc.fetch(sym)
                except Exception:
                    pass
                await asyncio.sleep(2)
            await asyncio.sleep(20)

    asyncio.create_task(_ofi_fetcher())

    # ── Background: SL/TP watcher (checks every second) ───────────────────────
    async def _sltp_watcher():
        while trader.running:
            await asyncio.sleep(1)
            if not trader.positions:
                continue
            ws_prices = public_ws.get_prices() if public_ws else {}
            for sym in list(trader.positions.keys()):
                try:
                    price = ws_prices.get(sym)
                    if not price:
                        ticker = await exchange.get_ticker(sym)
                        price  = float(ticker.get('last', 0))
                    if not price:
                        continue
                    pos = trader.positions.get(sym)
                    if not pos:
                        continue

                    # Chandelier trailing stop — ratchet SL up (long) or down (short)
                    if pos.atr_at_entry > 0:
                        if pos.side != 'short':
                            if price > pos.highest_price_since_entry:
                                pos.highest_price_since_entry = price
                            trail = pos.highest_price_since_entry - 2.5 * pos.atr_at_entry
                            if trail > pos.stop_loss_price:
                                pos.stop_loss_price = trail
                        else:
                            if price < pos.lowest_price_since_entry:
                                pos.lowest_price_since_entry = price
                            trail = pos.lowest_price_since_entry + 2.5 * pos.atr_at_entry
                            if trail < pos.stop_loss_price:
                                pos.stop_loss_price = trail

                    exit_reason = None
                    if pos.side == 'short':
                        # Short: SL above entry, TP below entry
                        if price >= pos.stop_loss_price:
                            exit_reason = 'STOP_LOSS'
                        elif price <= pos.take_profit_price:
                            exit_reason = 'TAKE_PROFIT'
                    else:
                        if price <= pos.stop_loss_price:
                            exit_reason = 'STOP_LOSS'
                        elif price >= pos.take_profit_price:
                            exit_reason = 'TAKE_PROFIT'

                    if exit_reason:
                        if pos.side == 'short':
                            trade = await trader.close_short(sym, price, exit_reason)
                        else:
                            trade = await trader.close_long(sym, price, exit_reason)
                        if trade:
                            equity = trader.account.initial_capital + trader.account.total_pnl
                            equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                            recent_trades.append(_trade_dict(trade, sym, exit_reason))
                except Exception as e:
                    logger.debug(f"[LIVE SL/TP] {sym}: {e}")

    asyncio.create_task(_sltp_watcher())

    # ── OHLCV cache ────────────────────────────────────────────────────────────
    ohlcv_cache:    Dict[str, pd.DataFrame] = {}
    regime_cache:   Dict[str, dict]         = {}
    last_eval_time: Dict[str, float]        = {}
    prices:         Dict[str, float]        = {}
    indicators:     Dict[str, dict]         = {}
    recent_trades:  List[dict]              = []
    equity_curve:   List[dict]              = []
    iteration = 0

    # Seed OHLCV
    for sym in symbols:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
            if ohlcv:
                ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                logger.info(f"[LIVE] {sym} seeded with {len(ohlcv_cache[sym])} bars")
        except Exception as e:
            logger.warning(f"[LIVE] Seed failed for {sym}: {e}")

    # ── Candle refresher ───────────────────────────────────────────────────────
    async def _candle_refresher():
        while trader.running:
            try:
                if public_ws:
                    candle = await asyncio.wait_for(public_ws.candle_queue.get(), timeout=90)
                    refresh_syms = [candle.symbol] if candle.symbol in symbols else symbols
                else:
                    await asyncio.sleep(60)
                    refresh_syms = symbols

                for sym in refresh_syms:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                            result = trader.regime_detector.detect(ohlcv_cache[sym])
                            if result:
                                regime_cache[sym] = result.to_dict()
                    except Exception as e:
                        logger.debug(f"[LIVE] Candle refresh failed {sym}: {e}")
            except asyncio.TimeoutError:
                for sym in symbols:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                    except Exception:
                        pass

    asyncio.create_task(_candle_refresher())

    # ── Main tick loop ─────────────────────────────────────────────────────────
    while trader.running:
        try:
            await asyncio.sleep(EVAL_INTERVAL)
            iteration += 1

            # Daily loss circuit breaker
            session_loss = session_start_pnl - trader.account.total_pnl
            if session_loss >= max_daily_loss:
                logger.warning(f"[LIVE RISK] Daily loss limit ${max_daily_loss:.2f} hit — stopping")
                if notifier:
                    notifier.send_message(
                        f"⛔ <b>DAILY LOSS LIMIT HIT</b>\n"
                        f"Lost ${session_loss:.2f} today (limit: ${max_daily_loss:.2f})\n"
                        f"Bot stopped — restart manually when ready"
                    )
                trader.running = False
                break

            ws_prices = public_ws.get_prices() if public_ws else {}

            for symbol in symbols:
                now_ts = time.time()
                if now_ts - last_eval_time.get(symbol, 0) < EVAL_INTERVAL:
                    continue
                last_eval_time[symbol] = now_ts

                if symbol not in ohlcv_cache:
                    continue

                current_price = ws_prices.get(symbol) or prices.get(symbol)
                if not current_price:
                    continue

                prices[symbol] = current_price
                trader.pairs_strategy.update_price(symbol, current_price)

                df = _inject_live_price(ohlcv_cache[symbol], current_price)

                from .paper_trading import _get_funding_rate
                funding_rate = _get_funding_rate(symbol)

                sig = trader.strategy.evaluate(
                    df, symbol, funding_rate=funding_rate, ofw=trader.ofw
                )
                if sig is None:
                    continue

                indicators[symbol] = {
                    'signal':     sig.signal.value,
                    'confidence': round(sig.confidence, 1),
                    'rsi':        round(sig.rsi, 2),
                    'adx':        round(sig.adx, 2),
                    'regime':     sig.regime,
                    'ofi':        round(sig.ofi, 3) if sig.ofi is not None else None,
                }

                pos            = trader.positions.get(symbol)
                pos_side       = pos.side if pos else None
                current_equity = trader.account.initial_capital + trader.account.total_pnl

                # ── Log rationale before any entry ────────────────────────────
                if sig.signal != Signal.HOLD and sig.confidence >= LIVE_MIN_CONFIDENCE and pos is None:
                    _log_trade_rationale(symbol, sig)

                # ── LONG ENTRY ─────────────────────────────────────────────────
                if sig.is_buy and pos is None and sig.confidence >= LIVE_MIN_CONFIDENCE:
                    if sentiment_monitor and not sentiment_monitor.allows_long(symbol):
                        continue

                    _base_btc = float(os.getenv('POSITION_SIZE_BTC', '0.001'))
                    _sz_btc   = _base_btc if sig.score >= 3 else _base_btc * 0.6
                    size_usd  = _sz_btc * prices.get('BTC/USD', current_price)
                    if size_usd < 5.0:
                        continue

                    position = await trader.open_long(symbol, current_price, size_usd, sig)
                    if position and notifier:
                        notifier.send_trade_alert(
                            action="BUY", symbol=symbol, price=current_price,
                            size=size_usd, signal=sig,
                        )

                # ── FLIP: long→short on reversal ───────────────────────────────
                elif sig.is_sell and pos is not None and pos_side == 'long':
                    # Close the long first
                    trade = await trader.close_long(symbol, current_price, "SIGNAL")
                    if trade:
                        equity = trader.account.initial_capital + trader.account.total_pnl
                        equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                        recent_trades.append(_trade_dict(trade, symbol, "SIGNAL"))
                    # Then open a short if enabled
                    if enable_shorts and sig.confidence >= LIVE_MIN_CONFIDENCE:
                        _base_btc = float(os.getenv('POSITION_SIZE_BTC', '0.001'))
                        _sz_btc   = _base_btc if sig.score >= 3 else _base_btc * 0.6
                        size_usd  = _sz_btc * prices.get('BTC/USD', current_price)
                        if size_usd >= 5.0:
                            position = await trader.open_short(symbol, current_price, size_usd, sig)
                            if position and notifier:
                                notifier.send_trade_alert(
                                    action="SHORT", symbol=symbol, price=current_price,
                                    size=size_usd, signal=sig,
                                )

                # ── SHORT ENTRY (no position, signal is sell) ──────────────────
                elif sig.is_sell and pos is None and enable_shorts and sig.confidence >= LIVE_MIN_CONFIDENCE:
                    if sentiment_monitor and not sentiment_monitor.allows_long(symbol):
                        pass  # sentiment blocks longs, shorts are fine
                    _base_btc = float(os.getenv('POSITION_SIZE_BTC', '0.001'))
                    _sz_btc   = _base_btc if sig.score >= 3 else _base_btc * 0.6
                    size_usd  = _sz_btc * prices.get('BTC/USD', current_price)
                    if size_usd >= 5.0:
                        position = await trader.open_short(symbol, current_price, size_usd, sig)
                        if position and notifier:
                            notifier.send_trade_alert(
                                action="SHORT", symbol=symbol, price=current_price,
                                size=size_usd, signal=sig,
                            )

                # ── SHORT EXIT (signal reversed to buy) ────────────────────────
                elif sig.is_buy and pos is not None and pos_side == 'short':
                    trade = await trader.close_short(symbol, current_price, "SIGNAL")
                    if trade:
                        equity = trader.account.initial_capital + trader.account.total_pnl
                        equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                        recent_trades.append(_trade_dict(trade, symbol, "SIGNAL"))
                    # Flip to long if confidence is high enough
                    if sig.confidence >= LIVE_MIN_CONFIDENCE:
                        _base_btc = float(os.getenv('POSITION_SIZE_BTC', '0.001'))
                        _sz_btc   = _base_btc if sig.score >= 3 else _base_btc * 0.6
                        size_usd  = _sz_btc * prices.get('BTC/USD', current_price)
                        if size_usd >= 5.0:
                            position = await trader.open_long(symbol, current_price, size_usd, sig)
                            if position and notifier:
                                notifier.send_trade_alert(
                                    action="BUY", symbol=symbol, price=current_price,
                                    size=size_usd, signal=sig,
                                )

            trader.update_unrealized(prices)

            # ── Pairs trading ─────────────────────────────────────────────────
            if trader._pairs_symbol is None:
                psig = trader.pairs_strategy.evaluate()
                if psig:
                    lagger       = psig.lagger
                    lagger_price = prices.get(lagger)
                    if lagger_price and lagger not in trader.positions:
                        _log_pairs_rationale(psig)
                        synth    = _pairs_to_signal(psig, lagger_price)
                        _base    = float(os.getenv('POSITION_SIZE_BTC', '0.001'))
                        size_usd = _base * 0.6 * prices.get('BTC/USD', lagger_price)
                        if size_usd >= 5.0:
                            if psig.direction == 'long':
                                pairs_pos = await trader.open_long(lagger, lagger_price, size_usd, synth)
                            else:
                                pairs_pos = await trader.open_short(lagger, lagger_price, size_usd, synth)
                            if pairs_pos:
                                trader._pairs_symbol = lagger
                                if notifier:
                                    notifier.send_message(
                                        f"<b>PAIRS TRADE</b> {psig.direction.upper()} "
                                        f"{lagger.split('/')[0]}\n"
                                        f"PAIRS: {psig.leader.split('/')[0]} led, "
                                        f"{lagger.split('/')[0]} lagged  z={psig.z_score:.1f}"
                                    )
            elif trader._pairs_symbol in trader.positions:
                psym   = trader._pairs_symbol
                ppos   = trader.positions[psym]
                pprice = prices.get(psym)
                if pprice and ppos.entry_signal:
                    pair_leader = ppos.entry_signal.rationale.get('pairs_leader')
                    if pair_leader and trader.pairs_strategy.should_exit(psym, pair_leader):
                        if ppos.side == 'long':
                            trade = await trader.close_long(psym, pprice, 'PAIRS_REVERSION')
                        else:
                            trade = await trader.close_short(psym, pprice, 'PAIRS_REVERSION')
                        if trade:
                            trader._pairs_symbol = None
                            equity = trader.account.initial_capital + trader.account.total_pnl
                            equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                            recent_trades.append(_trade_dict(trade, psym, 'PAIRS_REVERSION'))
            else:
                # Position was closed by SL/TP watcher — clear the pairs slot
                trader._pairs_symbol = None

            # State write for dashboard
            summary = trader.get_summary()
            write_state({
                'status':        'running',
                'mode':          'live',
                'started_at':    trader._started_at,
                'iteration':     iteration,
                'account':       summary,
                'positions': {
                    sym: {
                        'entry_price':    pos.entry_price,
                        'size':           pos.size,
                        'size_usd':       pos.size_usd,
                        'stop_loss':      pos.stop_loss_price,
                        'take_profit':    pos.take_profit_price,
                        'unrealized_pnl': round(pos.unrealized_pnl, 4),
                        'side':           pos.side,
                        'confidence':     pos.entry_signal.confidence if pos.entry_signal else 0,
                        'regime':         pos.entry_signal.regime if pos.entry_signal else 'UNKNOWN',
                    }
                    for sym, pos in trader.positions.items()
                },
                'prices':        {k: round(v, 2) for k, v in prices.items()},
                'indicators':    indicators,
                'recent_trades': recent_trades[-50:],
                'equity_curve':  equity_curve[-200:],
                'journal':       trader.journal.stats(),
                'ofi':           {s: round(trader.ofi_calc.get_smoothed(s), 3) if trader.ofi_calc.get_smoothed(s) else None for s in symbols},
            })

            if iteration % 30 == 0:
                s  = trader.get_summary()
                wr = round(s['winning_trades'] / s['closed_trades'] * 100, 1) if s['closed_trades'] else 0
                logger.info(
                    f"[LIVE] Tick {iteration}  equity=${s['total_equity']:,.2f}  "
                    f"pnl=${s['total_pnl']:+.2f}  trades={s['closed_trades']}(WR={wr}%)  "
                    f"fees=${s['total_fees']:.2f}"
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[LIVE] Loop error: {e}", exc_info=True)
            await asyncio.sleep(5)

    trader.running = False
    logger.info("[LIVE] Session ended")
    if notifier:
        s = trader.get_summary()
        notifier.send_message(
            f"<b>LIVE SESSION ENDED</b>\n"
            f"Final P&L: <b>${s['total_pnl']:+.2f}  ({s['pnl_pct']:+.1f}%)</b>\n"
            f"Trades: {s['closed_trades']}  (WR {round(s['winning_trades']/max(1,s['closed_trades'])*100)}%)\n"
            f"Fees paid: ${s['total_fees']:.2f}"
        )


# ── bot.py integration ─────────────────────────────────────────────────────────

async def start_live_session(exchange, symbols, timeframe, notifier, sentiment,
                              public_ws, private_ws, risk_cfg):
    """Called by bot.py._run_live_mode — thin wrapper."""
    trader = LiveTrader(
        exchange=exchange,
        symbols=symbols,
        notifier=notifier,
        sentiment_monitor=sentiment,
        public_ws=public_ws,
        private_ws=private_ws,
    )
    await run_live_trading_session(
        exchange=exchange,
        trader=trader,
        symbols=symbols,
        timeframe=timeframe,
        lookback=250,
        notifier=notifier,
        sentiment_monitor=sentiment,
        public_ws=public_ws,
    )
    return trader


# ── Helpers ────────────────────────────────────────────────────────────────────

def _inject_live_price(df: pd.DataFrame, live_price: float) -> pd.DataFrame:
    df = df.copy()
    idx = df.index[-1]
    df.at[idx, 'close'] = live_price
    df.at[idx, 'high']  = max(float(df.at[idx, 'high']), live_price)
    df.at[idx, 'low']   = min(float(df.at[idx, 'low']),  live_price)
    return df

def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

def _trade_dict(trade, symbol: str, reason: str) -> dict:
    return {
        'symbol':      symbol,
        'entry_time':  trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
        'exit_time':   trade.exit_time.isoformat()  if hasattr(trade.exit_time,  'isoformat') else str(trade.exit_time),
        'entry_price': round(trade.entry_price, 4),
        'exit_price':  round(trade.exit_price,  4),
        'pnl':         round(trade.pnl, 4),
        'pnl_pct':     round(trade.pnl_pct, 2),
        'reason':      reason,
    }

def _record_live_trade(journal: TradeJournal, trade: Trade, symbol: str,
                       reason: str, sig: Optional[ScientificSignal],
                       direction: str = 'buy'):
    now = datetime.now(timezone.utc)
    from .trade_journal import TradeRecord
    record = TradeRecord(
        trade_id    = f"{symbol}_{int(now.timestamp())}",
        symbol      = symbol,
        opened_at   = trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
        closed_at   = now.isoformat(),
        rsi          = sig.rsi          if sig else 50.0,
        adx          = sig.adx          if sig else 20.0,
        volume_ratio = sig.volume_ratio if sig else 1.0,
        regime       = sig.regime       if sig else 'UNKNOWN',
        atr_pct      = (sig.atr / sig.close * 100) if sig and sig.atr and sig.close else 1.0,
        ema100_gap   = 0.0,
        ema200_gap   = 0.0,
        hour_utc     = now.hour,
        day_of_week  = now.weekday(),
        pnl          = round(trade.pnl, 4),
        pnl_pct      = round(trade.pnl_pct, 2),
        won          = trade.pnl > 0,
        reason       = reason,
        ofi               = float(sig.ofi or 0.0) if sig else 0.0,
        lead_lag_strength = max(0.0, sig.lead_lag_score) / 20.0 if sig else 0.0,
        lead_lag_aligned  = (sig.lead_lag_dir == 'BUY') if sig and sig.lead_lag_dir else False,
        confidence        = float(sig.confidence) if sig else 0.0,
        ofi_score         = float(sig.ofi_score) if sig else 0.0,
        lead_lag_score    = float(sig.lead_lag_score) if sig else 0.0,
        regime_score      = float(sig.regime_score) if sig else 0.0,
        regime_confidence = 0.5,
        funding_rate      = float(sig.funding_rate or 0.0) if sig else 0.0,
        direction         = direction,
    )
    journal.add(record)

def _log_trade_rationale(symbol: str, sig: ScientificSignal):
    r = sig.rationale
    if not r:
        return
    ind = r.get('indicators', {})
    st  = ind.get('supertrend', {})
    cvd = ind.get('cvd', {})
    ofi = ind.get('ofi', {})
    ok  = lambda v: '+' if v else '-'
    lines = [
        f"",
        f"┌─ ENTRY ─ {symbol} {r.get('direction')} ─── score={r.get('score')}/3 ─────────────",
        f"│  Supertrend : {'BULL' if st.get('bull') else 'BEAR'}  ({ok(st.get('vote'))})",
        f"│  CVD        : {cvd.get('trend', '?')}  ({ok(cvd.get('vote'))})",
        f"│  OFI (OBI)  : {ofi.get('obi', 'n/a')}  ({ok(ofi.get('vote'))})",
        f"│  ATR        : {sig.atr:.2f}   SL={sig.stop_loss_pct():.2f}%  TP={sig.take_profit_pct():.2f}%",
        f"└────────────────────────────────────────────────────────────────────────",
    ]
    logger.info('\n'.join(lines))


def _pairs_to_signal(psig: PairsSignal, price: float) -> ScientificSignal:
    """Build a synthetic ScientificSignal so open_long/open_short can accept it."""
    return ScientificSignal(
        signal=Signal.BUY if psig.direction == 'long' else Signal.SELL,
        confidence=psig.confidence,
        size_mult=0.6,
        score=2,
        close=price,
        atr=0.0,   # triggers fallback SL (1.5%) and TP (3.0%)
        rationale={
            'direction':    psig.direction.upper(),
            'score':        2,
            'confidence':   psig.confidence,
            'pairs_leader': psig.leader,
            'pairs_lagger': psig.lagger,
            'pairs_zscore': psig.z_score,
        },
    )


def _log_pairs_rationale(psig: PairsSignal):
    leader_sym = psig.leader.split('/')[0]
    lagger_sym = psig.lagger.split('/')[0]
    logger.info(
        f"\n┌─ PAIRS ENTRY ─ {psig.direction.upper()} {lagger_sym} "
        f"─────────────────────────────────────────\n"
        f"│  PAIRS: {leader_sym} led +{abs(psig.z_score * 0.5):.1f}%, "
        f"{lagger_sym} lagged → {psig.direction} {lagger_sym}  z={psig.z_score:.2f}\n"
        f"│  Confidence: {psig.confidence:.0f}\n"
        f"└─────────────────────────────────────────────────────────────────────────"
    )


def _quick_diagnose(pnl: float, reason: str, sig: ScientificSignal):
    issues, positives = [], []
    if sig.ofi is not None:
        if sig.ofi > 0.15:
            positives.append(f"OFI {sig.ofi:+.2f} confirmed direction at entry")
        elif sig.ofi < -0.15:
            issues.append(f"OFI {sig.ofi:+.2f} was against direction — order flow warned us")
    if reason == 'STOP_LOSS':
        issues.append("Stopped out — immediate rejection at entry level")
    elif reason == 'TAKE_PROFIT':
        positives.append("Target reached as predicted")
    if sig.confidence >= 90:
        positives.append(f"High conviction entry ({sig.confidence:.0f}% confidence)")
    elif sig.confidence < 70:
        issues.append(f"Low confidence entry ({sig.confidence:.0f}%) — should have waited")
    return issues, positives
