"""
Unit tests for src/live_trading.py — LiveTrader order execution & accounting.

What is tested (all without real network calls or real money):
  - get_balance()        : happy path + exception → 0.0
  - reconcile_positions(): adds untracked exchange pos, removes ghost pos,
                           raises (does not swallow) on fetch failure
  - run_live_trading_session(): aborts startup without trading when
                           reconciliation fails, instead of proceeding blind
  - open_long()          : successful fill, insufficient balance, order failure,
                           unconfirmed fill status, correct SL/TP math
  - close_long()         : no position early-return, order failure, PnL sign
                           (profit & loss), position cleanup, trade appended
  - update_unrealized()  : positive / negative / multi-symbol
  - get_summary()        : equity, win/loss counts, pnl_pct, open_positions
  - _inject_live_price() : close updated, high raised, low lowered, copy returned
  - _quick_diagnose()    : OFI +/- issues/positives, STOP_LOSS, TAKE_PROFIT,
                           high/low confidence
"""

import sys
import types
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Stub modules that live_trading.py imports but are heavy or irrelevant ───────
# These stubs are installed before the first import of src.live_trading so that
# live_trading.py's top-level `from .xxx import ...` statements resolve cleanly.

def _ensure_stub(name):
    """Install a MagicMock at sys.modules[name] only if not already present."""
    if name not in sys.modules:
        sys.modules[name] = MagicMock()


for _mod in [
    "src.regime_detector",
    "src.order_flow",
    "src.lead_lag_detector",
    "src.multi_timeframe",
    "src.ml_scorer",
    "src.learner",
    "src.market_sentiment",
    "src.kraken_ws",
    "src.notifications",
    "src.state",
    "src.portfolio_optimizer",
]:
    _ensure_stub(_mod)

# ── Now import the real module under test ────────────────────────────────────────
from src.live_trading import (
    LiveTrader,
    LivePosition,
    LiveAccount,
    _inject_live_price,
    _quick_diagnose,
    run_live_trading_session,
    FEE_RATE,
)
from src.scientific_strategy import ScientificSignal
from src.indicators import Signal
from src.exchange import ExchangeConnection, CircuitBreakerOpen
from src.backtester import Trade

import pandas as pd
import numpy as np


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _make_signal(
    direction: str = "BUY",
    confidence: float = 80.0,
    atr: float = 500.0,
    close: float = 50_000.0,
    ofi: float = 0.20,
    rsi: float = 55.0,
    adx: float = 30.0,
) -> ScientificSignal:
    """Construct a minimal ScientificSignal for testing."""
    sig = Signal.BUY if direction == "BUY" else Signal.SELL
    return ScientificSignal(
        signal=sig,
        confidence=confidence,
        size_mult=1.0,
        ofi_score=20.0,
        lead_lag_score=15.0,
        regime_score=20.0,
        rsi_score=15.0,
        technical_score=10.0,
        funding_score=0.0,
        ofi=ofi,
        lead_lag_dir="BUY" if direction == "BUY" else "SELL",
        regime="TRENDING_UP",
        rsi=rsi,
        adx=adx,
        atr=atr,
        close=close,
        ema_fast=close * 1.001,
        ema_slow=close * 0.999,
        volume_ratio=1.2,
        funding_rate=0.0001,
    )


def _make_exchange() -> MagicMock:
    """Return a mock ExchangeConnection with async methods."""
    ex = MagicMock(spec=ExchangeConnection)
    ex.get_balance = AsyncMock(return_value={"USD": {"free": 1000.0}})
    ex.get_positions = AsyncMock(return_value=[])
    ex.create_order = AsyncMock(return_value={
        "id": "order-001",
        "status": "closed",
        "average": 50_000.0,
        "price": 50_000.0,
        "fee": {"cost": 1.30},
    })
    return ex


