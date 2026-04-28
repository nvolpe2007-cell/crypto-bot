"""
Paper Trading Engine
Simulates live trading with fake money for testing
"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import logging

from .indicators import Signal, prepare_ohlcv_dataframe
from .advanced_strategy import AdvancedStrategy
from .exchange import ExchangeConnection
from .backtester import Trade
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
        self.fee_pct = fee_pct / 100
        self.slippage_pct = slippage_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.take_profit_pct = take_profit_pct / 100

        self.strategy = AdvancedStrategy()
        self.running = False
        self._started_at = None

    def execute_buy(self, symbol: str, price: float, timestamp: datetime) -> Optional[PaperPosition]:
        """Execute a simulated buy order"""
        # Calculate size
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


async def run_paper_trading_session(exchange: ExchangeConnection,
                                     trader: PaperTrader,
                                     symbols: List[str],
                                     timeframe: str = '1m',
                                     lookback: int = 100,
                                     mode: str = 'paper'):
    """Run paper trading on live market data using the advanced strategy."""
    from datetime import datetime, timezone
    trader.running = True
    trader._started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"Starting paper trading session for {symbols}")

    iteration = 0
    last_candle_times = {}
    prices: Dict[str, float] = {}
    indicators: Dict[str, dict] = {}
    recent_trades: List[dict] = []

    while trader.running:
        try:
            iteration += 1

            for symbol in symbols:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=lookback)
                if not ohlcv:
                    continue

                df = prepare_ohlcv_dataframe(ohlcv)
                current_price = float(df['close'].iloc[-1])
                current_time = df.index[-1]

                if symbol in last_candle_times and last_candle_times[symbol] == current_time:
                    continue
                last_candle_times[symbol] = current_time

                prices[symbol] = current_price

                # Check SL/TP on open positions
                if symbol in trader.account.positions:
                    pos = trader.account.positions[symbol]
                    pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                    if pnl_pct <= -trader.stop_loss_pct * 100:
                        trade = trader.execute_sell(symbol, current_price, current_time, reason="STOP_LOSS")
                        if trade:
                            recent_trades.append(_trade_to_dict(trade, symbol, "STOP_LOSS"))
                    elif pnl_pct >= trader.take_profit_pct * 100:
                        trade = trader.execute_sell(symbol, current_price, current_time, reason="TAKE_PROFIT")
                        if trade:
                            recent_trades.append(_trade_to_dict(trade, symbol, "TAKE_PROFIT"))

                # Get advanced signal
                signal_result = trader.strategy.get_latest_signal(df)
                if signal_result is None:
                    continue

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
                    trader.execute_buy(symbol, current_price, current_time)
                elif signal_result.is_sell and symbol in trader.account.positions:
                    trade = trader.execute_sell(symbol, current_price, current_time, reason="SIGNAL")
                    if trade:
                        recent_trades.append(_trade_to_dict(trade, symbol, "SIGNAL"))

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
            })

            if iteration % 10 == 0:
                logger.info(f"Iteration {iteration}: Equity=${summary['total_equity']:.2f}, PnL=${summary['total_pnl']:.2f}")

            await asyncio.sleep(60)

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
