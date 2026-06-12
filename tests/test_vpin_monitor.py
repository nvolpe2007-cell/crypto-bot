"""
Unit tests for src/vpin_monitor.py — the VPIN order-flow toxicity monitor that
feeds entry_checklist.py's `vpin_safe` hard veto in the live decision pipeline.
"""

from dataclasses import dataclass

import pytest

from src.vpin_monitor import (
    VPINMonitor,
    _bucket_size_for,
    _DEFAULT_BUCKETS,
    _FALLBACK_BUCKET,
)


@dataclass
class _Tick:
    """Minimal duck-typed stand-in for kraken_ws.TradeTick."""
    symbol: str
    qty: float
    side: str


# ── _bucket_size_for ───────────────────────────────────────────────────────────

class TestBucketSizeFor:
    def test_btc_uses_configured_bucket(self):
        assert _bucket_size_for("BTC/USD") == _DEFAULT_BUCKETS["BTC"]

    def test_eth_uses_configured_bucket(self):
        assert _bucket_size_for("ETH/USD") == _DEFAULT_BUCKETS["ETH"]

    def test_sol_uses_configured_bucket(self):
        assert _bucket_size_for("SOL/USD") == _DEFAULT_BUCKETS["SOL"]

    def test_unknown_symbol_uses_fallback(self):
        assert _bucket_size_for("XRP/USD") == _FALLBACK_BUCKET

    def test_is_case_insensitive_on_base(self):
        assert _bucket_size_for("btc/usd") == _DEFAULT_BUCKETS["BTC"]


# ── on_trade ingestion guards ───────────────────────────────────────────────────

class TestOnTradeGuards:
    def test_ignores_tick_with_no_symbol(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="", qty=1.0, side="buy"))
        assert vpin.current("") is None
        assert vpin._state == {}

    def test_ignores_zero_qty(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.0, side="buy"))
        assert vpin.n_buckets("XRP/USD") == 0
        assert "XRP/USD" not in vpin._state

    def test_ignores_negative_qty(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=-1.0, side="buy"))
        assert "XRP/USD" not in vpin._state

    def test_ignores_invalid_side(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=1.0, side="hold"))
        assert "XRP/USD" not in vpin._state

    def test_side_is_case_insensitive(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.5, side="BUY"))
        st = vpin._state["XRP/USD"]
        assert st.buy_acc == pytest.approx(0.5)

    def test_ignores_tick_with_unconvertible_qty(self):
        vpin = VPINMonitor()
        bad = _Tick(symbol="XRP/USD", qty="not-a-number", side="buy")
        vpin.on_trade(bad)
        assert "XRP/USD" not in vpin._state

    def test_ignores_object_missing_attributes(self):
        vpin = VPINMonitor()
        # A bare object has none of .symbol/.qty/.side
        vpin.on_trade(object())
        assert vpin._state == {}


# ── bucket accumulation / closing ───────────────────────────────────────────────

class TestBucketAccumulation:
    def test_partial_fill_does_not_close_bucket(self):
        vpin = VPINMonitor()
        # XRP/USD falls back to bucket_volume = 1.0
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.4, side="buy"))
        st = vpin._state["XRP/USD"]
        assert st.buy_acc == pytest.approx(0.4)
        assert st.sell_acc == 0.0
        assert len(st.closed) == 0

    def test_exact_fill_closes_bucket(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.6, side="buy"))
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.4, side="sell"))
        st = vpin._state["XRP/USD"]
        assert len(st.closed) == 1
        assert st.closed[0] == pytest.approx((0.6, 0.4))
        # accumulators reset for the next bucket
        assert st.buy_acc == 0.0
        assert st.sell_acc == 0.0

    def test_single_trade_spans_multiple_buckets(self):
        vpin = VPINMonitor()
        # bucket_volume = 1.0 for XRP; a 2.5-unit buy spans 2 full buckets
        # plus a 0.5 remainder left open.
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=2.5, side="buy"))
        st = vpin._state["XRP/USD"]
        assert len(st.closed) == 2
        assert st.closed[0] == pytest.approx((1.0, 0.0))
        assert st.closed[1] == pytest.approx((1.0, 0.0))
        assert st.buy_acc == pytest.approx(0.5)
        assert st.sell_acc == 0.0

    def test_n_trades_counter_increments(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.1, side="buy"))
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.1, side="sell"))
        assert vpin._state["XRP/USD"].n_trades == 2

    def test_symbols_are_tracked_independently(self):
        vpin = VPINMonitor()
        vpin.on_trade(_Tick(symbol="XRP/USD", qty=0.3, side="buy"))
        vpin.on_trade(_Tick(symbol="ADA/USD", qty=0.7, side="sell"))
        assert vpin._state["XRP/USD"].buy_acc == pytest.approx(0.3)
        assert vpin._state["ADA/USD"].sell_acc == pytest.approx(0.7)