def _make_trader(initial_capital: float = 1000.0) -> LiveTrader:
    """Build a LiveTrader with mocked subsystems."""
    ex = _make_exchange()

    trader = LiveTrader.__new__(LiveTrader)
    trader.exchange = ex
    trader.symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    trader.notifier = None
    trader.sentiment_monitor = None
    trader.public_ws = None
    trader.private_ws = None
    trader.running = False

    trader.strategy = MagicMock()
    trader.regime_detector = MagicMock()
    trader.ofi_calc = MagicMock()
    trader.lead_lag = MagicMock()
    trader.htf_filter = MagicMock()
    trader.journal = MagicMock()
    trader.learner = MagicMock()
    trader.ml_scorer = MagicMock()
    trader.ml_scorer.should_retrain.return_value = False

    trader.account = LiveAccount(initial_capital=initial_capital)
    trader.positions = {}
    trader._started_at = datetime.now(timezone.utc).isoformat()
    return trader


def _open_position(
    symbol: str = "BTC/USD",
    entry_price: float = 50_000.0,
    size: float = 0.001,
    size_usd: float = 50.0,
    sl: float = 49_000.0,
    tp: float = 52_000.0,
    signal: Optional[ScientificSignal] = None,
) -> LivePosition:
    return LivePosition(
        symbol=symbol,
        entry_time=datetime.now(timezone.utc),
        entry_price=entry_price,
        size=size,
        size_usd=size_usd,
        order_id="order-001",
        stop_loss_price=sl,
        take_profit_price=tp,
        entry_signal=signal,
    )


# ── get_balance ───────────────────────────────────────────────────────────────────

