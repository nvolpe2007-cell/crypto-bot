"""
Unit tests for src/vpin_monitor.py

VPINMonitor computes per-symbol VPIN (Volume-Synchronized Probability of
Informed Trading) from a live trade stream. It gates entry decisions via the
_vpin_safe check in entry_checklist.py.

Covers:
  - Fresh/empty state: current() → None, is_toxic() → False, n_buckets() → 0
  - Warm-up window: no VPIN until 5 buckets have closed
  - Pure-buy flow: VPIN reaches 1.0 (maximum imbalance)
  - Balanced flow: VPIN reaches 0.0 (minimum imbalance)
  - Mixed flow: VPIN correctly reflects partial imbalance
  - Bucket splitting: single large trade spans multiple buckets
  - Multiple symbols: state is isolated per-symbol
  - Invalid input: zero qty, unknown side, missing symbol — all silently ignored
  - is_toxic boundary: below/at/above threshold, None-data → not toxic
  - n_buckets: grows as buckets close, capped at window (maxlen)
  - snapshot(): correct keys, values, 'toxic' flag
  - VPIN always in [0.0, 1.0]
  - Bucket sizes: BTC→0.5, ETH→10.0, SOL→150.0, unknown→1.0
"""

from __future__ import annotations

import pytest
from dataclasses import dataclass
from typing import Optional

from src.vpin_monitor import VPINMonitor, TOXIC_THRESHOLD, N_BUCKETS, _bucket_size_for


# ── helpers ───────────────────────────────────────────────────────────────────

@dataclass
class _Tick:
    """Minimal duck-typed TradeTick for tests."""
    symbol: str
    qty:    float
    side:   str   # "buy" or "sell"


def _push(monitor: VPINMonitor, symbol: str, qty: float, side: str) -> None:
    monitor.on_trade(_Tick(symbol=symbol, qty=qty, side=side))


def _fill_buckets(
    monitor: VPINMonitor,
    symbol: str,
    n: int,
    side: str = "buy",
    bucket_size: float = 1.0,
) -> None:
    """Close exactly `n` full buckets with the given side."""
    for _ in range(n):
        _push(monitor, symbol, bucket_size, side)


def _fill_balanced_buckets(
    monitor: VPINMonitor,
    symbol: str,
    n: int,
    bucket_size: float = 1.0,
) -> None:
    """Close `n` perfectly balanced buckets (50% buy, 50% sell each)."""
    half = bucket_size / 2.0
    for _ in range(n):
        _push(monitor, symbol, half, "buy")
        _push(monitor, symbol, half, "sell")


# ── bucket size lookup ────────────────────────────────────────────────────────

class TestBucketSizeFor:
    def test_btc(self):
        assert _bucket_size_for("BTC/USD") == pytest.approx(0.5)

    def test_eth(self):
        assert _bucket_size_for("ETH/USD") == pytest.approx(10.0)

    def test_sol(self):
        assert _bucket_size_for("SOL/USD") == pytest.approx(150.0)

    def test_unknown_symbol_falls_back_to_default(self):
        assert _bucket_size_for("XYZ/USD") == pytest.approx(1.0)

    def test_lowercase_base_normalised(self):
        # The helper splits on "/" and upper-cases the base before lookup.
        assert _bucket_size_for("btc/usd") == pytest.approx(0.5)


# ── initial / empty state ─────────────────────────────────────────────────────

class TestEmptyState:
    def test_current_returns_none_on_fresh_monitor(self):
        m = VPINMonitor()
        assert m.current("BTC/USD") is None

    def test_is_toxic_returns_false_on_fresh_monitor(self):
        m = VPINMonitor()
        assert m.is_toxic("BTC/USD") is False

    def test_n_buckets_zero_on_fresh_monitor(self):
        m = VPINMonitor()
        assert m.n_buckets("BTC/USD") == 0

    def test_snapshot_empty_on_fresh_monitor(self):
        m = VPINMonitor()
        assert m.snapshot() == {}

    def test_unknown_symbol_current_none(self):
        m = VPINMonitor()
        _fill_buckets(m, "ETH/USD", 10)
        assert m.current("BTC/USD") is None  # ETH buckets don't affect BTC


# ── warm-up window ────────────────────────────────────────────────────────────

class TestWarmup:
    """No VPIN until 5 closed buckets (see `if len(st.closed) >= 5:`)."""

    def test_no_vpin_after_0_closed_buckets(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 0.5, "buy")   # partially filling the first bucket
        assert m.current("XYZ/USD") is None

    def test_no_vpin_after_4_closed_buckets(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 4)
        assert m.current("XYZ/USD") is None

    def test_vpin_available_after_5_closed_buckets(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 5)
        assert m.current("XYZ/USD") is not None

    def test_vpin_available_after_more_than_5_closed_buckets(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 20)
        assert m.current("XYZ/USD") is not None