# ── VPIN computation & warm-up ──────────────────────────────────────────────────

def _feed_buckets(vpin: VPINMonitor, symbol: str, buckets):
    """Feed whole buckets of (buy_qty, sell_qty) as separate ticks.

    Each bucket uses the symbol's configured bucket_volume, so callers must
    pass quantities that sum to exactly that per bucket.
    """
    for buy_qty, sell_qty in buckets:
        if buy_qty:
            vpin.on_trade(_Tick(symbol=symbol, qty=buy_qty, side="buy"))
        if sell_qty:
            vpin.on_trade(_Tick(symbol=symbol, qty=sell_qty, side="sell"))


class TestVpinComputation:
    def test_current_is_none_before_five_buckets(self):
        vpin = VPINMonitor()
        # 4 closed buckets — below the 5-bucket warm-up
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 4)
        assert vpin.n_buckets("XRP/USD") == 4
        assert vpin.current("XRP/USD") is None

    def test_current_unknown_symbol_is_none(self):
        vpin = VPINMonitor()
        assert vpin.current("DOGE/USD") is None

    def test_balanced_flow_gives_vpin_near_zero(self):
        vpin = VPINMonitor()
        # 5 perfectly balanced buckets (0.5 buy / 0.5 sell, bucket_volume=1.0)
        _feed_buckets(vpin, "XRP/USD", [(0.5, 0.5)] * 5)
        assert vpin.current("XRP/USD") == pytest.approx(0.0)

    def test_one_sided_flow_gives_vpin_near_one(self):
        vpin = VPINMonitor()
        # 5 fully one-sided buckets (all buy)
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)
        assert vpin.current("XRP/USD") == pytest.approx(1.0)

    def test_mixed_flow_gives_intermediate_vpin(self):
        vpin = VPINMonitor()
        # 5 buckets: 3 balanced (imbalance 0), 2 fully one-sided (imbalance 1.0 each)
        buckets = [(0.5, 0.5)] * 3 + [(1.0, 0.0)] * 2
        _feed_buckets(vpin, "XRP/USD", buckets)
        # imbalance sum = 0+0+0+1+1 = 2 ; total_vol = 1.0 * 5 = 5
        assert vpin.current("XRP/USD") == pytest.approx(2.0 / 5.0)

    def test_vpin_recomputed_on_each_new_closed_bucket(self):
        vpin = VPINMonitor()
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)
        first = vpin.current("XRP/USD")
        assert first == pytest.approx(1.0)
        # add one balanced bucket -> average should drop
        _feed_buckets(vpin, "XRP/USD", [(0.5, 0.5)])
        second = vpin.current("XRP/USD")
        # 5 one-sided buckets (imbalance 1.0 each) + 1 balanced (imbalance 0)
        assert second == pytest.approx((5 * 1.0 + 0.0) / 6.0)
        assert second < first


# ── rolling window (maxlen) ──────────────────────────────────────────────────────