class TestGetBalance:
    def test_returns_free_usd(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(
            return_value={"USD": {"free": 427.50}}
        )
        result = asyncio.run(trader.get_balance())
        assert result == pytest.approx(427.50)

    def test_returns_zero_on_exception(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(side_effect=RuntimeError("timeout"))
        result = asyncio.run(trader.get_balance())
        assert result == 0.0

    def test_returns_zero_when_usd_missing(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"BTC": {"free": 1.0}})
        result = asyncio.run(trader.get_balance())
        assert result == 0.0


# ── reconcile_positions ───────────────────────────────────────────────────────────

class TestReconcilePositions:
    def test_adds_untracked_exchange_position(self):
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(return_value=[
            {"symbol": "BTC/USD", "contracts": 0.001, "entryPrice": 50_000.0}
        ])
        asyncio.run(trader.reconcile_positions())
        assert "BTC/USD" in trader.positions
        pos = trader.positions["BTC/USD"]
        assert pos.entry_price == 50_000.0
        assert pos.order_id == "reconciled"

    def test_removes_ghost_position(self):
        """Bot thinks it has a position but exchange has none."""
        trader = _make_trader()
        trader.positions["ETH/USD"] = _open_position("ETH/USD")
        trader.exchange.get_positions = AsyncMock(return_value=[])
        asyncio.run(trader.reconcile_positions())
        assert "ETH/USD" not in trader.positions

    def test_fetch_failure_propagates_instead_of_assuming_empty(self):
        """A reconciliation failure must NOT be swallowed into "no open
        positions" — that could let the bot open a duplicate position on top
        of one already live on Kraken. The caller (run_live_trading_session)
        is responsible for aborting startup when this raises."""
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(
            side_effect=RuntimeError("network error")
        )
        with pytest.raises(RuntimeError):
            asyncio.run(trader.reconcile_positions())

    def test_ignores_zero_size_positions(self):
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(return_value=[
            {"symbol": "BTC/USD", "contracts": 0, "entryPrice": 50_000.0}
        ])
        asyncio.run(trader.reconcile_positions())
        assert "BTC/USD" not in trader.positions

    def test_ignores_unknown_symbol(self):
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(return_value=[
            {"symbol": "DOGE/USD", "contracts": 1000.0, "entryPrice": 0.15}
        ])
        asyncio.run(trader.reconcile_positions())
        assert "DOGE/USD" not in trader.positions


# ── open_long ────────────────────────────────────────────────────────────────────

class TestOpenLong:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_successful_fill_records_position(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "order-123",
            "status": "closed",
            "average": 50_000.0,
            "fee": {"cost": 1.30},
        })
        sig = _make_signal()
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))

        assert pos is not None
        assert "BTC/USD" in trader.positions
        assert trader.positions["BTC/USD"].order_id == "order-123"
        assert trader.positions["BTC/USD"].entry_price == 50_000.0

    def test_position_size_capped_at_95pct_balance(self):
        """size_usd larger than balance → capped at 95% of available funds."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 40.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "order-x", "status": "closed", "average": 1_000.0,
            "fee": {"cost": 0.10},
        })
        sig = _make_signal(close=1_000.0, atr=10.0)
        pos = self._run(trader.open_long("ETH/USD", 1_000.0, 200.0, sig))

        # 200 requested but only 40 * 0.95 = 38 available
        assert pos is not None
        assert pos.size_usd == pytest.approx(38.0)

    def test_insufficient_balance_returns_none(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 3.0}})
        sig = _make_signal()
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        assert pos is None
        assert "BTC/USD" not in trader.positions

    def test_order_exception_returns_none(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(side_effect=RuntimeError("rejected"))
        sig = _make_signal()
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        assert pos is None
        assert "BTC/USD" not in trader.positions

    def test_unconfirmed_status_returns_none(self):
        """An order that comes back with status 'open' must not be recorded."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "order-pending", "status": "open", "average": 50_000.0,
            "fee": {"cost": 0.0},
        })
        sig = _make_signal()
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        assert pos is None
        assert "BTC/USD" not in trader.positions

    def test_sl_tp_calculated_from_atr(self):
        """SL must be below entry price, TP above; ratio ≥ 2:1."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "o", "status": "closed", "average": 50_000.0,
            "fee": {"cost": 1.0},
        })
        sig = _make_signal(atr=500.0, close=50_000.0)
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))

        assert pos.stop_loss_price < pos.entry_price
        assert pos.take_profit_price > pos.entry_price
        sl_pct = (pos.entry_price - pos.stop_loss_price) / pos.entry_price
        tp_pct = (pos.take_profit_price - pos.entry_price) / pos.entry_price
        assert tp_pct / sl_pct >= 1.99

    def test_fee_accumulated_on_fill(self):
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "o", "status": "closed", "average": 50_000.0,
            "fee": {"cost": 2.60},
        })
        sig = _make_signal()
        self._run(trader.open_long("BTC/USD", 50_000.0, 100.0, sig))
        assert trader.account.total_fees == pytest.approx(2.60)

    def test_entry_fee_recorded_on_position(self):
        """The actual order fee must be stashed on the position so close_long()
        can use it later instead of re-deriving an estimate."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "o", "status": "closed", "average": 50_000.0,
            "fee": {"cost": 2.60},
        })
        sig = _make_signal()
        pos = self._run(trader.open_long("BTC/USD", 50_000.0, 100.0, sig))
        assert pos.entry_fee == pytest.approx(2.60)


# ── close_long ───────────────────────────────────────────────────────────────────

