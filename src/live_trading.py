"""
Live Trading Engine
Places real orders on Kraken via ccxt
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from .indicators import Signal, prepare_ohlcv_dataframe
from .advanced_strategy import AdvancedStrategy
from .production_strategy import ProductionStrategy
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .exchange import ExchangeConnection
from .backtester import Trade
from .notifications import TelegramNotifier
from .state import write_state

logger = logging.getLogger(__name__)


@dataclass
class LivePosition:
    symbol: str
    entry_time: datetime
    entry_price: float
    size: float          # base currency amount
    order_id: str
    stop_loss_price: float
    take_profit_price: float
    unrealized_pnl: float = 0.0


@dataclass
class LiveAccount:
    initial_capital: float
    closed_trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0


class LiveTrader:
    def __init__(self,
                 exchange: ExchangeConnection,
                 position_size_usd: float = 50.0,
                 notifier: Optional[TelegramNotifier] = None):
        self.exchange = exchange
        self.position_size_usd = position_size_usd
        self.notifier = notifier
        self.strategy = ProductionStrategy()
        self.positions: Dict[str, LivePosition] = {}
        self.account = LiveAccount(initial_capital=position_size_usd)
        self.running = False
        self.journal = TradeJournal()
        self.learner = Learner(self.journal)
        self._entry_signals: Dict[str, object] = {}   # symbol → signal at entry

    async def get_balance(self) -> float:
        """Fetch available USD balance from Kraken."""
        try:
            balance = await self.exchange.get_balance()
            return float(balance.get('USD', {}).get('free', 0))
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return 0.0

    async def open_position(self, symbol: str, price: float,
                            stop_loss: float, take_profit: float) -> Optional[LivePosition]:
        usd_balance = await self.get_balance()
        trade_usd = min(self.position_size_usd, usd_balance * 0.95)

        if trade_usd < 5:
            logger.warning(f"Insufficient balance for {symbol}: ${usd_balance:.2f}")
            return None

        size = trade_usd / price

        try:
            order = await self.exchange.create_order(
                symbol=symbol,
                order_type='market',
                side='buy',
                amount=size
            )
            exec_price = float(order.get('average') or order.get('price') or price)
            pos = LivePosition(
                symbol=symbol,
                entry_time=datetime.now(timezone.utc),
                entry_price=exec_price,
                size=size,
                order_id=order.get('id', ''),
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            self.positions[symbol] = pos
            logger.info(f"[LIVE BUY] {symbol} @ ${exec_price:.2f} | Size: {size:.6f} | SL: ${stop_loss:.2f} | TP: ${take_profit:.2f}")
            return pos
        except Exception as e:
            logger.error(f"Buy order failed for {symbol}: {e}")
            return None

    def record_entry_signal(self, symbol: str, signal):
        """Store the signal conditions at entry time for later journal recording."""
        self._entry_signals[symbol] = signal

    async def close_position(self, symbol: str, current_price: float, reason: str) -> Optional[Trade]:
        pos = self.positions.get(symbol)
        if not pos:
            return None

        try:
            order = await self.exchange.create_order(
                symbol=symbol,
                order_type='market',
                side='sell',
                amount=pos.size
            )
            exec_price = float(order.get('average') or order.get('price') or current_price)
            pnl = (exec_price - pos.entry_price) * pos.size
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
                fees=0.0
            )
            self.account.closed_trades.append(trade)
            self.account.total_pnl += pnl
            del self.positions[symbol]

            logger.info(f"[LIVE SELL] {symbol} @ ${exec_price:.2f} | PnL: ${pnl:.2f} ({pnl_pct:.2f}%) | {reason}")

            # Record to journal for learning
            entry_signal = self._entry_signals.pop(symbol, None)
            record = self.journal.build_record(trade, symbol, reason, entry_signal)
            self.journal.add(record)
            self.learner.log_summary()

            total_equity = self.account.initial_capital + self.account.total_pnl
            self._notify_trade(symbol, exec_price, pnl, pnl_pct, reason, total_equity)
            return trade
        except Exception as e:
            logger.error(f"Sell order failed for {symbol}: {e}")
            return None

    def update_unrealized(self, prices: Dict[str, float]):
        for sym, pos in self.positions.items():
            if sym in prices:
                pos.unrealized_pnl = (prices[sym] - pos.entry_price) * pos.size

    def get_summary(self) -> dict:
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        wins = [t for t in self.account.closed_trades if t.pnl > 0]
        losses = [t for t in self.account.closed_trades if t.pnl <= 0]
        return {
            'cash': 0,       # live — get from exchange
            'total_equity': self.account.initial_capital + self.account.total_pnl + total_unrealized,
            'total_pnl': round(self.account.total_pnl, 4),
            'pnl_pct': round(self.account.total_pnl / self.account.initial_capital * 100, 2),
            'open_positions': len(self.positions),
            'closed_trades': len(self.account.closed_trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
        }

    def _notify(self, msg: str):
        if self.notifier:
            try:
                self.notifier.send_message(msg)
            except Exception:
                pass

    def _notify_trade(self, symbol: str, price: float, pnl: float,
                      pnl_pct: float, reason: str, total_equity: float):
        if pnl > 0:
            # Scale emoji count to size of win
            money_emojis = "💰" * min(10, max(1, int(pnl // 5) + 1))
            msg = (
                f"💸💸💸 <b>WIN</b> 💸💸💸\n\n"
                f"{money_emojis}\n\n"
                f"<b>+${pnl:.2f}</b>  ({pnl_pct:+.2f}%)\n\n"
                f"Pair:      <code>{symbol}</code>\n"
                f"Exit:      <code>${price:.2f}</code>\n"
                f"Reason:    {reason}\n\n"
                f"💼 Account: <b>${total_equity:.2f}</b>"
            )
        else:
            msg = (
                f"🔴 <b>LOSS</b>\n\n"
                f"<b>${pnl:.2f}</b>  ({pnl_pct:+.2f}%)\n\n"
                f"Pair:      <code>{symbol}</code>\n"
                f"Exit:      <code>${price:.2f}</code>\n"
                f"Reason:    {reason}\n\n"
                f"💼 Account: <b>${total_equity:.2f}</b>"
            )
        self._notify(msg)


CONFIDENCE_THRESHOLD = 75   # minimum score to place a trade
SLTP_POLL_SECS      = 15   # how often to check SL/TP between candles
CANDLE_OFFSET_SECS  = 5    # seconds after candle close before fetching


def _seconds_until_next_candle(timeframe: str = '1h') -> float:
    """Return seconds until the next candle close + offset."""
    import time
    now = time.time()
    if timeframe == '1h':
        period = 3600
    elif timeframe == '5m':
        period = 300
    else:
        period = 60
    seconds_into_period = now % period
    return max(1.0, period - seconds_into_period + CANDLE_OFFSET_SECS)


async def _check_higher_timeframes(exchange: ExchangeConnection,
                                    symbol: str, direction: str) -> bool:
    """
    Return True only if 5m and 15m agree with the 1m signal direction.
    Checks: 5m EMA fast > slow (uptrend) and price > 50 EMA on 15m.
    """
    try:
        # 5-minute check
        ohlcv_5m = await exchange.fetch_ohlcv(symbol, '5m', limit=60)
        if not ohlcv_5m:
            return False
        df5 = prepare_ohlcv_dataframe(ohlcv_5m)
        import pandas_ta as ta
        ema9_5m  = ta.ema(df5['close'], length=9).iloc[-1]
        ema21_5m = ta.ema(df5['close'], length=21).iloc[-1]
        rsi_5m   = ta.rsi(df5['close'], length=14).iloc[-1]

        # 15-minute check
        ohlcv_15m = await exchange.fetch_ohlcv(symbol, '15m', limit=60)
        if not ohlcv_15m:
            return False
        df15 = prepare_ohlcv_dataframe(ohlcv_15m)
        ema50_15m = ta.ema(df15['close'], length=50).iloc[-1]
        price_15m = df15['close'].iloc[-1]

        if direction == 'BUY':
            tf5_ok  = ema9_5m > ema21_5m and rsi_5m < 70
            tf15_ok = price_15m > ema50_15m
        else:
            tf5_ok  = ema9_5m < ema21_5m and rsi_5m > 30
            tf15_ok = price_15m < ema50_15m

        return tf5_ok and tf15_ok
    except Exception as e:
        logger.debug(f"Higher TF check failed for {symbol}: {e}")
        return False


async def run_live_trading_session(exchange: ExchangeConnection,
                                    trader: LiveTrader,
                                    symbols: List[str],
                                    timeframe: str = '1h',
                                    lookback: int = 250):
    import time
    trader.running = True
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"LIVE TRADING STARTED — {symbols} | confidence threshold: {CONFIDENCE_THRESHOLD}")

    iteration = 0
    last_candle_times: Dict[str, object] = {}
    prices: Dict[str, float] = {}
    indicators: Dict[str, dict] = {}
    recent_trades: List[dict] = []
    equity_curve: List[dict] = []
    last_sltp_check = 0.0

    while trader.running:
        try:
            now = time.time()

            # ── SL/TP fast poll (every SLTP_POLL_SECS) ──────────────────────
            if now - last_sltp_check >= SLTP_POLL_SECS and trader.positions:
                for symbol in list(trader.positions.keys()):
                    try:
                        ticker = await exchange.get_ticker(symbol)
                        price = float(ticker.get('last', 0))
                        if price <= 0:
                            continue
                        prices[symbol] = price
                        pos = trader.positions.get(symbol)
                        if pos is None:
                            continue
                        if price <= pos.stop_loss_price:
                            trade = await trader.close_position(symbol, price, "STOP_LOSS")
                            if trade:
                                recent_trades.append(_trade_dict(trade, symbol, "STOP_LOSS"))
                                equity_curve.append({'t': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'), 'v': round(trader.account.initial_capital + trader.account.total_pnl, 2)})
                        elif price >= pos.take_profit_price:
                            trade = await trader.close_position(symbol, price, "TAKE_PROFIT")
                            if trade:
                                recent_trades.append(_trade_dict(trade, symbol, "TAKE_PROFIT"))
                                equity_curve.append({'t': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'), 'v': round(trader.account.initial_capital + trader.account.total_pnl, 2)})
                    except Exception as e:
                        logger.debug(f"SL/TP poll error {symbol}: {e}")
                last_sltp_check = now

            # ── Sleep until next candle close ────────────────────────────────
            sleep_secs = _seconds_until_next_candle(timeframe)
            await asyncio.sleep(sleep_secs)
            iteration += 1

            # ── Process new 1m candles ───────────────────────────────────────
            for symbol in symbols:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=lookback)
                if not ohlcv:
                    continue

                df = prepare_ohlcv_dataframe(ohlcv)
                current_price = float(df['close'].iloc[-1])
                current_time = df.index[-1]

                # Skip if candle already processed
                if last_candle_times.get(symbol) == current_time:
                    continue
                last_candle_times[symbol] = current_time
                prices[symbol] = current_price

                signal = trader.strategy.get_latest_signal(df)
                if signal is None:
                    continue

                # Use the confidence score already computed by the strategy
                score = signal.confidence

                indicators[symbol] = {
                    'signal': signal.signal.value,
                    'confidence': score,
                    'rsi': round(signal.rsi, 2) if signal.rsi else None,
                    'ema_fast': round(signal.ema_fast, 2) if signal.ema_fast else None,
                    'ema_slow': round(signal.ema_slow, 2) if signal.ema_slow else None,
                    'adx': round(signal.adx, 2) if signal.adx else None,
                    'atr': round(signal.atr, 4) if signal.atr else None,
                    'volume_ratio': round(signal.volume_ratio, 2) if signal.volume_ratio else None,
                }

                # ── Entry: buy signal + learner-adjusted confidence ──────────
                if signal.is_buy and symbol not in trader.positions:
                    # Ask learner what threshold is needed given current conditions
                    current_features = {
                        'rsi':          signal.rsi or 50.0,
                        'adx':          signal.adx or 20.0,
                        'volume_ratio': signal.volume_ratio if hasattr(signal, 'volume_ratio') and signal.volume_ratio else 1.0,
                        'atr_pct':      (signal.atr / signal.close * 100) if signal.atr and signal.close else 1.0,
                        'ema100_gap':   ((signal.close - signal.ema100) / signal.ema100 * 100) if hasattr(signal, 'ema100') and signal.ema100 else 0.0,
                        'ema200_gap':   ((signal.close - signal.ema200) / signal.ema200 * 100) if hasattr(signal, 'ema200') and signal.ema200 else 0.0,
                        'hour_utc':     float(datetime.now(timezone.utc).hour),
                        'day_of_week':  float(datetime.now(timezone.utc).weekday()),
                    }
                    regime = signal.regime if hasattr(signal, 'regime') else 'UNKNOWN'
                    required = trader.learner.required_confidence(current_features, regime, symbol)

                    if score < required:
                        logger.info(f"[SKIP BUY] {symbol} — score {score} < required {required} (learner)")
                        continue
                    tf_aligned = await _check_higher_timeframes(exchange, symbol, 'BUY')
                    if not tf_aligned:
                        logger.info(f"[SKIP BUY] {symbol} — higher TFs not aligned (score {score})")
                        continue
                    logger.info(f"[ENTER] {symbol} — score {score}/{required} | regime={regime} | TFs aligned")
                    trader.record_entry_signal(symbol, signal)
                    await trader.open_position(
                        symbol, current_price,
                        stop_loss=signal.stop_loss_price,
                        take_profit=signal.take_profit_price
                    )

                # ── Exit: sell signal ────────────────────────────────────────
                elif signal.is_sell and symbol in trader.positions:
                    trade = await trader.close_position(symbol, current_price, "SIGNAL")
                    if trade:
                        recent_trades.append(_trade_dict(trade, symbol, "SIGNAL"))
                        equity_curve.append({
                            't': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                            'v': round(trader.account.initial_capital + trader.account.total_pnl, 2)
                        })

            trader.update_unrealized(prices)

            summary = trader.get_summary()
            positions_data = {
                sym: {
                    'entry_time': pos.entry_time.isoformat(),
                    'entry_price': pos.entry_price,
                    'size': pos.size,
                    'stop_loss_price': pos.stop_loss_price,
                    'take_profit_price': pos.take_profit_price,
                    'unrealized_pnl': round(pos.unrealized_pnl, 4),
                }
                for sym, pos in trader.positions.items()
            }
            write_state({
                'status': 'running',
                'mode': 'live',
                'started_at': started_at,
                'iteration': iteration,
                'account': summary,
                'positions': positions_data,
                'prices': {k: round(v, 2) for k, v in prices.items()},
                'indicators': indicators,
                'recent_trades': recent_trades[-50:],
                'equity_curve': equity_curve[-200:],
                'learning': trader.journal.stats(),
            })

            if iteration % 10 == 0:
                logger.info(f"Iteration {iteration}: PnL=${summary['total_pnl']:.2f} | Open={summary['open_positions']}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Live trading loop error: {e}")
            await asyncio.sleep(10)

    trader.running = False
    logger.info("Live trading session ended")


def _trade_dict(trade, symbol, reason):
    return {
        'symbol': symbol,
        'entry_time': trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
        'exit_time': trade.exit_time.isoformat() if hasattr(trade.exit_time, 'isoformat') else str(trade.exit_time),
        'entry_price': round(trade.entry_price, 4),
        'exit_price': round(trade.exit_price, 4),
        'size': trade.size,
        'pnl': round(trade.pnl, 4),
        'pnl_pct': round(trade.pnl_pct, 2),
        'reason': reason,
    }
