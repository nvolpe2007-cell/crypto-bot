"""
Paper Trading Engine
Simulates live trading with fake money for testing
"""

import asyncio
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import logging

import pandas_ta as _pta
from .indicators import Signal, prepare_ohlcv_dataframe
from .advanced_strategy import AdvancedStrategy
from .exchange import ExchangeConnection
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS
from .regime_detector import RegimeDetector
from .portfolio_optimizer import PortfolioOptimizer
from .crypto_vol import CryptoVolMonitor
from .state import write_state

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    """Current open position in paper trading"""
    entry_time: datetime
    entry_price: float
    size: float
    side: str
    entry_fee: float = 0.0   # fee paid at entry — needed for accurate round-trip PnL
    unrealized_pnl: float = 0.0


@dataclass
class PaperAccount:
    """Paper trading account state"""
    initial_capital: float
    cash: float
    positions: Dict[str, PaperPosition] = field(default_factory=dict)
    closed_trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0


class PaperTrader:
    """
    Paper trading simulator

    Runs the strategy in real-time (or on historical data replay)
    with simulated order execution and no real money at risk
    """

    def __init__(self, initial_capital: float = 100.0,
                 position_size: float = 50.0,
                 fee_pct: float = 0.26,
                 slippage_pct: float = 0.1,
                 stop_loss_pct: float = 2.0,
                 take_profit_pct: float = 3.0):
        self.initial_capital = initial_capital
        self.account = PaperAccount(initial_capital=initial_capital, cash=initial_capital)
        self.position_size = position_size
        self._initial_position_size = position_size
        self.fee_pct = fee_pct / 100
        self.slippage_pct = slippage_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.take_profit_pct = take_profit_pct / 100

        self.strategy = AdvancedStrategy()
        self.running = False
        self._started_at = None

    def execute_buy(self, symbol: str, price: float, timestamp: datetime,
                    atr: Optional[float] = None) -> Optional[PaperPosition]:
        """Execute a simulated buy order. Uses ATR-based sizing when available."""
        # Risk 1% of equity per trade when ATR is known; else use fixed position_size
        if atr and atr > 0:
            risk_usd = self.account.cash * 0.01
            stop_dist = atr * 1.5
            atr_size = risk_usd / stop_dist
            size = min(atr_size, self.position_size / price, self.account.cash / price)
        else:
            size = min(self.position_size / price, self.account.cash / price)

        if size <= 0:
            logger.warning(f"Insufficient cash for buy: ${self.account.cash:.2f}")
            return None

        # Apply slippage and fees
        exec_price = price * (1 + self.slippage_pct)
        fee = exec_price * size * self.fee_pct
        total_cost = exec_price * size + fee

        if total_cost > self.account.cash:
            logger.warning(f"Cannot afford position after fees: ${total_cost:.2f}")
            return None

        # Update account
        self.account.cash -= total_cost

        position = PaperPosition(
            entry_time=timestamp,
            entry_price=exec_price,
            size=size,
            side='buy',
            entry_fee=fee,
        )
        self.account.positions[symbol] = position

        logger.info(f"[PAPER BUY] {symbol} @ ${exec_price:.2f} | Size: {size:.6f} | Fee: ${fee:.4f}")
        return position

    def execute_sell(self, symbol: str, price: float, timestamp: datetime,
                     reason: str = "signal") -> Optional[Trade]:
        """Execute a simulated sell order"""
        if symbol not in self.account.positions:
            return None

        position = self.account.positions[symbol]

        # Apply slippage and fees
        exec_price = price * (1 - self.slippage_pct)
        exit_fee = exec_price * position.size * self.fee_pct
        total_fees = exit_fee + position.entry_fee

        # Net PnL: price gain minus both entry and exit fees
        pnl = (exec_price - position.entry_price) * position.size - total_fees
        cost_basis = position.entry_price * position.size + position.entry_fee
        pnl_pct = pnl / cost_basis * 100

        # Update account
        self.account.cash += exec_price * position.size - exit_fee
        self.account.total_pnl += pnl

        # Create trade record
        trade = Trade(
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=exec_price,
            size=position.size,
            side='sell',
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=total_fees
        )
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]

        logger.info(f"[PAPER SELL] {symbol} @ ${exec_price:.2f} | PnL: ${pnl:.2f} ({pnl_pct:.2f}%) | Reason: {reason}")
        return trade

    def check_stop_loss_take_profit(self, symbol: str, current_price: float,
                                     timestamp: datetime) -> Optional[Trade]:
        """Check if SL/TP should be triggered"""
        if symbol not in self.account.positions:
            return None

        position = self.account.positions[symbol]
        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100

        # Stop loss
        if pnl_pct <= -self.stop_loss_pct * 100:
            return self.execute_sell(symbol, current_price, timestamp, reason="STOP_LOSS")

        # Take profit
        if pnl_pct >= self.take_profit_pct * 100:
            return self.execute_sell(symbol, current_price, timestamp, reason="TAKE_PROFIT")

        return None

    def update_unrealized_pnl(self, prices: Dict[str, float]):
        """Update unrealized PnL for all open positions"""
        for symbol, position in self.account.positions.items():
            if symbol in prices:
                current_price = prices[symbol]
                position.unrealized_pnl = (current_price - position.entry_price) * position.size

    def get_account_summary(self) -> Dict:
        """Get current account status"""
        # position market value = cost basis + unrealized price movement
        position_values = sum(
            p.entry_price * p.size + p.unrealized_pnl
            for p in self.account.positions.values()
        )
        total_equity = self.account.cash + position_values

        return {
            'cash': self.account.cash,
            'total_equity': total_equity,
            'total_pnl': self.account.total_pnl,
            'pnl_pct': (self.account.total_pnl / self.initial_capital) * 100,
            'open_positions': len(self.account.positions),
            'closed_trades': len(self.account.closed_trades),
            'winning_trades': len([t for t in self.account.closed_trades if t.pnl > 0]),
            'losing_trades': len([t for t in self.account.closed_trades if t.pnl <= 0])
        }

    def print_summary(self):
        """Print account summary to console"""
        summary = self.get_account_summary()
        print("\n" + "=" * 50)
        print("PAPER TRADING ACCOUNT")
        print("=" * 50)
        print(f"Cash:            ${summary['cash']:.2f}")
        print(f"Total Equity:    ${summary['total_equity']:.2f}")
        print(f"Total PnL:       ${summary['total_pnl']:.2f} ({summary['pnl_pct']:.2f}%)")
        print(f"Open Positions:  {summary['open_positions']}")
        print(f"Closed Trades:   {summary['closed_trades']} ({summary['winning_trades']}W / {summary['losing_trades']}L)")
        print("=" * 50)