class TestRollingWindow:
    def test_custom_window_limits_bucket_history(self):
        vpin = VPINMonitor(window=5)
        st = None
        # Feed 5 one-sided buckets first (vpin -> 1.0)
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)
        assert vpin.current("XRP/USD") == pytest.approx(1.0)
        assert vpin.n_buckets("XRP/USD") == 5

        # Now feed 5 balanced buckets — they should evict the toxic ones
        # entirely given window=5, dropping vpin back to 0.
        _feed_buckets(vpin, "XRP/USD", [(0.5, 0.5)] * 5)
        assert vpin.n_buckets("XRP/USD") == 5
        assert vpin.current("XRP/USD") == pytest.approx(0.0)

    def test_default_window_matches_module_constant(self):
        from src.vpin_monitor import N_BUCKETS
        vpin = VPINMonitor()
        st_window = vpin.window
        assert st_window == N_BUCKETS
        # Feed N_BUCKETS+5 one-sided buckets; closed deque should cap at N_BUCKETS
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * (N_BUCKETS + 5))
        assert vpin.n_buckets("XRP/USD") == N_BUCKETS


# ── is_toxic ─────────────────────────────────────────────────────────────────────

class TestIsToxic:
    def test_false_when_no_data(self):
        vpin = VPINMonitor()
        assert vpin.is_toxic("XRP/USD") is False

    def test_false_below_threshold(self):
        vpin = VPINMonitor(threshold=0.55)
        _feed_buckets(vpin, "XRP/USD", [(0.5, 0.5)] * 5)  # vpin = 0.0
        assert vpin.is_toxic("XRP/USD") is False

    def test_true_above_threshold(self):
        vpin = VPINMonitor(threshold=0.55)
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)  # vpin = 1.0
        assert vpin.is_toxic("XRP/USD") is True

    def test_false_at_exactly_threshold(self):
        vpin = VPINMonitor(threshold=0.5)
        # 5 buckets: 5*0.5/5 = 0.5 exactly -> not > threshold
        buckets = [(0.75, 0.25)] * 5  # imbalance 0.5 each -> mean 0.5
        _feed_buckets(vpin, "XRP/USD", buckets)
        assert vpin.current("XRP/USD") == pytest.approx(0.5)
        assert vpin.is_toxic("XRP/USD") is False

    def test_custom_threshold_respected(self):
        vpin = VPINMonitor(threshold=0.9)
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)  # vpin = 1.0
        assert vpin.is_toxic("XRP/USD") is True

        vpin2 = VPINMonitor(threshold=0.95)
        _feed_buckets(vpin2, "XRP/USD", [(0.9, 0.1)] * 5)  # vpin = 0.8
        assert vpin2.is_toxic("XRP/USD") is False


# ── n_buckets ────────────────────────────────────────────────────────────────────

class TestNBuckets:
    def test_zero_for_unknown_symbol(self):
        vpin = VPINMonitor()
        assert vpin.n_buckets("XRP/USD") == 0

    def test_increments_as_buckets_close(self):
        vpin = VPINMonitor()
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 3)
        assert vpin.n_buckets("XRP/USD") == 3


# ── snapshot ─────────────────────────────────────────────────────────────────────

class TestSnapshot:
    def test_empty_monitor_returns_empty_snapshot(self):
        vpin = VPINMonitor()
        assert vpin.snapshot() == {}

    def test_snapshot_reports_per_symbol_state(self):
        vpin = VPINMonitor(threshold=0.55)
        _feed_buckets(vpin, "XRP/USD", [(1.0, 0.0)] * 5)   # toxic
        _feed_buckets(vpin, "ADA/USD", [(0.5, 0.5)] * 3)   # warm-up, balanced

        snap = vpin.snapshot()
        assert set(snap.keys()) == {"XRP/USD", "ADA/USD"}

        xrp = snap["XRP/USD"]
        assert xrp["vpin"] == pytest.approx(1.0)
        assert xrp["buckets"] == 5
        assert xrp["trades"] == 5
        assert xrp["toxic"] is True

        ada = snap["ADA/USD"]
        assert ada["vpin"] is None       # still warming up
        assert ada["buckets"] == 3
        assert ada["trades"] == 6        # buy + sell tick per bucket
        assert ada["toxic"] is False