class TestCloseLong:
    def _run(self, coro):
        return asyncio.run(coro)

    def _setup_position(self, trader, symbol="BTC/USD",
                        entry_price=50_000.0, size=0.001, size_usd=50.0,
                        entry_fee=0.0):
        sig = _make_signal(close=entry_price)
        trader.positions[symbol] = LivePosition(
            symbol=symbol,
            entry_time=datetime.now(timezone.utc),
            entry_price=entry_price,
            size=size,
            size_usd=size_usd,
            order_id="entry-order",
            stop_loss_price=entry_price * 0.98,
            take_profit_price=entry_price * 1.03,
            entry_signal=sig,
            entry_fee=entry_fee,
        )

    def test_no_position_returns_none(self):
        trader = _make_trader()
        trade = self._run(trader.close_long("BTC/USD", 51_000.0, "SIGNAL"))
        assert trade is None

    def test_profitable_close_positive_pnl(self):
        """Exit above entry price → positive PnL."""
        trader = _make_trader()
        self._setup_position(trader, entry_price=50_000.0, size=0.001, size_usd=50.0)
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit-order", "status": "closed",
            "average": 51_000.0, "fee": {"cost": 0.13},
        })
        trade = self._run(trader.close_long("BTC/USD", 51_000.0, "SIGNAL"))
        assert trade is not None
        assert trade.pnl > 0

    def test_loss_close_negative_pnl(self):
        """Exit below entry price → negative PnL."""
        trader = _make_trader()
        self._setup_position(trader, entry_price=50_000.0, size=0.001, size_usd=50.0)
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit-order", "status": "closed",
            "average": 49_000.0, "fee": {"cost": 0.13},
        })
        trade = self._run(trader.close_long("BTC/USD", 49_000.0, "STOP_LOSS"))
        assert trade is not None
        assert trade.pnl < 0

    def test_position_removed_after_close(self):
        trader = _make_trader()
        self._setup_position(trader)
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit", "status": "closed", "average": 51_000.0,
            "fee": {"cost": 0.13},
        })
        self._run(trader.close_long("BTC/USD", 51_000.0, "SIGNAL"))
        assert "BTC/USD" not in trader.positions

    def test_trade_appended_to_closed_trades(self):
        trader = _make_trader()
        self._setup_position(trader)
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit", "status": "closed", "average": 51_000.0,
            "fee": {"cost": 0.13},
        })
        self._run(trader.close_long("BTC/USD", 51_000.0, "TAKE_PROFIT"))
        assert len(trader.account.closed_trades) == 1

    def test_order_failure_returns_none_position_kept(self):
        """If the sell order throws, position stays open — don't wipe it."""
        trader = _make_trader()
        self._setup_position(trader)
        trader.exchange.create_order = AsyncMock(
            side_effect=RuntimeError("exchange down")
        )
        trade = self._run(trader.close_long("BTC/USD", 50_000.0, "SIGNAL"))
        assert trade is None
        assert "BTC/USD" in trader.positions

    def test_total_pnl_updated(self):
        trader = _make_trader()
        self._setup_position(trader, entry_price=50_000.0, size=0.002, size_usd=100.0)
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit", "status": "closed", "average": 51_000.0,
            "fee": {"cost": 0.26},
        })
        self._run(trader.close_long("BTC/USD", 51_000.0, "SIGNAL"))
        assert trader.account.total_pnl != 0.0

    def test_pnl_uses_actual_entry_fee_not_flat_rate_reestimate(self):
        """Regression: PnL must subtract the real fee charged at entry (pos.entry_fee),
        not FEE_RATE * size_usd recomputed from scratch — those diverge whenever the
        actual fill fee differs from the flat-rate guess (maker fills, fee tiers...).
        Exit at the entry price isolates the fee math: price PnL is 0, so total PnL
        must equal exactly -(entry_fee + exit_fee).
        """
        trader = _make_trader()
        entry_fee = 5.0  # deliberately far from FEE_RATE * 100.0 == 0.26
        self._setup_position(trader, entry_price=50_000.0, size=0.002, size_usd=100.0,
                             entry_fee=entry_fee)
        exit_fee = 0.13
        trader.exchange.create_order = AsyncMock(return_value={
            "id": "exit", "status": "closed", "average": 50_000.0,
            "fee": {"cost": exit_fee},
        })
        trade = self._run(trader.close_long("BTC/USD", 50_000.0, "SIGNAL"))
        assert trade.pnl == pytest.approx(-(entry_fee + exit_fee))
        assert trade.fees == pytest.approx(entry_fee + exit_fee)


# ── update_unrealized ─────────────────────────────────────────────────────────────

