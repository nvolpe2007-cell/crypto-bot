"""
Unit tests for src/backtester.py — Backtester simulation engine.

What is tested (all without live network calls):
  - Input validation   : empty DataFrame, too-few rows, exactly-30-row edge case
  - Return type/shape  : BacktestResult, equity curve non-empty, trade count
  - Metrics math       : win_rate bounds, drawdown non-negative, sharpe float,
                         total_return_pct derived from total_pnl,
                         profit_factor = inf for all-win scenarios (bug fix)
  - Stop-loss          : triggered at correct threshold, loss bounded
  - Take-profit        : triggered at correct threshold, PnL positive
  - No-trade scenario  : consistent zero-state when no signals fire
  - Fee accounting     : fees reduce net PnL, zero fee > nonzero fee
  - _calculate_metrics : directly tested for profit_factor edge cases
"""

import math

import numpy as np
import pandas as pd
import pytest

from src.backtester import Backtester, BacktestResult, Trade
from src.indicators import Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(prices: list) -> pd.DataFrame:
    """Build an OHLCV DataFrame from a list of close prices."""
    p = np.array(prices, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(p), freq="1min")
    return pd.DataFrame(
        {
            "open":   p,
            "high":   p * 1.001,
            "low":    p * 0.999,
            "close":  p,
            "volume": np.ones(len(p)) * 1000.0,
        },
        index=idx,
    )


def _wave_df(n: int = 200) -> pd.DataFrame:
    """Sine-wave + noise prices that reliably produce EMA crossovers."""
    rng = np.random.default_rng(42)
    t = np.linspace(0, 6 * np.pi, n)
    prices = 50_000 + 4_000 * np.sin(t) + rng.normal(0, 80, n)
    return _make_df(prices.tolist())


def _inject_buy_at(backtester: Backtester, inject_idx: int) -> None:
    """Monkeypatch the strategy to force a BUY signal at a specific bar index."""
    original_calculate = backtester.strategy.calculate

    def _patched(df: pd.DataFrame) -> pd.DataFrame:
        df = original_calculate(df)
        if inject_idx < len(df):
            df.iloc[inject_idx, df.columns.get_loc("signal")] = Signal.BUY
        return df

    backtester.strategy.calculate = _patched


def _make_equity_series(values: list) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1min")
    return pd.DataFrame({"equity": values}, index=idx)


def _make_trade(pnl: float, entry: float = 50_000.0, exit_: float = None,
                size: float = 0.001) -> Trade:
    if exit_ is None:
        exit_ = entry + pnl / size
    now = pd.Timestamp("2024-01-01")
    return Trade(
        entry_time=now,
        exit_time=now + pd.Timedelta(minutes=5),
        entry_price=entry,
        exit_price=exit_,
        size=size,
        side="buy",
        pnl=pnl,
        pnl_pct=pnl / (entry * size) * 100,
        fees=0.0,
    )


# ── Input validation ──────────────────────────────────────────────────────────

class TestInputValidation:
    def test_empty_dataframe_raises(self):
        with pytest.raises(ValueError, match="Not enough data"):
            Backtester().run(pd.DataFrame(), "BTC/USD")

    def test_ten_rows_raises(self):
        with pytest.raises(ValueError, match="Not enough data"):
            Backtester().run(_make_df([50_000.0] * 10), "BTC/USD")

    def test_exactly_30_rows_accepted(self):
        result = Backtester().run(_make_df([50_000.0] * 30), "BTC/USD")
        assert isinstance(result, BacktestResult)

    def test_29_rows_raises(self):
        with pytest.raises(ValueError, match="Not enough data"):
            Backtester().run(_make_df([50_000.0] * 29), "BTC/USD")


# ── Return type and shape ─────────────────────────────────────────────────────