async def _check_higher_timeframes(exchange: ExchangeConnection,
                                    symbol: str, direction: str) -> bool:
    """
    5m trend must not be strongly against the 1m signal.
    15m EMA50 used as a soft filter — only blocks if price is more than 1.5% against trend.
    Falls back to True if data unavailable so the trade still proceeds.
    """
    try:
        ohlcv_5m = await exchange.fetch_ohlcv(symbol, '5m', limit=60)
        if not ohlcv_5m:
            return True   # no data — allow trade
        df5 = prepare_ohlcv_dataframe(ohlcv_5m)
        ema9_5m  = _pta.ema(df5['close'], length=9).iloc[-1]
        ema21_5m = _pta.ema(df5['close'], length=21).iloc[-1]
        rsi_5m   = _pta.rsi(df5['close'], length=14).iloc[-1]

        ohlcv_15m = await exchange.fetch_ohlcv(symbol, '15m', limit=60)
        df15 = prepare_ohlcv_dataframe(ohlcv_15m) if ohlcv_15m else None
        price_15m = float(df15['close'].iloc[-1]) if df15 is not None else None
        ema50_15m = float(_pta.ema(df15['close'], length=50).iloc[-1]) if df15 is not None else None

        if direction == 'BUY':
            # Block if 5m is in clear downtrend (EMA9 well below EMA21)
            ema_gap_pct = (ema9_5m - ema21_5m) / ema21_5m * 100
            if ema_gap_pct < -0.15:   # 5m strongly bearish
                logger.info(f"[MTF] {symbol} BUY blocked — 5m EMA gap {ema_gap_pct:.2f}%")
                return False
            # Block if RSI already overbought on 5m
            if rsi_5m >= 72:
                logger.info(f"[MTF] {symbol} BUY blocked — 5m RSI {rsi_5m:.1f}")
                return False
            # Soft 15m check: only block if price is >1.5% below EMA50
            if price_15m and ema50_15m:
                gap = (price_15m - ema50_15m) / ema50_15m * 100
                if gap < -1.5:
                    logger.info(f"[MTF] {symbol} BUY blocked — 15m price {gap:.1f}% below EMA50")
                    return False
        else:
            ema_gap_pct = (ema9_5m - ema21_5m) / ema21_5m * 100
            if ema_gap_pct > 0.15:
                logger.info(f"[MTF] {symbol} SELL blocked — 5m EMA gap {ema_gap_pct:.2f}%")
                return False
            if rsi_5m <= 28:
                logger.info(f"[MTF] {symbol} SELL blocked — 5m RSI {rsi_5m:.1f}")
                return False
            if price_15m and ema50_15m:
                gap = (price_15m - ema50_15m) / ema50_15m * 100
                if gap > 1.5:
                    logger.info(f"[MTF] {symbol} SELL blocked — 15m price {gap:.1f}% above EMA50")
                    return False

        return True
    except Exception as e:
        logger.debug(f"Higher TF check failed for {symbol}: {e}")
        return True   # fail open — allow trade


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
    """Run paper trading on live market data using the advanced strategy."""
    from datetime import datetime, timezone
    trader.running = True
    trader._started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"Starting paper trading session for {symbols}")

    if notifier:
        notifier.send_message(
            f"<b>PAPER STARTED</b>\n"
            f"{' '.join(s.split('/')[0] for s in symbols)}   {timeframe}\n"
            f"capital ${trader.initial_capital:.2f}"
        )

    regime_detector = RegimeDetector()
    portfolio_opt   = PortfolioOptimizer()
    symbol_returns: Dict[str, List[float]] = {s: [] for s in symbols}
    regime_cache:   Dict[str, dict] = {}   # per-symbol regime results

    import os
    max_daily_loss_usd = float(os.getenv('MAX_DAILY_LOSS', 10))
    session_start_equity = trader.initial_capital

    iteration = 0
    last_candle_times = {}
    prices: Dict[str, float] = {}
    indicators: Dict[str, dict] = {}
    recent_trades: List[dict] = []

    # Hourly Telegram digest
    async def _hourly_digest():
        await asyncio.sleep(3600)
        while trader.running:
            if notifier:
                s = trader.get_account_summary()
                notifier.send_status(
                    capital=s['total_equity'],
                    pnl=s['total_pnl'],
                    pnl_pct=s['pnl_pct'],
                    open_positions=s['open_positions'],
                    trades_today=s['closed_trades'],
                )
            await asyncio.sleep(3600)

    asyncio.create_task(_hourly_digest())

    # Background SL/TP watcher — runs every second using WS prices when available
    async def _sltp_watcher():
        from datetime import datetime, timezone
        while trader.running:
            await asyncio.sleep(1)
            if not trader.account.positions:
                continue
            ws_prices = public_ws.get_prices() if public_ws else {}
            for symbol in list(trader.account.positions.keys()):
                price = ws_prices.get(symbol) or prices.get(symbol)
                if not price:
                    continue
                pos = trader.account.positions.get(symbol)
                if not pos:
                    continue
                pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                now = datetime.now(timezone.utc)
                if pnl_pct <= -trader.stop_loss_pct * 100:
                    trade = trader.execute_sell(symbol, price, now, reason="STOP_LOSS")
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, symbol, "STOP_LOSS"))
                        s = trader.get_account_summary()
                        equity_curve.append({'t': now.strftime('%Y-%m-%d %H:%M'), 'v': round(s['total_equity'], 2)})
                        if notifier:
                            notifier.send_loss(symbol, trade.pnl, trade.pnl_pct, price, s['total_equity'], reason="Stop Loss")
                elif pnl_pct >= trader.take_profit_pct * 100:
                    trade = trader.execute_sell(symbol, price, now, reason="TAKE_PROFIT")
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, symbol, "TAKE_PROFIT"))
                        s = trader.get_account_summary()
                        equity_curve.append({'t': now.strftime('%Y-%m-%d %H:%M'), 'v': round(s['total_equity'], 2)})
                        if notifier:
                            notifier.send_win(symbol, trade.pnl, trade.pnl_pct, price, s['total_equity'], reason="Take Profit")

    asyncio.create_task(_sltp_watcher())

    equity_curve: List[dict] = []

    while trader.running:
        try:
            iteration += 1

            # ── Max daily loss circuit breaker ───────────────────────────────
            current_equity = trader.get_account_summary()['total_equity']
            daily_loss = session_start_equity - current_equity
            if daily_loss >= max_daily_loss_usd:
                logger.warning(
                    f"[RISK] Daily loss limit hit: ${daily_loss:.2f} >= ${max_daily_loss_usd:.2f} — halting new trades"
                )
                if notifier:
                    notifier.send_message(
                        f"<b>DAILY LOSS LIMIT HIT</b>\n"
                        f"Loss ${daily_loss:.2f} reached limit ${max_daily_loss_usd:.2f}\n"
                        f"No new trades until next session"
                    )
                trader.running = False
                break

            # ── Decide which symbol to process this iteration ────────────────
            if public_ws:
                # Event-driven: block until a confirmed candle close arrives
                try:
                    candle = await asyncio.wait_for(
                        public_ws.candle_queue.get(), timeout=90
                    )
                    trigger_symbols = [candle.symbol] if candle.symbol in symbols else symbols
                except asyncio.TimeoutError:
                    trigger_symbols = symbols   # fallback if no event in 90s
            else:
                trigger_symbols = symbols
                await asyncio.sleep(60)

            for symbol in trigger_symbols:
                # ── Fetch fresh candle history for strategy calculation ───────
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=lookback)
                if not ohlcv:
                    continue

                df = prepare_ohlcv_dataframe(ohlcv)
                current_price = (
                    public_ws.get_price(symbol)
                    or float(df['close'].iloc[-1])
                )
                current_time = df.index[-1]

                if symbol in last_candle_times and last_candle_times[symbol] == current_time:
                    continue
                last_candle_times[symbol] = current_time
                prices[symbol] = current_price

                # Get advanced signal
                signal_result = trader.strategy.get_latest_signal(df)
                if signal_result is None:
                    continue

                # ── Regime detection ─────────────────────────────────────────
                regime_result = regime_detector.detect(df)
                if regime_result:
                    regime_cache[symbol] = regime_result.to_dict()

                # ── Track returns for CVaR optimizer ─────────────────────────
                if len(df) >= 2:
                    ret = float(df['close'].pct_change().iloc[-1])
                    if not pd.isna(ret):
                        symbol_returns[symbol].append(ret)
                        if len(symbol_returns[symbol]) > 500:
                            symbol_returns[symbol].pop(0)

                # ── Regime gate: hard block on crash/downtrend ───────────────
                regime_blocks_long = (
                    regime_result is not None and
                    regime_result.regime in ('CRASH', 'TRENDING_DOWN')
                )

                # ── Position sizing: CVaR + regime + IV ──────────────────────
                base_size = trader.initial_capital * 0.5
                cvar_weight = (
                    portfolio_opt._last_weights.get(symbol, 1.0 / len(symbols))
                    if portfolio_opt._last_weights else 1.0 / len(symbols)
                )
                sized = base_size * cvar_weight * len(symbols)
                if regime_result:
                    regime_scale = {
                        'CRASH': 0.0, 'TRENDING_DOWN': 0.0,
                        'VOLATILE': 0.4, 'RANGING': 0.7,
                        'TRENDING_UP': 1.0,
                    }.get(regime_result.regime, 0.8)
                    sized *= regime_scale
                if vol_monitor:
                    sized *= vol_monitor.get_size_multiplier(symbol)
                ema50_val = _pta.ema(df['close'], length=50).iloc[-1]
                if ema50_val and not pd.isna(ema50_val):
                    if (current_price - ema50_val) / ema50_val * 100 < -3.0:
                        sized *= 0.5
                # Only enforce minimum if not a blocked regime
                trader.position_size = (
                    max(10.0, min(sized, trader.initial_capital * 0.8))
                    if sized > 0 else 0.0
                )

                # Re-run CVaR optimizer every 50 iterations
                if iteration % 50 == 0 and any(len(v) >= 20 for v in symbol_returns.values()):
                    portfolio_opt.optimize(symbol_returns)

                # Update per-symbol stop/tp from ATR
                if signal_result.atr and signal_result.atr > 0:
                    trader.stop_loss_pct = signal_result.stop_loss_pct() / 100
                    trader.take_profit_pct = signal_result.take_profit_pct() / 100

                indicators[symbol] = {
                    'signal': signal_result.signal.value,
                    'rsi': round(signal_result.rsi, 2) if signal_result.rsi else None,
                    'ema_fast': round(signal_result.ema_fast, 2) if signal_result.ema_fast else None,
                    'ema_slow': round(signal_result.ema_slow, 2) if signal_result.ema_slow else None,
                    'macd': round(signal_result.macd, 4) if signal_result.macd else None,
                    'macd_hist': round(signal_result.macd_hist, 4) if signal_result.macd_hist else None,
                    'adx': round(signal_result.adx, 2) if signal_result.adx else None,
                    'atr': round(signal_result.atr, 4) if signal_result.atr else None,
                    'volume_ratio': round(signal_result.volume_ratio, 2) if signal_result.volume_ratio else None,
                }

                if signal_result.is_buy and symbol not in trader.account.positions:
                    if regime_blocks_long:
                        logger.info(f"[SKIP BUY] {symbol} — regime {regime_result.regime} blocks longs")
                    elif trader.position_size <= 0:
                        logger.info(f"[SKIP BUY] {symbol} — position size reduced to zero by risk filters")
                    elif sentiment_monitor and not sentiment_monitor.allows_long(symbol):
                        logger.info(f"[SKIP BUY] {symbol} — sentiment gate blocked entry")
                    else:
                        tf_aligned = await _check_higher_timeframes(exchange, symbol, 'BUY')
                        if not tf_aligned:
                            logger.info(f"[SKIP BUY] {symbol} — 5m/15m not aligned @ ${current_price:.2f}")
                        else:
                            position = trader.execute_buy(symbol, current_price, current_time,
                                                          atr=signal_result.atr)
                            if position and notifier:
                                notifier.send_trade_alert(
                                    action="BUY", symbol=symbol, price=current_price,
                                    size=position.size * current_price,
                                    reason=f"EMA crossover | RSI {signal_result.rsi:.1f} | ADX {signal_result.adx:.1f} | TFs aligned"
                                )
                elif signal_result.is_sell and symbol in trader.account.positions:
                    tf_aligned = await _check_higher_timeframes(exchange, symbol, 'SELL')
                    if not tf_aligned:
                        logger.info(f"[SKIP SELL] {symbol} — 5m/15m not aligned @ ${current_price:.2f}")
                    else:
                        trade = trader.execute_sell(symbol, current_price, current_time, reason="SIGNAL")
                        if trade:
                            recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))
                            summary = trader.get_account_summary()
                            equity_curve.append({'t': current_time.strftime('%Y-%m-%d %H:%M') if hasattr(current_time, 'strftime') else str(current_time), 'v': round(summary['total_equity'], 2)})
                            if notifier:
                                if trade.pnl >= 0:
                                    notifier.send_win(symbol, trade.pnl, trade.pnl_pct, current_price, summary['total_equity'], reason="Signal")
                                else:
                                    notifier.send_loss(symbol, trade.pnl, trade.pnl_pct, current_price, summary['total_equity'], reason="Signal")

            # Update prices from WS if available
            if public_ws:
                for sym, p in public_ws.get_prices().items():
                    if sym in symbols:
                        prices[sym] = p
            trader.update_unrealized_pnl(prices)

            # Write state for dashboard
            summary = trader.get_account_summary()
            positions_data = {
                sym: {
                    'entry_time': pos.entry_time.isoformat() if hasattr(pos.entry_time, 'isoformat') else str(pos.entry_time),
                    'entry_price': pos.entry_price,
                    'size': pos.size,
                    'unrealized_pnl': round(pos.unrealized_pnl, 4),
                }
                for sym, pos in trader.account.positions.items()
            }
            sentiment_data = (
                sentiment_monitor.get_snapshot().to_dict()
                if sentiment_monitor and sentiment_monitor.get_snapshot()
                else None
            )
            write_state({
                'status': 'running',
                'mode': mode,
                'started_at': trader._started_at,
                'iteration': iteration,
                'account': summary,
                'positions': positions_data,
                'prices': {k: round(v, 2) for k, v in prices.items()},
                'indicators': indicators,
                'recent_trades': recent_trades[-50:],
                'equity_curve': equity_curve[-200:],
                'sentiment': sentiment_data,
                'regime': regime_cache.get(symbols[0]) if regime_cache else None,
                'regime_all': regime_cache,
                'cvar': portfolio_opt.to_dict(),
                'iv': vol_monitor.to_dict() if vol_monitor else {},
                'ws_connected': public_ws is not None,
            })

            if iteration % 10 == 0:
                logger.info(f"Iteration {iteration}: Equity=${summary['total_equity']:.2f}, PnL=${summary['total_pnl']:.2f}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in paper trading loop: {e}")
            await asyncio.sleep(5)

    trader.running = False
    logger.info("Paper trading session ended")


def _trade_to_dict(trade, symbol: str, reason: str) -> dict:
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


if __name__ == '__main__':
    # Test paper trading
    async def main():
        exchange = ExchangeConnection(sandbox=False)
        await exchange.connect()

        trader = PaperTrader(
            initial_capital=100,
            position_size=50,
            stop_loss_pct=2.0,
            take_profit_pct=3.0
        )

        # Run for a limited time for testing
        try:
            await asyncio.wait_for(
                run_paper_trading_session(
                    exchange, trader,
                    symbols=['BTC/USD', 'ETH/USD'],
                    timeframe='1m'
                ),
                timeout=300  # Run for 5 minutes
            )
        except asyncio.TimeoutError:
            pass

        trader.print_summary()
        await exchange.disconnect()

    asyncio.run(main())