class TestUpdateUnrealized:
    def test_positive_move_positive_unrealized(self):
        trader = _make_trader()
        trader.positions["BTC/USD"] = _open_position("BTC/USD", entry_price=50_000.0, size=0.001)
        trader.update_unrealized({"BTC/USD": 51_000.0})
        assert trader.positions["BTC/USD"].unrealized_pnl == pytest.approx(1.0)

    def test_negative_move_negative_unrealized(self):
        trader = _make_trader()
        trader.positions["BTC/USD"] = _open_position("BTC/USD", entry_price=50_000.0, size=0.001)
        trader.update_unrealized({"BTC/USD": 49_000.0})
        assert trader.positions["BTC/USD"].unrealized_pnl == pytest.approx(-1.0)

    def test_multi_symbol_update(self):
        trader = _make_trader()
        trader.positions["BTC/USD"] = _open_position("BTC/USD", entry_price=50_000.0, size=0.001)
        trader.positions["ETH/USD"] = _open_position("ETH/USD", entry_price=3_000.0, size=0.01)
        trader.update_unrealized({"BTC/USD": 51_000.0, "ETH/USD": 2_900.0})
        assert trader.positions["BTC/USD"].unrealized_pnl == pytest.approx(1.0)
        assert trader.positions["ETH/USD"].unrealized_pnl == pytest.approx(-1.0)

    def test_ignores_symbol_not_in_positions(self):
        trader = _make_trader()
        trader.update_unrealized({"BTC/USD": 51_000.0})  # no positions — must not raise


# ── get_summary ───────────────────────────────────────────────────────────────────

class TestGetSummary:
    def test_empty_state_returns_initial_capital_as_equity(self):
        trader = _make_trader(initial_capital=1000.0)
        s = trader.get_summary()
        assert s["total_equity"] == pytest.approx(1000.0)
        assert s["total_pnl"] == 0.0
        assert s["open_positions"] == 0
        assert s["closed_trades"] == 0

    def test_equity_includes_unrealized_pnl(self):
        trader = _make_trader(initial_capital=1000.0)
        pos = _open_position("BTC/USD")
        pos.unrealized_pnl = 25.0
        trader.positions["BTC/USD"] = pos
        s = trader.get_summary()
        assert s["total_equity"] == pytest.approx(1025.0)

    def test_win_loss_counts_correct(self):
        trader = _make_trader(initial_capital=1000.0)
        now = datetime.now(timezone.utc)
        trader.account.closed_trades = [
            Trade(entry_time=now, exit_time=now, entry_price=100.0, exit_price=110.0,
                  size=1.0, side="sell", pnl=10.0, pnl_pct=10.0, fees=0.1),
            Trade(entry_time=now, exit_time=now, entry_price=100.0, exit_price=90.0,
                  size=1.0, side="sell", pnl=-10.0, pnl_pct=-10.0, fees=0.1),
            Trade(entry_time=now, exit_time=now, entry_price=100.0, exit_price=115.0,
                  size=1.0, side="sell", pnl=15.0, pnl_pct=15.0, fees=0.1),
        ]
        s = trader.get_summary()
        assert s["winning_trades"] == 2
        assert s["losing_trades"] == 1
        assert s["closed_trades"] == 3

    def test_pnl_pct_calculation(self):
        trader = _make_trader(initial_capital=200.0)
        trader.account.total_pnl = 20.0
        s = trader.get_summary()
        assert s["pnl_pct"] == pytest.approx(10.0)


# ── _inject_live_price ────────────────────────────────────────────────────────────