class TestReturnType:
    def test_returns_backtest_result(self):
        assert isinstance(Backtester().run(_wave_df(), "BTC/USD"), BacktestResult)

    def test_equity_curve_non_empty(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert len(result.equity_curve) > 0

    def test_trade_count_non_negative(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert result.total_trades >= 0

    def test_win_loss_sums_to_total(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert result.winning_trades + result.losing_trades == result.total_trades

    def test_trades_list_length_matches(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert len(result.trades) == result.total_trades


# ── Metrics math ──────────────────────────────────────────────────────────────

class TestMetricsMath:
    def test_win_rate_is_zero_when_no_trades(self):
        result = Backtester().run(_make_df([50_000.0] * 100), "BTC/USD")
        if result.total_trades == 0:
            assert result.win_rate == 0.0

    def test_win_rate_bounds(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert 0.0 <= result.win_rate <= 100.0

    def test_max_drawdown_pct_non_negative(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert result.max_drawdown_pct >= 0.0

    def test_sharpe_is_finite_float(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        assert isinstance(result.sharpe_ratio, float)
        assert math.isfinite(result.sharpe_ratio)

    def test_total_return_pct_derived_from_total_pnl(self):
        bt = Backtester(initial_capital=1000.0)
        result = bt.run(_wave_df(), "BTC/USD")
        expected = result.total_pnl / 1000.0 * 100
        assert result.total_return_pct == pytest.approx(expected, rel=1e-6)

    def test_avg_win_positive_when_wins_exist(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        if result.winning_trades > 0:
            assert result.avg_win > 0

    def test_avg_loss_non_positive_when_losses_exist(self):
        result = Backtester().run(_wave_df(), "BTC/USD")
        if result.losing_trades > 0:
            assert result.avg_loss <= 0


# ── profit_factor edge cases (via _calculate_metrics directly) ────────────────

class TestProfitFactor:
    def test_all_losing_trades_returns_zero(self):
        bt = Backtester(initial_capital=1000.0)
        trades = [_make_trade(-5.0), _make_trade(-3.0)]
        eq = _make_equity_series([1000.0, 995.0, 992.0])
        result = bt._calculate_metrics(trades, eq, 992.0)
        assert result.profit_factor == 0.0

    def test_all_winning_trades_returns_infinity(self):
        """Fixed bug: profit_factor must be inf (not 0) when there are no losses."""
        bt = Backtester(initial_capital=1000.0)
        trades = [_make_trade(10.0), _make_trade(8.0)]
        eq = _make_equity_series([1000.0, 1010.0, 1018.0])
        result = bt._calculate_metrics(trades, eq, 1018.0)
        assert math.isinf(result.profit_factor), (
            f"Expected inf profit_factor for all-wins, got {result.profit_factor}"
        )

    def test_mixed_trades_positive_finite_factor(self):
        bt = Backtester(initial_capital=1000.0)
        trades = [_make_trade(20.0), _make_trade(-5.0)]
        eq = _make_equity_series([1000.0, 1020.0, 1015.0])
        result = bt._calculate_metrics(trades, eq, 1015.0)
        assert result.profit_factor == pytest.approx(4.0)   # 20 / 5

    def test_no_trades_returns_zero(self):
        bt = Backtester(initial_capital=1000.0)
        eq = _make_equity_series([1000.0])
        result = bt._calculate_metrics([], eq, 1000.0)
        assert result.profit_factor == 0.0
        assert result.win_rate == 0.0
        assert result.total_trades == 0


# ── Stop-loss ─────────────────────────────────────────────────────────────────

class TestStopLoss:
    """BUY injected at bar 28; price then drops well past the 2% SL."""

    ENTRY = 50_000.0
    DROP  = 47_500.0   # 5% below entry, past 2% SL

    def _bt(self):
        bt = Backtester(
            stop_loss_pct=2.0, take_profit_pct=5.0,
            initial_capital=10_000.0, position_size=1_000.0,
        )
        prices = [self.ENTRY] * 60 + [self.DROP] * 40
        _inject_buy_at(bt, inject_idx=28)
        return bt, _make_df(prices)

    def test_stop_loss_closes_position(self):
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        assert result.total_trades >= 1
        assert any(t.pnl < 0 for t in result.trades), "Expected at least one losing trade"

    def test_stop_loss_loss_is_bounded(self):
        """Loss must not exceed stop_loss_pct + slippage significantly."""
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        for t in result.trades:
            if t.pnl < 0:
                loss_pct = abs(t.pnl_pct)
                assert loss_pct < 10.0, f"Stop-loss runaway loss: {loss_pct:.2f}%"

    def test_stop_loss_exit_below_entry(self):
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        for t in result.trades:
            if t.pnl < 0:
                assert t.exit_price < t.entry_price


# ── Take-profit ───────────────────────────────────────────────────────────────

class TestTakeProfit:
    """BUY injected at bar 28; price then rises well past the 3% TP."""

    ENTRY = 50_000.0
    RISE  = 52_500.0   # 5% above entry, past 3% TP

    def _bt(self):
        bt = Backtester(
            stop_loss_pct=2.0, take_profit_pct=3.0,
            initial_capital=10_000.0, position_size=1_000.0,
        )
        prices = [self.ENTRY] * 60 + [self.RISE] * 40
        _inject_buy_at(bt, inject_idx=28)
        return bt, _make_df(prices)

    def test_take_profit_closes_position(self):
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        assert result.total_trades >= 1
        assert any(t.pnl > 0 for t in result.trades), "Expected at least one winning trade"

    def test_take_profit_pnl_positive(self):
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        for t in result.trades:
            if t.pnl > 0:
                assert t.exit_price > t.entry_price

    def test_take_profit_win_rate_100(self):
        bt, df = self._bt()
        result = bt.run(df, "TEST/USD")
        if result.total_trades > 0:
            assert result.winning_trades == result.total_trades


# ── No-trade scenario ─────────────────────────────────────────────────────────

class TestNoTrades:
    def test_flat_market_consistent_zero_state(self):
        """Flat price → no EMA crossover → 0 trades → metrics are valid zeros."""
        result = Backtester().run(_make_df([50_000.0] * 100), "BTC/USD")
        if result.total_trades == 0:
            assert result.win_rate    == 0.0
            assert result.profit_factor == 0.0
            assert result.total_pnl   == pytest.approx(0.0, abs=0.01)
            assert result.winning_trades == 0
            assert result.losing_trades  == 0


# ── Fee accounting ────────────────────────────────────────────────────────────

class TestFees:
    ENTRY = 50_000.0
    RISE  = 52_500.0

    def _make(self, fee_pct: float):
        bt = Backtester(
            fee_pct=fee_pct, stop_loss_pct=2.0, take_profit_pct=3.0,
            initial_capital=10_000.0, position_size=1_000.0,
        )
        prices = [self.ENTRY] * 60 + [self.RISE] * 40
        _inject_buy_at(bt, inject_idx=28)
        return bt.run(_make_df(prices), "TEST/USD")

    def test_fees_reduce_net_pnl(self):
        """Gross PnL (price diff × size) must be >= net PnL (after fees)."""
        r = self._make(fee_pct=0.26)
        for t in r.trades:
            gross = (t.exit_price - t.entry_price) * t.size
            assert t.pnl <= gross

    def test_lower_fee_yields_higher_pnl(self):
        """Zero-fee run must have pnl >= 0.26%-fee run on identical signals."""
        r_fee   = self._make(fee_pct=0.26)
        r_nofee = self._make(fee_pct=0.0)
        if r_fee.total_trades > 0 and r_nofee.total_trades > 0:
            assert r_nofee.total_pnl >= r_fee.total_pnl

    def test_fee_tracked_per_trade(self):
        r = self._make(fee_pct=0.26)
        for t in r.trades:
            assert t.fees >= 0.0