# ── pure buy flow (maximum imbalance) ─────────────────────────────────────────

class TestPureBuyFlow:
    def test_vpin_equals_one_for_all_buy(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10)
        assert m.current("XYZ/USD") == pytest.approx(1.0)

    def test_vpin_one_is_toxic(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10)
        assert m.is_toxic("XYZ/USD") is True

    def test_pure_sell_also_gives_vpin_one(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10, side="sell")
        assert m.current("XYZ/USD") == pytest.approx(1.0)


# ── balanced flow (minimum imbalance) ────────────────────────────────────────

class TestBalancedFlow:
    def test_vpin_zero_for_perfectly_balanced(self):
        m = VPINMonitor()
        _fill_balanced_buckets(m, "XYZ/USD", 10)
        assert m.current("XYZ/USD") == pytest.approx(0.0, abs=1e-9)

    def test_balanced_flow_is_not_toxic(self):
        m = VPINMonitor()
        _fill_balanced_buckets(m, "XYZ/USD", 10)
        assert m.is_toxic("XYZ/USD") is False


# ── mixed flow ────────────────────────────────────────────────────────────────

class TestMixedFlow:
    def test_partial_imbalance_gives_correct_vpin(self):
        # 6 all-buy buckets + 4 balanced buckets = 10 total
        # imbalance = 6×1.0 + 4×0.0 = 6.0
        # VPIN = 6.0 / (1.0 × 10) = 0.6
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 6, side="buy")
        _fill_balanced_buckets(m, "XYZ/USD", 4)
        vpin = m.current("XYZ/USD")
        assert vpin == pytest.approx(0.6, abs=0.01)

    def test_vpin_above_threshold_is_toxic(self):
        m = VPINMonitor(threshold=0.55)
        _fill_buckets(m, "XYZ/USD", 8, side="buy")
        _fill_balanced_buckets(m, "XYZ/USD", 2)
        # VPIN = 0.8 → should be toxic
        assert m.is_toxic("XYZ/USD") is True

    def test_vpin_below_threshold_is_not_toxic(self):
        m = VPINMonitor(threshold=0.55)
        _fill_buckets(m, "XYZ/USD", 2, side="buy")
        _fill_balanced_buckets(m, "XYZ/USD", 8)
        # VPIN = 0.2 → should NOT be toxic
        assert m.is_toxic("XYZ/USD") is False

    def test_vpin_exactly_at_threshold_is_not_toxic(self):
        # is_toxic uses strict >, so VPIN == threshold → not toxic
        m = VPINMonitor(threshold=0.5)
        _fill_buckets(m, "XYZ/USD", 5, side="buy")
        _fill_balanced_buckets(m, "XYZ/USD", 5)
        vpin = m.current("XYZ/USD")
        # VPIN should be 0.5
        assert vpin == pytest.approx(0.5, abs=0.01)
        assert m.is_toxic("XYZ/USD") is False   # 0.5 > 0.5 is False


# ── bucket splitting ───────────────────────────────────────────────────────────

class TestBucketSplitting:
    def test_large_trade_closes_multiple_buckets(self):
        # One trade of 3.0 with bucket_size=1.0 → 3 buckets closed
        m = VPINMonitor()
        _push(m, "XYZ/USD", 3.0, "buy")
        assert m.n_buckets("XYZ/USD") == 3

    def test_partial_remaining_does_not_close_extra_bucket(self):
        # 2.5 units fills 2 complete buckets and leaves 0.5 in the third
        m = VPINMonitor()
        _push(m, "XYZ/USD", 2.5, "buy")
        assert m.n_buckets("XYZ/USD") == 2

    def test_split_trade_counted_correctly_for_vpin(self):
        # One buy trade of 10.0 closes 10 all-buy buckets → VPIN = 1.0
        m = VPINMonitor()
        _push(m, "XYZ/USD", 10.0, "buy")
        assert m.current("XYZ/USD") == pytest.approx(1.0)

    def test_split_buy_then_split_sell_symmetric(self):
        # Fill 5 buckets via one big buy, then 5 via one big sell
        m = VPINMonitor()
        _push(m, "XYZ/USD", 5.0, "buy")
        _push(m, "XYZ/USD", 5.0, "sell")
        # First 5 closed buckets are pure buy (VPIN=1.0), next 5 are pure sell (VPIN=1.0)
        # → final VPIN still 1.0
        assert m.current("XYZ/USD") == pytest.approx(1.0)

    def test_exact_bucket_fill_across_buy_sell_alternating(self):
        # 10 trades of 0.5 buy + 0.5 sell = perfectly balanced 5 buckets
        m = VPINMonitor()
        for _ in range(5):
            _push(m, "XYZ/USD", 0.5, "buy")
            _push(m, "XYZ/USD", 0.5, "sell")
        assert m.current("XYZ/USD") == pytest.approx(0.0, abs=1e-9)