class TestInjectLivePrice:
    def _make_df(self, close=50_000.0, high=51_000.0, low=49_000.0) -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=3, freq="1min")
        df = pd.DataFrame({
            "open":   [close] * 3,
            "high":   [high] * 3,
            "low":    [low] * 3,
            "close":  [close] * 3,
            "volume": [1.0] * 3,
        }, index=idx)
        return df

    def test_close_is_updated(self):
        df = self._make_df(close=50_000.0)
        result = _inject_live_price(df, 52_000.0)
        assert float(result["close"].iloc[-1]) == pytest.approx(52_000.0)

    def test_high_raised_when_price_exceeds_current_high(self):
        df = self._make_df(close=50_000.0, high=51_000.0)
        result = _inject_live_price(df, 55_000.0)
        assert float(result["high"].iloc[-1]) == pytest.approx(55_000.0)

    def test_high_unchanged_when_price_below_current_high(self):
        df = self._make_df(close=50_000.0, high=51_000.0)
        result = _inject_live_price(df, 50_500.0)
        assert float(result["high"].iloc[-1]) == pytest.approx(51_000.0)

    def test_low_lowered_when_price_falls_below_current_low(self):
        df = self._make_df(close=50_000.0, low=49_000.0)
        result = _inject_live_price(df, 48_000.0)
        assert float(result["low"].iloc[-1]) == pytest.approx(48_000.0)

    def test_low_unchanged_when_price_above_current_low(self):
        df = self._make_df(close=50_000.0, low=49_000.0)
        result = _inject_live_price(df, 49_500.0)
        assert float(result["low"].iloc[-1]) == pytest.approx(49_000.0)

    def test_returns_copy_not_mutates_original(self):
        df = self._make_df(close=50_000.0)
        original_close = float(df["close"].iloc[-1])
        _inject_live_price(df, 55_000.0)
        assert float(df["close"].iloc[-1]) == pytest.approx(original_close)

    def test_earlier_rows_unchanged(self):
        df = self._make_df(close=50_000.0)
        result = _inject_live_price(df, 99_000.0)
        assert float(result["close"].iloc[0]) == pytest.approx(50_000.0)
        assert float(result["close"].iloc[1]) == pytest.approx(50_000.0)


# ── _quick_diagnose ───────────────────────────────────────────────────────────────

class TestQuickDiagnose:
    def _sig(self, ofi=None, confidence=80.0):
        return _make_signal(ofi=ofi, confidence=confidence)

    def test_positive_ofi_confirms_direction(self):
        issues, positives = _quick_diagnose(10.0, "SIGNAL", self._sig(ofi=0.20))
        assert any("OFI" in p for p in positives)
        assert not any("OFI" in i for i in issues)

    def test_negative_ofi_warns_against_direction(self):
        issues, positives = _quick_diagnose(-5.0, "STOP_LOSS", self._sig(ofi=-0.20))
        assert any("OFI" in i for i in issues)

    def test_no_ofi_neither_issue_nor_positive(self):
        issues, positives = _quick_diagnose(5.0, "SIGNAL", self._sig(ofi=None))
        assert not any("OFI" in x for x in issues + positives)

    def test_stop_loss_reason_adds_issue(self):
        issues, _ = _quick_diagnose(-10.0, "STOP_LOSS", self._sig())
        assert any("stop" in i.lower() or "Stopped" in i for i in issues)

    def test_take_profit_reason_adds_positive(self):
        _, positives = _quick_diagnose(10.0, "TAKE_PROFIT", self._sig())
        assert any("Target" in p or "reached" in p for p in positives)

    def test_high_confidence_adds_positive(self):
        _, positives = _quick_diagnose(10.0, "SIGNAL", self._sig(confidence=95.0))
        assert any("confidence" in p.lower() or "conviction" in p.lower() for p in positives)

    def test_low_confidence_adds_issue(self):
        issues, _ = _quick_diagnose(5.0, "SIGNAL", self._sig(confidence=65.0))
        assert any("confidence" in i.lower() for i in issues)


# ── CircuitBreakerOpen handling ───────────────────────────────────────────────────

