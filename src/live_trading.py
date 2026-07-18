"""
Live Trading Engine — places real orders on Kraken.

Uses the same ScientificStrategy pipeline as paper_trading (OFI + BTC lead-lag +
regime + MTF + ML scorer).  Longs only on first deployment — shorts can be enabled
via ENABLE_SHORTS=true in .env once the strategy has a proven live track record.

Safety guarantees:
  - Startup reconciliation: syncs bot state with actual Kraken open positions
  - Order fill verification: position only recorded after confirmed fill
  - Fee tracking: actual fees pulled from order response
  - Daily loss circuit breaker: halts NEW entries if realized loss exceeds
    MAX_DAILY_LOSS, without abandoning SL/TP protection on any open position
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
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .exchange import ExchangeConnection, CircuitBreakerOpen
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS, KrakenPrivateWS
from .regime_detector import RegimeDetector
from .order_flow import OrderFlowImbalance
from .lead_lag_detector import LeadLagDetector
from .multi_timeframe import MultiTimeframeFilter
from .ml_scorer import MLScorer
from .state import write_state, read_state

logger = logging.getLogger(__name__)

LIVE_MIN_CONFIDENCE = 70.0   # higher bar than paper (60) — real money
EVAL_INTERVAL       = 2.0    # seconds between signal evaluations per symbol
FEE_RATE            = 0.0026  # Kraken taker fee (0.26%) — overridden by actual order fee
ATR_TRAIL_MULT      = 2.5    # chandelier exit: trail = highest_since_entry - ATR_TRAIL_MULT × entry ATR


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
    entry_signal:     Optional[ScientificSignal] = None
    unrealized_pnl:   float = 0.0
    entry_fee:        float = 0.0  # actual fee charged on the opening order
    # ATR trailing stop state (chandelier exit) — ratchets stop_loss_price up as
    # price makes new highs so a winner can't fully round-trip back to a loss.
    # atr_at_entry=0 (reconciled positions) disables the trail; static SL/TP only.
    atr_at_entry:              float = 0.0
    highest_price_since_entry: float = 0.0


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
        self.ofi_calc        = OrderFlowImbalance(exchange, symbols)
        self.lead_lag        = LeadLagDetector(lead_symbol='BTC/USD')
        self.htf_filter      = MultiTimeframeFilter(exchange)
        self.journal         = TradeJournal()
        self.learner         = Learner(self.journal)
        self.ml_scorer       = MLScorer(self.journal)
        self.strategy.ml_scorer = self.ml_scorer
        if self.ml_scorer.should_retrain():
            self.ml_scorer.train()

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

        Raises on failure (after the exchange wrapper's own retries are
        exhausted) instead of swallowing the error and assuming "no open
        positions" — that assumption could let the bot open a duplicate
        position on top of one already live on Kraken. The caller must not
        start trading if this raises. Also raises if an untracked exchange
        position has no usable entry/mark price and the ticker fallback also
        fails — recording it with a fabricated price is worse than aborting.
        """
        exchange_positions = await self.exchange.get_positions(self.symbols)
        open_syms = set()
        for ep in (exchange_positions or []):
            sym    = ep.get('symbol', '')
            size   = float(ep.get('contracts', 0) or ep.get('size', 0) or 0)
            if size > 0 and sym in self.symbols:
                open_syms.add(sym)
                if sym not in self.positions:
                    # Exchange has a position we don't know about — record it
                    price = float(ep.get('entryPrice') or ep.get('markPrice') or 0)
                    if price <= 0:
                        # entryPrice/markPrice missing on the position response —
                        # fall back to a live ticker price rather than silently
                        # dropping this position. Silently skipping here would
                        # leave it out of self.positions while it's still in
                        # open_syms, so the next signal loop sees pos is None
                        # and opens a SECOND position on top of this real one.
                        try:
                            ticker = await self.exchange.get_ticker(sym)
                            price = float(ticker.get('last') or ticker.get('close') or ticker.get('bid') or 0)
                        except Exception as e:
                            logger.error(f"[RECONCILE] Ticker fallback for {sym} failed: {e}")
                    if price <= 0:
                        raise RuntimeError(
                            f"Untracked open position on {sym} (size={size:.6f}) but no price "
                            f"available from entryPrice/markPrice or ticker fallback — refusing "
                            f"to silently drop it, which could open a duplicate position."
                        )
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
        except CircuitBreakerOpen:
            raise  # propagate to main loop so it can sleep the correct cooldown
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
            entry_fee=actual_fee,
            atr_at_entry=float(getattr(signal, 'atr', 0.0) or 0.0),
            highest_price_since_entry=exec_price,
        )
        self.positions[symbol] = pos
        logger.info(
            f"[LIVE BUY] {symbol} @ ${exec_price:.2f}  "
            f"size ${safe_usd:.2f}  SL ${sl_price:.2f}  TP ${tp_price:.2f}  "
            f"fee ${actual_fee:.3f}  order={order_id}"
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
        except CircuitBreakerOpen:
            raise  # propagate to caller; position stays open and will be retried
        except Exception as e:
            logger.error(f"[LIVE] Sell order FAILED for {symbol}: {e}")
            if self.notifier:
                self.notifier.send_error(f"SELL FAILED {symbol} @ ${current_price:.2f} — {e} — close manually on Kraken!")
            return None

        exec_price = float(order.get('average') or order.get('price') or current_price)
        exit_fee   = float(order.get('fee', {}).get('cost', 0) or pos.size_usd * FEE_RATE)
        self.account.total_fees += exit_fee

        # Use the actual entry fee charged at open (not a re-estimate) so realized
        # PnL matches the real account balance change.
        pnl     = (exec_price - pos.entry_price) * pos.size - pos.entry_fee - exit_fee
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
            fees=pos.entry_fee + exit_fee,
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

    def update_unrealized(self, prices: Dict[str, float]):
        for sym, pos in self.positions.items():
            if sym in prices:
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

    try:
        await trader.reconcile_positions()
    except Exception as e:
        logger.error(f"[LIVE] Position reconciliation failed — refusing to start trading blind: {e}")
        if notifier:
            try:
                notifier.send_message(
                    "🛑 <b>LIVE start aborted</b>\n"
                    "Could not verify open positions on Kraken — refusing to trade blind "
                    "(treating this as \"no positions\" could open a duplicate on top of "
                    "one already live).\n"
                    f"Error: {e}\nCheck Kraken manually, then restart."
                )
            except Exception:
                pass
        trader.running = False
        return

    max_daily_loss    = float(os.getenv('MAX_DAILY_LOSS', 15))
    enable_shorts     = os.getenv('ENABLE_SHORTS', 'false').lower() == 'true'
    session_start_pnl = trader.account.total_pnl

    if notifier:
        notifier.send_message(
            f"<b>Bot started — LIVE</b>\n"
            f"Balance: <b>${real_balance:.2f}</b>\n"
            f"Trading: {', '.join(s.split('/')[0] for s in symbols)}"
        )

    # ── Background: OFI prefetch ───────────────────────────────────────────────
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

    # ── Background: 5m HTF cache ───────────────────────────────────────────────
    async def _htf_fetcher():
        while trader.running:
            for sym in symbols:
                try:
                    await trader.htf_filter.fetch(sym)
                except Exception:
                    pass
                await asyncio.sleep(3)
            await asyncio.sleep(45)

    asyncio.create_task(_htf_fetcher())

    # ── Background: SL/TP watcher (checks every second) ───────────────────────
    sltp_circuit_blocked: Dict[str, bool] = {}  # per-symbol: exit currently blocked by circuit breaker

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

                    _update_chandelier_stop(pos, price)

                    exit_reason = None
                    if price <= pos.stop_loss_price:
                        exit_reason = 'STOP_LOSS'
                    elif price >= pos.take_profit_price:
                        exit_reason = 'TAKE_PROFIT'

                    if exit_reason:
                        trade = await trader.close_long(sym, price, exit_reason)
                        if trade:
                            equity = trader.account.initial_capital + trader.account.total_pnl
                            equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                            recent_trades.append(_trade_dict(trade, sym, exit_reason))

                    if sltp_circuit_blocked.get(sym):
                        sltp_circuit_blocked[sym] = _sltp_circuit_cleared(notifier, sym)
                except CircuitBreakerOpen as e:
                    # The exchange order/data circuit is open — a triggered
                    # stop-loss/take-profit could NOT be executed. Silently
                    # swallowing this (like a generic fetch failure) would mean
                    # a real-money position sits past its stop with no signal
                    # to the operator. Alert once on the transition, not every
                    # second, then keep retrying — close_long left the position
                    # open, so the next tick will try again automatically.
                    wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                    sltp_circuit_blocked[sym] = _sltp_circuit_alert(
                        notifier, sym, wait, sltp_circuit_blocked.get(sym, False)
                    )
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
    killed = False  # master kill switch state — see _kill_switch_engaged()
    daily_loss_halted = False  # daily-loss breaker state — see _daily_loss_halted()

    # Seed OHLCV
    for sym in symbols:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
            if ohlcv:
                ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                logger.info(f"[LIVE] {sym} seeded with {len(ohlcv_cache[sym])} bars")
        except CircuitBreakerOpen as e:
            wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
            logger.error(f"[LIVE] Exchange circuit breaker open during seed — stopping early ({wait:.0f}s remaining)")
            if notifier:
                try:
                    notifier.send_message(
                        f"⚠️ <b>Exchange circuit breaker open</b>\n"
                        f"Data seed interrupted — cooldown {wait:.0f}s.\n"
                        f"Bot will trade on stale/incomplete cache until exchange recovers."
                    )
                except Exception:
                    pass
            break  # no point seeding further while circuit is open
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
                    except CircuitBreakerOpen as e:
                        wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                        logger.warning(f"[LIVE] Exchange circuit breaker open — pausing refresher {wait:.0f}s (symbol={sym})")
                        await asyncio.sleep(wait)
                        break  # skip remaining symbols; refresher will retry next cycle
                    except Exception as e:
                        logger.debug(f"[LIVE] Candle refresh failed {sym}: {e}")
            except asyncio.TimeoutError:
                for sym in symbols:
                    try:
                        ohlcv = await exchange.fetch_ohlcv(sym, timeframe, limit=lookback)
                        if ohlcv:
                            ohlcv_cache[sym] = prepare_ohlcv_dataframe(ohlcv)
                    except CircuitBreakerOpen as e:
                        wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
                        logger.warning(f"[LIVE] Circuit open (fallback path) — sleeping {wait:.0f}s")
                        await asyncio.sleep(wait)
                        break  # skip remaining symbols; will resume next cycle
                    except Exception:
                        pass

    asyncio.create_task(_candle_refresher())

    # ── Main tick loop ─────────────────────────────────────────────────────────
    while trader.running:
        try:
            await asyncio.sleep(EVAL_INTERVAL)
            iteration += 1

            # Master kill switch — halts NEW entries (exits below still run).
            # Same flag the paper arms honor; file/env toggled, no restart needed.
            killed = _kill_switch_engaged(notifier, killed)

            # Daily loss circuit breaker — halts NEW entries only (same
            # halt-not-kill pattern as the master kill switch above). Must NOT
            # set trader.running=False here: that flag also gates the
            # background _sltp_watcher/_candle_refresher/_ofi_fetcher tasks,
            # so flipping it would abandon any still-open real-money position
            # with no further stop-loss/take-profit protection.
            session_loss = session_start_pnl - trader.account.total_pnl
            daily_loss_halted = _daily_loss_halted(
                notifier, session_loss, max_daily_loss, daily_loss_halted
            )

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
                trader.lead_lag.update_price(symbol, current_price)

                df = _inject_live_price(ohlcv_cache[symbol], current_price)

                cached_regime = regime_cache.get(symbol, {})
                regime_name   = cached_regime.get('regime', 'UNKNOWN')
                regime_conf   = cached_regime.get('confidence', 0.5)

                from .paper_trading import _get_funding_rate
                funding_rate = _get_funding_rate(symbol)

                sig = trader.strategy.evaluate(
                    df, symbol, trader.ofi_calc, trader.lead_lag,
                    regime_name, regime_conf, funding_rate
                )
                if sig is None:
                    continue

                # MTF alignment adjustment
                if sig.signal != Signal.HOLD:
                    mtf_adj = trader.htf_filter.alignment_score(symbol, is_buy=sig.is_buy)
                    if mtf_adj != 0.0:
                        sig.confidence = max(0.0, min(100.0, sig.confidence + mtf_adj))
                        sig.size_mult  = _get_size_mult(sig.confidence)

                indicators[symbol] = {
                    'signal':     sig.signal.value,
                    'confidence': round(sig.confidence, 1),
                    'rsi':        round(sig.rsi, 2),
                    'adx':        round(sig.adx, 2),
                    'regime':     sig.regime,
                    'ofi':        round(sig.ofi, 3) if sig.ofi is not None else None,
                }

                pos      = trader.positions.get(symbol)
                pos_side = pos.entry_signal.signal if pos and pos.entry_signal else None

                current_equity = trader.account.initial_capital + trader.account.total_pnl

                # ── LONG ENTRY ─────────────────────────────────────────────────
                if (sig.is_buy and pos is None and sig.confidence >= LIVE_MIN_CONFIDENCE
                        and not killed and not daily_loss_halted):
                    if sentiment_monitor and not sentiment_monitor.allows_long(symbol):
                        continue

                    size_usd = compute_position_size(sig.confidence, current_equity)
                    if size_usd < 5.0:
                        continue

                    position = await trader.open_long(symbol, current_price, size_usd, sig)
                    if position and notifier:
                        notifier.send_trade_alert(
                            action="BUY", symbol=symbol, price=current_price,
                            size=size_usd, signal=sig,
                        )

                # ── LONG EXIT (signal reversed) ────────────────────────────────
                elif sig.signal == Signal.SELL and pos is not None:
                    trade = await trader.close_long(symbol, current_price, "SIGNAL")
                    if trade:
                        equity = trader.account.initial_capital + trader.account.total_pnl
                        equity_curve.append({'t': _ts(), 'v': round(equity, 2)})
                        recent_trades.append(_trade_dict(trade, symbol, "SIGNAL"))

            trader.update_unrealized(prices)

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
        except CircuitBreakerOpen as e:
            wait = e.remaining_seconds if e.remaining_seconds > 0 else 60.0
            logger.warning(f"[LIVE] Exchange circuit breaker open — pausing main loop {wait:.0f}s")
            if notifier:
                try:
                    notifier.send_message(
                        f"⚠️ <b>Exchange circuit breaker open</b>\n"
                        f"Trading paused for {wait:.0f}s — exchange unavailable."
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait)
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

def _update_chandelier_stop(pos: LivePosition, current_price: float) -> None:
    """Ratchet pos.stop_loss_price up as price makes new highs (chandelier exit) —
    never loosens it. No-op when atr_at_entry is unset (e.g. reconciled positions
    with no signal context), in which case the static entry SL/TP still applies."""
    if pos.atr_at_entry <= 0:
        return
    if current_price > pos.highest_price_since_entry:
        pos.highest_price_since_entry = current_price
    trail = pos.highest_price_since_entry - ATR_TRAIL_MULT * pos.atr_at_entry
    if trail > pos.stop_loss_price:
        pos.stop_loss_price = trail


def _kill_switch_engaged(notifier: Optional[TelegramNotifier], was_killed: bool) -> bool:
    """Check the master kill switch (src/kill_switch.py) and notify on transitions.

    Mirrors the check paper_trading.py runs before every directional entry:
    BOT_KILL_SWITCH=1 or the data/KILL_SWITCH flag file halts NEW entries only —
    exits (SL/TP, signal-reversal close) keep running so real positions can
    always be closed. Returns the current killed state.
    """
    from .kill_switch import is_killed
    killed = is_killed()
    if killed and not was_killed:
        logger.warning("[LIVE] Master kill switch engaged — halting new entries (exits continue)")
        if notifier:
            try:
                notifier.send_message(
                    "🛑 <b>Master kill switch engaged</b>\n"
                    "LIVE trading: new entries halted, exits continue."
                )
            except Exception:
                pass
    elif not killed and was_killed:
        logger.info("[LIVE] Master kill switch released — resuming entries")
        if notifier:
            try:
                notifier.send_message(
                    "✅ <b>Master kill switch released</b>\n"
                    "LIVE trading: entries resumed."
                )
            except Exception:
                pass
    return killed


def _daily_loss_halted(notifier: Optional[TelegramNotifier], session_loss: float,
                       max_daily_loss: float, was_halted: bool) -> bool:
    """Daily loss circuit breaker — halts NEW entries only, once realized
    `session_loss` breaches `max_daily_loss`. Mirrors _kill_switch_engaged's
    halt-not-kill pattern: it never touches trader.running, so the SL/TP
    watcher keeps protecting any position that was still open when the
    breaker tripped instead of abandoning it. One-way latch for the rest of
    the session — a later winning exit pulling session_loss back under the
    cap does not re-arm entries; per the alert text, that needs a manual
    restart.
    """
    if was_halted:
        return True
    if session_loss < max_daily_loss:
        return False
    logger.warning(
        f"[LIVE RISK] Daily loss limit ${max_daily_loss:.2f} hit "
        f"(lost ${session_loss:.2f}) — halting new entries (exits continue)"
    )
    if notifier:
        try:
            notifier.send_message(
                f"⛔ <b>DAILY LOSS LIMIT HIT</b>\n"
                f"Lost ${session_loss:.2f} today (limit: ${max_daily_loss:.2f})\n"
                f"New entries halted — any open position remains protected by "
                f"its stop-loss/take-profit. Restart the bot manually to resume entries."
            )
        except Exception:
            pass
    return True


def _sltp_circuit_alert(notifier: Optional[TelegramNotifier], symbol: str,
                        wait: float, was_blocked: bool) -> bool:
    """Alert once when the exchange circuit breaker starts blocking a
    triggered stop-loss/take-profit exit for `symbol`. Mirrors
    _kill_switch_engaged's once-per-transition pattern so a stuck exit can't
    fail silently forever, without spamming an alert every second it stays
    open. Returns True (the new blocked state) for the caller to store.
    """
    if not was_blocked:
        logger.warning(
            f"[LIVE SL/TP] {symbol}: cannot close — exchange circuit breaker "
            f"open ({wait:.0f}s remaining)"
        )
        if notifier:
            try:
                notifier.send_message(
                    f"🛑 <b>Cannot close {symbol}</b>\n"
                    f"Stop-loss/take-profit triggered but the exchange circuit "
                    f"breaker is open ({wait:.0f}s) — the order could not be "
                    f"placed. Position remains live on Kraken; the bot will "
                    f"keep retrying automatically."
                )
            except Exception:
                pass
    return True


def _sltp_circuit_cleared(notifier: Optional[TelegramNotifier], symbol: str) -> bool:
    """Alert once when a previously-blocked exit for `symbol` is retried
    without hitting the circuit breaker again. Returns False (the new
    blocked state) for the caller to store.
    """
    logger.info(f"[LIVE SL/TP] {symbol}: exchange circuit breaker cleared — exit retries resumed")
    if notifier:
        try:
            notifier.send_message(
                f"✅ <b>{symbol} exit retries resumed</b>\n"
                f"Exchange circuit breaker cleared."
            )
        except Exception:
            pass
    return False


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
                       reason: str, sig: Optional[ScientificSignal]):
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
        direction         = 'buy',
    )
    journal.add(record)

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