# ── multiple symbols ──────────────────────────────────────────────────────────

class TestMultiSymbol:
    def test_symbols_tracked_independently(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10, side="buy")     # VPIN=1.0
        _fill_balanced_buckets(m, "ABC/USD", 10)         # VPIN=0.0
        assert m.current("XYZ/USD") == pytest.approx(1.0)
        assert m.current("ABC/USD") == pytest.approx(0.0, abs=1e-9)

    def test_toxic_on_one_symbol_does_not_affect_another(self):
        m = VPINMonitor(threshold=0.55)
        _fill_buckets(m, "XYZ/USD", 10, side="buy")
        _fill_balanced_buckets(m, "ABC/USD", 10)
        assert m.is_toxic("XYZ/USD") is True
        assert m.is_toxic("ABC/USD") is False

    def test_n_buckets_per_symbol(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 7)
        _fill_buckets(m, "ABC/USD", 3)
        assert m.n_buckets("XYZ/USD") == 7
        assert m.n_buckets("ABC/USD") == 3


# ── invalid input handling ────────────────────────────────────────────────────

class TestInvalidInput:
    def test_tick_with_no_symbol_is_ignored(self):
        m = VPINMonitor()

        class _NoSym:
            qty  = 1.0
            side = "buy"

        m.on_trade(_NoSym())
        assert m.snapshot() == {}

    def test_zero_qty_is_ignored(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 0.0, "buy")
        assert m.n_buckets("XYZ/USD") == 0

    def test_negative_qty_is_ignored(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", -1.0, "buy")
        assert m.n_buckets("XYZ/USD") == 0

    def test_unknown_side_is_ignored(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 1.0, "unknown")
        assert m.n_buckets("XYZ/USD") == 0

    def test_empty_side_string_is_ignored(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 1.0, "")
        assert m.n_buckets("XYZ/USD") == 0

    def test_non_numeric_qty_is_ignored(self):
        m = VPINMonitor()

        class _BadQty:
            symbol = "XYZ/USD"
            qty    = "bad"
            side   = "buy"

        m.on_trade(_BadQty())
        assert m.n_buckets("XYZ/USD") == 0

    def test_valid_trades_accepted_after_invalid(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", -999.0, "buy")   # invalid
        _push(m, "XYZ/USD", 1.0, "unknown")  # invalid
        _fill_buckets(m, "XYZ/USD", 5)       # valid — should proceed normally
        assert m.current("XYZ/USD") is not None


# ── is_toxic boundary conditions ─────────────────────────────────────────────

class TestIsToxicBoundary:
    def test_none_data_not_toxic(self):
        m = VPINMonitor()
        # No trades → is_toxic must be False (warm-up: don't block entries)
        assert m.is_toxic("XYZ/USD") is False

    def test_below_threshold_not_toxic(self):
        m = VPINMonitor(threshold=TOXIC_THRESHOLD)
        # Balanced flow → VPIN = 0 << threshold
        _fill_balanced_buckets(m, "XYZ/USD", 10)
        assert m.is_toxic("XYZ/USD") is False

    def test_above_threshold_is_toxic(self):
        m = VPINMonitor(threshold=0.55)
        # All-buy flow → VPIN = 1.0 > 0.55
        _fill_buckets(m, "XYZ/USD", 10, side="buy")
        assert m.is_toxic("XYZ/USD") is True

    def test_custom_threshold_respected(self):
        m = VPINMonitor(threshold=0.9)
        # All-buy VPIN=1.0 still exceeds 0.9
        _fill_buckets(m, "XYZ/USD", 10, side="buy")
        assert m.is_toxic("XYZ/USD") is True

    def test_custom_high_threshold_not_toxic(self):
        m = VPINMonitor(threshold=0.99)
        # Partial imbalance VPIN≈0.6 does not exceed 0.99
        _fill_buckets(m, "XYZ/USD", 6, side="buy")
        _fill_balanced_buckets(m, "XYZ/USD", 4)
        assert m.is_toxic("XYZ/USD") is False


# ── n_buckets counter ─────────────────────────────────────────────────────────

class TestNBuckets:
    def test_increments_with_each_closed_bucket(self):
        m = VPINMonitor()
        for expected in range(1, 8):
            _push(m, "XYZ/USD", 1.0, "buy")
            assert m.n_buckets("XYZ/USD") == expected

    def test_partial_fill_does_not_increment(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 0.3, "buy")
        assert m.n_buckets("XYZ/USD") == 0
        _push(m, "XYZ/USD", 0.3, "sell")
        assert m.n_buckets("XYZ/USD") == 0   # 0.6 < 1.0

    def test_window_capped_at_max_window_size(self):
        # The closed-bucket deque uses maxlen=N_BUCKETS (the global, default 50).
        # The `window` constructor arg is stored but the deque ignores it.
        # Fill more than N_BUCKETS buckets and verify n_buckets saturates at N_BUCKETS.
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", N_BUCKETS + 10)
        assert m.n_buckets("XYZ/USD") == N_BUCKETS


# ── snapshot ──────────────────────────────────────────────────────────────────

class TestSnapshot:
    def test_empty_snapshot_on_no_trades(self):
        m = VPINMonitor()
        assert m.snapshot() == {}

    def test_snapshot_contains_tracked_symbols(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 5)
        snap = m.snapshot()
        assert "XYZ/USD" in snap

    def test_snapshot_has_required_keys(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10)
        entry = m.snapshot()["XYZ/USD"]
        assert "vpin"    in entry
        assert "buckets" in entry
        assert "trades"  in entry
        assert "toxic"   in entry

    def test_snapshot_vpin_matches_current(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 10)
        assert m.snapshot()["XYZ/USD"]["vpin"] == m.current("XYZ/USD")

    def test_snapshot_buckets_matches_n_buckets(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 7)
        assert m.snapshot()["XYZ/USD"]["buckets"] == m.n_buckets("XYZ/USD")

    def test_snapshot_toxic_flag_consistent(self):
        m = VPINMonitor(threshold=0.55)
        _fill_buckets(m, "XYZ/USD", 10, side="buy")
        snap = m.snapshot()["XYZ/USD"]
        assert snap["toxic"] == m.is_toxic("XYZ/USD")

    def test_snapshot_trade_count_increments(self):
        m = VPINMonitor()
        _push(m, "XYZ/USD", 0.1, "buy")
        _push(m, "XYZ/USD", 0.2, "sell")
        snap = m.snapshot()["XYZ/USD"]
        assert snap["trades"] == 2

    def test_snapshot_covers_all_tracked_symbols(self):
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", 5)
        _fill_buckets(m, "ABC/USD", 3)
        snap = m.snapshot()
        assert set(snap.keys()) == {"XYZ/USD", "ABC/USD"}


# ── VPIN always in [0, 1] ─────────────────────────────────────────────────────

class TestVPINRange:
    def test_vpin_never_exceeds_one(self):
        m = VPINMonitor()
        # All-buy is the maximum possible imbalance
        _fill_buckets(m, "XYZ/USD", 50, side="buy")
        v = m.current("XYZ/USD")
        assert v is not None
        assert v <= 1.0 + 1e-9

    def test_vpin_never_below_zero(self):
        m = VPINMonitor()
        _fill_balanced_buckets(m, "XYZ/USD", 50)
        v = m.current("XYZ/USD")
        assert v is not None
        assert v >= 0.0 - 1e-9

    def test_alternating_sides_vpin_in_range(self):
        m = VPINMonitor()
        # Alternating full buy / full sell buckets
        for _ in range(20):
            _push(m, "XYZ/USD", 1.0, "buy")
            _push(m, "XYZ/USD", 1.0, "sell")
        v = m.current("XYZ/USD")
        assert v is not None
        assert 0.0 <= v <= 1.0 + 1e-9


# ── window / rolling behaviour ────────────────────────────────────────────────

class TestWindowRolling:
    def test_old_buckets_drop_out_of_window(self):
        # The closed deque has maxlen=N_BUCKETS; once full, oldest entries are evicted.
        # Fill N_BUCKETS all-buy buckets (VPIN=1.0), then flood with N_BUCKETS balanced
        # buckets so all the all-buy entries roll off and VPIN drops to 0.
        m = VPINMonitor()
        _fill_buckets(m, "XYZ/USD", N_BUCKETS)
        assert m.current("XYZ/USD") == pytest.approx(1.0)
        # Now replace every slot in the deque with balanced buckets
        _fill_balanced_buckets(m, "XYZ/USD", N_BUCKETS)
        assert m.current("XYZ/USD") == pytest.approx(0.0, abs=1e-9)

    def test_vpin_updates_as_new_buckets_arrive(self):
        m = VPINMonitor()
        _fill_balanced_buckets(m, "XYZ/USD", 10)
        v1 = m.current("XYZ/USD")
        _fill_buckets(m, "XYZ/USD", 10, side="buy")
        v2 = m.current("XYZ/USD")
        assert v2 > v1  # VPIN should increase as one-sided buckets dominate