class TestCircuitBreakerOpenHandling:
    """
    Verify that CircuitBreakerOpen raised by the exchange propagates correctly
    rather than being silently swallowed as a generic "order failed" error.

    Paper trading handles this properly (paper_trading.py imports and catches
    CircuitBreakerOpen in 5 places).  These tests guard the equivalent paths
    in live_trading.py.
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def test_open_long_propagates_circuit_breaker_open(self):
        """CircuitBreakerOpen from create_order must NOT be caught as a plain
        order failure — it must propagate so the main loop can sleep the correct
        cooldown, not just log 'Buy order FAILED'."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(
            side_effect=CircuitBreakerOpen("exchange down", remaining_seconds=30.0)
        )
        sig = _make_signal()
        with pytest.raises(CircuitBreakerOpen):
            self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))

    def test_open_long_propagated_circuit_breaker_carries_remaining_seconds(self):
        """The re-raised exception must retain remaining_seconds so the caller
        can sleep the right duration."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(
            side_effect=CircuitBreakerOpen("down", remaining_seconds=45.0)
        )
        sig = _make_signal()
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        assert exc_info.value.remaining_seconds == pytest.approx(45.0)

    def test_open_long_does_not_record_position_on_circuit_breaker(self):
        """No position must be recorded when the circuit trips — the order
        was never placed."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(
            side_effect=CircuitBreakerOpen("down", remaining_seconds=10.0)
        )
        sig = _make_signal()
        try:
            self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        except CircuitBreakerOpen:
            pass
        assert "BTC/USD" not in trader.positions

    def test_close_long_propagates_circuit_breaker_open(self):
        """CircuitBreakerOpen during a sell must propagate — position stays
        open and the caller decides how long to wait before retrying."""
        trader = _make_trader()
        trader.positions["BTC/USD"] = _open_position("BTC/USD")
        trader.exchange.create_order = AsyncMock(
            side_effect=CircuitBreakerOpen("exchange down", remaining_seconds=60.0)
        )
        with pytest.raises(CircuitBreakerOpen):
            self._run(trader.close_long("BTC/USD", 51_000.0, "SIGNAL"))

    def test_close_long_keeps_position_open_on_circuit_breaker(self):
        """If the sell order cannot be placed because the circuit is open,
        the position must NOT be removed — it is still live on the exchange."""
        trader = _make_trader()
        trader.positions["BTC/USD"] = _open_position("BTC/USD")
        trader.exchange.create_order = AsyncMock(
            side_effect=CircuitBreakerOpen("down", remaining_seconds=30.0)
        )
        try:
            self._run(trader.close_long("BTC/USD", 51_000.0, "TAKE_PROFIT"))
        except CircuitBreakerOpen:
            pass
        assert "BTC/USD" in trader.positions, "Position must remain open; sell was never executed"

    def test_regular_order_exception_still_returns_none(self):
        """Regression: plain order failures (network, rejection) must still
        return None — they must NOT be caught by the CircuitBreakerOpen handler."""
        trader = _make_trader()
        trader.exchange.get_balance = AsyncMock(return_value={"USD": {"free": 500.0}})
        trader.exchange.create_order = AsyncMock(side_effect=RuntimeError("rejected"))
        sig = _make_signal()
        result = self._run(trader.open_long("BTC/USD", 50_000.0, 50.0, sig))
        assert result is None  # swallowed as before — circuit breaker change is non-breaking


# ── run_live_trading_session: reconciliation-failure abort ────────────────────────

class TestRunLiveTradingSessionReconcileAbort:
    """A reconciliation failure at startup must abort before any trading
    begins, rather than proceeding with an unknown position state."""

    def test_aborts_without_trading_when_reconciliation_fails(self):
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(side_effect=RuntimeError("network error"))
        notifier = MagicMock()
        asyncio.run(run_live_trading_session(
            exchange=trader.exchange, trader=trader, symbols=trader.symbols,
            notifier=notifier,
        ))
        assert trader.running is False
        assert trader.positions == {}
        notifier.send_message.assert_called_once()
        assert "aborted" in notifier.send_message.call_args[0][0].lower()

    def test_does_not_raise_when_no_notifier_configured(self):
        """The abort path must not blow up just because no notifier is wired."""
        trader = _make_trader()
        trader.exchange.get_positions = AsyncMock(side_effect=RuntimeError("network error"))
        asyncio.run(run_live_trading_session(
            exchange=trader.exchange, trader=trader, symbols=trader.symbols,
            notifier=None,
        ))
        assert trader.running is False
