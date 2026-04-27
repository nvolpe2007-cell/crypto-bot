"""
Backtesting Engine for Crypto Scalping Strategy
Tests strategy on historical data and provides PnL analysis
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging

from .indicators import EMACrossRSI, Signal, prepare_ohlcv_dataframe
from .exchange import ExchangeConnection

logger = logging.getLogger(__name__)


class PositionState(Enum):
    FLAT = "FLAT"
    LONG = "LONG"


@dataclass
class Trade:
    """Record of a single trade"""
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp]
    entry_price: float
    exit_price: Optional[float]
    size: float  # Amount in base currency
    side: str  # 'buy' or 'sell'
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0


@dataclass
class BacktestResult:
    """Complete backtest results"""
    trades: List[Trade]
    total_pnl: float
    total_return_pct: float
    win_rate: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    equity_curve: pd.Series


class Backtester:
    """
    Historical backtesting engine

    Simulates trading on historical data with realistic assumptions:
    - Trading fees (maker/taker)
    - Slippage on market orders
    - Position sizing based on capital
    """

    def __init__(self, initial_capital: float = 100.0,
                 position_size: float = 50.0,
                 fee_pct: float = 0.26,  # Kraken fee ~0.26%
                 slippage_pct: float = 0.1,  # Estimated slippage
                 stop_loss_pct: float = 2.0,
                 take_profit_pct: float = 3.0):
        self.initial_capital = initial_capital
        self.position_size = position_size
        self.fee_pct = fee_pct / 100
        self.slippage_pct = slippage_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.take_profit_pct = take_profit_pct / 100

        self.strategy = EMACrossRSI()

    def run(self, df: pd.DataFrame, symbol: str = "SYMBOL") -> BacktestResult:
        """
        Run backtest on historical data

        Args:
            df: DataFrame with OHLCV data, datetime index
            symbol: Trading pair symbol

        Returns:
            BacktestResult with all metrics
        """
        if df.empty or len(df) < 30:
            raise ValueError("Not enough data for backtest (need at least 30 candles)")

        # Calculate indicators
        df = self.strategy.calculate(df.copy())

        # Initialize tracking
        capital = self.initial_capital
        position: Optional[Trade] = None
        trades: List[Trade] = []
        equity_curve = []

        logger.info(f"Starting backtest on {len(df)} candles, initial capital: ${capital}")

        # Iterate through bars (skip warmup period)
        warmup = max(self.strategy.slow_ema, self.strategy.rsi_period) + 5

        for i in range(warmup, len(df)):
            row = df.iloc[i]
            current_time = row.name
            current_price = row['close']

            # Check for stop loss / take profit on open position
            if position:
                pnl_pct_current = (current_price - position.entry_price) / position.entry_price * 100

                # Stop loss hit
                if pnl_pct_current <= -self.stop_loss_pct * 100:
                    exit_price = current_price * (1 - self.slippage_pct)  # Slippage on exit
                    fee = exit_price * position.size * self.fee_pct
                    position.pnl = (exit_price - position.entry_price) * position.size - fee
                    position.pnl_pct = pnl_pct_current
                    position.exit_time = current_time
                    position.exit_price = exit_price
                    position.fees = fee

                    capital += position.pnl
                    trades.append(position)
                    logger.debug(f"Stop loss hit at {current_time}: PnL ${position.pnl:.2f}")
                    position = None

                # Take profit hit
                elif pnl_pct_current >= self.take_profit_pct * 100:
                    exit_price = current_price * (1 - self.slippage_pct)
                    fee = exit_price * position.size * self.fee_pct
                    position.pnl = (exit_price - position.entry_price) * position.size - fee
                    position.pnl_pct = pnl_pct_current
                    position.exit_time = current_time
                    position.exit_price = exit_price
                    position.fees = fee

                    capital += position.pnl
                    trades.append(position)
                    logger.debug(f"Take profit hit at {current_time}: PnL ${position.pnl:.2f}")
                    position = None

            # No position - check for entry signal
            if position is None and row['signal'] == Signal.BUY:
                # Calculate position size
                entry_price = current_price * (1 + self.slippage_pct)  # Slippage on entry
                size = min(self.position_size / entry_price, capital / entry_price)

                if size > 0:
                    fee = entry_price * size * self.fee_pct
                    position = Trade(
                        entry_time=current_time,
                        exit_time=None,
                        entry_price=entry_price,
                        exit_price=None,
                        size=size,
                        side='buy'
                    )
                    logger.debug(f"Entry at {current_time}: ${entry_price:.2f}, size: {size:.6f}")

            # Record equity
            equity = capital
            if position:
                unrealized_pnl = (current_price - position.entry_price) * position.size
                equity += unrealized_pnl
            equity_curve.append({'time': current_time, 'equity': equity})

        # Close any open position at end
        if position:
            last_row = df.iloc[-1]
            exit_price = last_row['close'] * (1 - self.slippage_pct)
            fee = exit_price * position.size * self.fee_pct
            position.pnl = (exit_price - position.entry_price) * position.size - fee
            position.pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100
            position.exit_time = last_row.name
            position.exit_price = exit_price
            position.fees = fee
            capital += position.pnl
            trades.append(position)
            equity_curve.append({'time': position.exit_time, 'equity': capital})

        # Calculate metrics
        equity_df = pd.DataFrame(equity_curve)
        equity_df.set_index('time', inplace=True)

        return self._calculate_metrics(trades, equity_df, capital)

    def _calculate_metrics(self, trades: List[Trade],
                           equity_curve: pd.Series,
                           final_capital: float) -> BacktestResult:
        """Calculate performance metrics"""

        total_pnl = final_capital - self.initial_capital
        total_return_pct = (total_pnl / self.initial_capital) * 100

        # Win/loss stats
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]

        win_rate = len(winning) / len(trades) * 100 if trades else 0
        avg_win = np.mean([t.pnl for t in winning]) if winning else 0
        avg_loss = np.mean([t.pnl for t in losing]) if losing else 0

        gross_profit = sum(t.pnl for t in winning)
        gross_loss = abs(sum(t.pnl for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Drawdown
        equity_values = equity_curve['equity'] if isinstance(equity_curve, pd.DataFrame) else equity_curve
        running_max = equity_values.expanding().max()
        drawdown = (equity_values - running_max) / running_max * 100
        max_drawdown = drawdown.min()
        max_drawdown_pct = abs(max_drawdown)

        # Sharpe ratio (assuming 1-minute bars, annualized)
        returns = equity_values.pct_change().dropna()
        sharpe = (returns.mean() / returns.std()) * np.sqrt(252 * 24 * 60) if returns.std() > 0 else 0

        logger.info(f"Backtest complete: {len(trades)} trades, Return: {total_return_pct:.2f}%, Win rate: {win_rate:.1f}%")

        return BacktestResult(
            trades=trades,
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            avg_win=avg_win,
            avg_loss=avg_loss,
            equity_curve=equity_values
        )


async def run_backtest(symbol: str = "BTC/USD",
                       start_date: str = "2024-06-01",
                       end_date: str = "2024-12-31",
                       initial_capital: float = 100,
                       position_size: float = 50):
    """
    Run a complete backtest fetching data from Kraken

    Args:
        symbol: Trading pair
        start_date: Backtest start (ISO format)
        end_date: Backtest end (ISO format)
        initial_capital: Starting capital
        position_size: USD per trade

    Returns:
        BacktestResult
    """
    logger.info(f"Fetching historical data for {symbol} from {start_date} to {end_date}")

    exchange = ExchangeConnection(sandbox=False)
    await exchange.connect()

    # Fetch historical 1-minute data
    ohlcv = await exchange.fetch_ohlcv_between(symbol, '1m', start_date, end_date)
    await exchange.disconnect()

    if not ohlcv:
        raise ValueError("No data fetched from exchange")

    # Convert to DataFrame
    df = prepare_ohlcv_dataframe(ohlcv)
    logger.info(f"Loaded {len(df)} candles")

    # Run backtest
    backtester = Backtester(
        initial_capital=initial_capital,
        position_size=position_size
    )
    result = backtester.run(df, symbol)

    return result


def print_backtest_report(result: BacktestResult):
    """Print formatted backtest report"""
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Total Trades:        {result.total_trades}")
    print(f"Winning Trades:      {result.winning_trades} ({result.win_rate:.1f}%)")
    print(f"Losing Trades:       {result.losing_trades}")
    print(f"Total PnL:           ${result.total_pnl:.2f}")
    print(f"Total Return:        {result.total_return_pct:.2f}%")
    print(f"Profit Factor:       {result.profit_factor:.2f}")
    print(f"Max Drawdown:        ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.2f}%)")
    print(f"Sharpe Ratio:        {result.sharpe_ratio:.2f}")
    print(f"Average Win:         ${result.avg_win:.2f}")
    print(f"Average Loss:        ${result.avg_loss:.2f}")
    print("=" * 60)

    # Show trade log
    if result.trades:
        print("\nTRADE LOG (first 10):")
        print("-" * 60)
        for i, trade in enumerate(result.trades[:10]):
            print(f"{i+1}. {trade.entry_time.strftime('%Y-%m-%d %H:%M')} | "
                  f"Entry: ${trade.entry_price:.2f} | "
                  f"Exit: ${trade.exit_price:.2f} | "
                  f"PnL: ${trade.pnl:.2f} ({trade.pnl_pct:.2f}%)")


if __name__ == '__main__':
    import asyncio

    async def main():
        result = await run_backtest(
            symbol="BTC/USD",
            start_date="2024-11-01",
            end_date="2024-12-31",
            initial_capital=100,
            position_size=50
        )
        print_backtest_report(result)

    asyncio.run(main())
