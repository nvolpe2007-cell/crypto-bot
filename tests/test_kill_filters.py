"""Unit tests for src/kill_filters.py

Covers:
- _compute_spread: normal book, empty/crossed book, best-bid/ask selection
- _compute_top5_depth: full 5 levels, fewer levels, empty, >5 levels, missing size
- _median: odd/even lists, single element, empty, unsorted input
- _extract_funding_rate: BTC/ETH/SOL symbol mapping, percentage-to-decimal
  conversion, missing symbol, negative rate, non-dict entries, missing key
- KillFilterState: rolling spread/depth history management, per-symbol isolation
- KillFilterState.check(): all 8 kill filters individually + all-pass baseline
- check_kill_filters(): stateless convenience wrapper for all 8 filters
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from src.kill_filters import (
    KillFilterState,
    check_kill_filters,
    _compute_spread,
    _compute_top5_depth,
    _median,
    _extract_funding_rate,
    _FUNDING_EXTREME_THRESH,
    _WS_STALE_SECONDS,
    _DAILY_LOSS_THRESHOLD,
)


# ── constants ─────────────────────────────────────────────────────

_NOW = 1_700_000_000.0                                                # fixed unix ts
_MONDAY_NOON = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)   # weekday 0
_SATURDAY    = datetime(2024, 1, 6, 12, 0, 0, tzinfo=timezone.utc)   # weekday 5
_SUNDAY      = datetime(2024, 1, 7, 12, 0, 0, tzinfo=timezone.utc)   # weekday 6
_DEAD_HOUR   = datetime(2024, 1, 1,  1, 30, 0, tzinfo=timezone.utc)  # UTC 01:30 dead window
_FRIDAY      = datetime(2024, 1, 5, 12, 0, 0, tzinfo=timezone.utc)   # weekday 4


# ── helpers ───────────────────────────────────────────────────────

def _make_book(n_levels=5, mid=50_000.0, half_spread=1.0, depth_per_level=1.0):
    """Return (bids, asks) where spread = 2*half_spread, top-5 depth = 10*depth_per_level."""
    bids = [[mid - half_spread - i, depth_per_level] for i in range(n_levels)]
    asks = [[mid + half_spread + i, depth_per_level] for i in range(n_levels)]
    return bids, asks


def _primed_state(symbol="BTC/USD", n=10):
    """Return a KillFilterState pre-populated with n book snapshots (spread=2, depth=10)."""
    ks = KillFilterState()
    bids, asks = _make_book()
    for _ in range(n):
        ks.update_book_history(symbol, bids, asks)
    return ks


def _normal_kwargs(last_price_time=None):
    """Return KillFilterState.check() kwargs that pass all 8 filters."""
    bids, asks = _make_book()
    return dict(
        symbol="BTC/USD",
        bids=bids,
        asks=asks,
        last_price_time=last_price_time if last_price_time is not None else _NOW - 1.0,
        candle_volume=1.0,
        volume_sma20=10.0,
        funding_opportunities=[],
        daily_pnl_pct=0.0,
    )


# ── _compute_spread ─────────────────────────────────────────────────

class TestComputeSpread:
    def test_normal_book(self):
        assert _compute_spread([[100.0, 1.0]], [[101.0, 1.0]]) == pytest.approx(1.0)

    def test_wide_spread(self):
        assert _compute_spread([[99.0, 1.0]], [[105.0, 1.0]]) == pytest.approx(6.0)

    def test_uses_best_bid_and_ask_only(self):
        bids = [[100.0, 1.0], [99.0, 1.0], [98.0, 1.0]]
        asks = [[101.0, 1.0], [102.0, 1.0], [103.0, 1.0]]
        assert _compute_spread(bids, asks) == pytest.approx(1.0)

    def test_empty_bids_returns_zero(self):
        assert _compute_spread([], [[101.0, 1.0]]) == 0.0

    def test_empty_asks_returns_zero(self):
        assert _compute_spread([[100.0, 1.0]], []) == 0.0

    def test_both_empty_returns_zero(self):
        assert _compute_spread([], []) == 0.0

    def test_ask_equals_bid_returns_zero(self):
        assert _compute_spread([[100.0, 1.0]], [[100.0, 1.0]]) == 0.0

    def test_crossed_book_returns_zero(self):
        assert _compute_spread([[101.0, 1.0]], [[100.0, 1.0]]) == 0.0

    def test_row_with_price_only_no_size(self):
        assert _compute_spread([[100.0]], [[101.0]]) == pytest.approx(1.0)


# ── _compute_top5_depth ───────────────────────────────────────────────

class TestComputeTop5Depth:
    def test_full_5_levels_each_side(self):
        bids = [[100.0 - i, 2.0] for i in range(5)]
        asks = [[101.0 + i, 3.0] for i in range(5)]
        assert _compute_top5_depth(bids, asks) == pytest.approx(25.0)

    def test_only_2_levels(self):
        bids = [[100.0, 1.0], [99.0, 1.0]]
        asks = [[101.0, 1.0], [102.0, 1.0]]
        assert _compute_top5_depth(bids, asks) == pytest.approx(4.0)

    def test_both_empty(self):
        assert _compute_top5_depth([], []) == 0.0

    def test_empty_bids(self):
        assert _compute_top5_depth([], [[101.0, 5.0]]) == pytest.approx(5.0)

    def test_empty_asks(self):
        assert _compute_top5_depth([[100.0, 3.0]], []) == pytest.approx(3.0)

    def test_more_than_5_levels_capped_at_top5(self):
        bids = [[100.0 - i, 1.0] for i in range(10)]
        asks = [[101.0 + i, 1.0] for i in range(10)]
        assert _compute_top5_depth(bids, asks) == pytest.approx(10.0)

    def test_rows_without_size_not_counted(self):
        assert _compute_top5_depth([[100.0]], [[101.0]]) == pytest.approx(0.0)


# ── _median ───────────────────────────────────────────────────────────

class TestMedian:
    def test_odd_length_list(self):
        assert _median([1.0, 3.0, 2.0]) == pytest.approx(2.0)

    def test_even_length_list(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)

    def test_single_element(self):
        assert _median([7.5]) == pytest.approx(7.5)

    def test_empty_returns_zero(self):
        assert _median([]) == 0.0

    def test_presorted(self):
        assert _median([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(3.0)

    def test_unsorted_input(self):
        assert _median([5.0, 1.0, 3.0]) == pytest.approx(3.0)

    def test_two_elements(self):
        assert _median([2.0, 4.0]) == pytest.approx(3.0)


# ── _extract_funding_rate ───────────────────────────────────────────────

class TestExtractFundingRate:
    def test_btc_usd_maps_to_btcusdt(self):
        opps = [{"symbol": "BTCUSDT", "rate_8h": 0.1}]
        assert _extract_funding_rate("BTC/USD", opps) == pytest.approx(0.001)

    def test_eth_usd_maps_to_ethusdt(self):
        opps = [{"symbol": "ETHUSDT", "rate_8h": 0.05}]
        assert _extract_funding_rate("ETH/USD", opps) == pytest.approx(0.0005)

    def test_sol_usd_maps_to_solusdt(self):
        opps = [{"symbol": "SOLUSDT", "rate_8h": 0.2}]
        assert _extract_funding_rate("SOL/USD", opps) == pytest.approx(0.002)

    def test_symbol_not_found_returns_none(self):
        opps = [{"symbol": "ETHUSDT", "rate_8h": 0.1}]
        assert _extract_funding_rate("BTC/USD", opps) is None

    def test_empty_list_returns_none(self):
        assert _extract_funding_rate("BTC/USD", []) is None

    def test_negative_funding_rate(self):
        opps = [{"symbol": "BTCUSDT", "rate_8h": -0.1}]
        assert _extract_funding_rate("BTC/USD", opps) == pytest.approx(-0.001)

    def test_percentage_to_decimal_conversion(self):
        # rate_8h=1.0 means 1% → 0.01 as decimal fraction
        opps = [{"symbol": "BTCUSDT", "rate_8h": 1.0}]
        assert _extract_funding_rate("BTC/USD", opps) == pytest.approx(0.01)

    def test_non_dict_entries_skipped(self):
        opps = ["not_a_dict", 42, {"symbol": "BTCUSDT", "rate_8h": 0.1}]
        assert _extract_funding_rate("BTC/USD", opps) == pytest.approx(0.001)

    def test_missing_rate_8h_key_returns_none(self):
        opps = [{"symbol": "BTCUSDT"}]
        assert _extract_funding_rate("BTC/USD", opps) is None

    def test_rate_at_threshold_equals_threshold(self):
        # rate_8h=0.1 % → 0.001 exactly equals _FUNDING_EXTREME_THRESH
        opps = [{"symbol": "BTCUSDT", "rate_8h": 0.1}]
        assert _extract_funding_rate("BTC/USD", opps) == pytest.approx(_FUNDING_EXTREME_THRESH)


# ── KillFilterState — rolling history ────────────────────────────────────────

class TestKillFilterStateHistory:
    def test_fresh_state_spread_median_is_zero(self):
        assert KillFilterState().get_spread_median("BTC/USD") == 0.0

    def test_fresh_state_depth_avg_is_zero(self):
        assert KillFilterState().get_depth_1h_avg("BTC/USD") == 0.0

    def test_fewer_than_5_entries_spread_median_stays_zero(self):
        ks = KillFilterState()
        bids, asks = _make_book()
        for _ in range(4):
            ks.update_book_history("BTC/USD", bids, asks)
        assert ks.get_spread_median("BTC/USD") == 0.0

    def test_five_entries_spread_median_nonzero(self):
        ks = KillFilterState()
        bids, asks = _make_book()  # spread = 2.0
        for _ in range(5):
            ks.update_book_history("BTC/USD", bids, asks)
        assert ks.get_spread_median("BTC/USD") == pytest.approx(2.0)

    def test_depth_avg_after_one_entry(self):
        ks = KillFilterState()
        bids, asks = _make_book()  # top-5 depth = 10.0 (5 bids + 5 asks at 1.0 each)
        ks.update_book_history("BTC/USD", bids, asks)
        assert ks.get_depth_1h_avg("BTC/USD") == pytest.approx(10.0)

    def test_zero_spread_not_added_to_history(self):
        ks = KillFilterState()
        for _ in range(10):
            ks.update_book_history("BTC/USD", [], [])  # empty book → spread=0
        assert ks.get_spread_median("BTC/USD") == 0.0

    def test_symbols_have_isolated_histories(self):
        ks = KillFilterState()
        bids_btc, asks_btc = _make_book(5, 50_000.0, 1.0)   # spread = 2.0
        bids_eth, asks_eth = _make_book(5,  3_000.0, 0.5)   # spread = 1.0
        for _ in range(5):
            ks.update_book_history("BTC/USD", bids_btc, asks_btc)
            ks.update_book_history("ETH/USD", bids_eth, asks_eth)
        assert ks.get_spread_median("BTC/USD") == pytest.approx(2.0)
        assert ks.get_spread_median("ETH/USD") == pytest.approx(1.0)


# ── KillFilterState.check() — all 8 filters ────────────────────────────

class TestKillFilterStateCheck:
    """
    Each test patches time.time and datetime.now to fixed values so filter
    results are deterministic, then asserts (is_killed, reason).
    """

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_all_filters_pass(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = _primed_state()
        is_killed, reason = ks.check(**_normal_kwargs())
        assert not is_killed
        assert reason == ""

    # ── Filter 1: Funding extreme ─────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter1_extreme_positive_funding_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["funding_opportunities"] = [{"symbol": "BTCUSDT", "rate_8h": 0.2}]  # 0.002 > 0.001
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "FUNDING_EXTREME" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter1_extreme_negative_funding_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["funding_opportunities"] = [{"symbol": "BTCUSDT", "rate_8h": -0.2}]
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "FUNDING_EXTREME" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter1_normal_funding_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["funding_opportunities"] = [{"symbol": "BTCUSDT", "rate_8h": 0.05}]  # 0.0005 < 0.001
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter1_no_funding_data_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["funding_opportunities"] = []
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    # ── Filter 2: Low liquidity window ───────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter2_utc_01_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _DEAD_HOUR  # 01:30 UTC

        is_killed, reason = _primed_state().check(**_normal_kwargs())
        assert is_killed
        assert "LOW_LIQUIDITY" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter2_utc_02_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = datetime(2024, 1, 1, 2, 0, 0, tzinfo=timezone.utc)

        is_killed, reason = _primed_state().check(**_normal_kwargs())
        assert is_killed
        assert "LOW_LIQUIDITY" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter2_utc_00_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = datetime(2024, 1, 1, 0, 30, 0, tzinfo=timezone.utc)

        is_killed, _ = _primed_state().check(**_normal_kwargs())
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter2_utc_03_exclusive_end_passes(self, mock_time, mock_dt):
        # Hour 3 is the exclusive boundary — should NOT trigger
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = datetime(2024, 1, 1, 3, 0, 0, tzinfo=timezone.utc)

        is_killed, _ = _primed_state().check(**_normal_kwargs())
        assert not is_killed

    # ── Filter 3: Spread too wide ─────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter3_wide_spread_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = _primed_state()  # median spread = 2.0
        kwargs = _normal_kwargs()
        # spread = 8.0 → 4× median, exceeds 3× threshold
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 4.0)
        is_killed, reason = ks.check(**kwargs)
        assert is_killed
        assert "SPREAD_TOO_WIDE" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter3_normal_spread_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = _primed_state()
        is_killed, _ = ks.check(**_normal_kwargs())
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter3_no_history_skips_check(self, mock_time, mock_dt):
        # With no history, median = 0 → check is skipped → passes
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = KillFilterState()  # fresh, no history
        kwargs = _normal_kwargs()
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 100.0)  # huge spread
        is_killed, _ = ks.check(**kwargs)
        assert not is_killed

    # ── Filter 4: Thin book ────────────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter4_thin_book_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = _primed_state()  # 1h avg depth ≈ 10.0
        kwargs = _normal_kwargs()
        # depth = 1.0 → 10% of avg, below 20% threshold
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 1.0, 0.1)
        is_killed, reason = ks.check(**kwargs)
        assert is_killed
        assert "THIN_BOOK" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter4_normal_depth_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        ks = _primed_state()
        is_killed, _ = ks.check(**_normal_kwargs())
        assert not is_killed

    # ── Filter 5: WebSocket stale ───────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter5_stale_price_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["last_price_time"] = _NOW - (_WS_STALE_SECONDS + 1.0)  # 6s ago
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "WS_STALE" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter5_fresh_price_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["last_price_time"] = _NOW - 1.0  # 1s ago
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter5_zero_timestamp_is_stale(self, mock_time, mock_dt):
        # last_price_time=0 triggers the 999s fallback → always stale
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["last_price_time"] = 0
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "WS_STALE" in reason

    # ── Filter 6: Whale print ──────────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter6_whale_volume_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["candle_volume"] = 200.0   # 20× SMA20 → exceeds 10× threshold
        kwargs["volume_sma20"]  = 10.0
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "WHALE_PRINT" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter6_normal_volume_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["candle_volume"] = 5.0    # 0.5× SMA20
        kwargs["volume_sma20"]  = 10.0
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter6_zero_sma20_skips_check(self, mock_time, mock_dt):
        # Guard: volume_sma20=0 should not trigger (division guard)
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["candle_volume"] = 99_999.0
        kwargs["volume_sma20"]  = 0.0
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    # ── Filter 7: Daily loss ──────────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter7_daily_loss_exceeded_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["daily_pnl_pct"] = -0.025   # -2.5% < -2% threshold
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "DAILY_LOSS" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter7_pnl_exactly_at_threshold_passes(self, mock_time, mock_dt):
        # threshold is strict < so equal value should NOT kill
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["daily_pnl_pct"] = _DAILY_LOSS_THRESHOLD   # exactly at threshold
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter7_positive_pnl_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = _normal_kwargs()
        kwargs["daily_pnl_pct"] = 0.05
        is_killed, _ = _primed_state().check(**kwargs)
        assert not is_killed

    # ── Filter 8: Weekend ─────────────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter8_saturday_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _SATURDAY

        is_killed, reason = _primed_state().check(**_normal_kwargs())
        assert is_killed
        assert "WEEKEND" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter8_sunday_kills(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _SUNDAY

        is_killed, reason = _primed_state().check(**_normal_kwargs())
        assert is_killed
        assert "WEEKEND" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter8_friday_passes(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _FRIDAY

        is_killed, _ = _primed_state().check(**_normal_kwargs())
        assert not is_killed

    # ── Priority: earlier filters win ─────────────────────────────────────────

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter1_blocks_before_filter8_weekend(self, mock_time, mock_dt):
        # Both filter 1 and filter 8 would trigger; filter 1 has priority
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _SATURDAY

        kwargs = _normal_kwargs()
        kwargs["funding_opportunities"] = [{"symbol": "BTCUSDT", "rate_8h": 0.5}]
        is_killed, reason = _primed_state().check(**kwargs)
        assert is_killed
        assert "FUNDING_EXTREME" in reason   # filter 1 wins

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_filter2_blocks_before_filter3_spread(self, mock_time, mock_dt):
        # Dead hour (filter 2) should fire before spread check (filter 3)
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _DEAD_HOUR

        ks = _primed_state()
        kwargs = _normal_kwargs()
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 4.0)  # wide spread
        is_killed, reason = ks.check(**kwargs)
        assert is_killed
        assert "LOW_LIQUIDITY" in reason   # filter 2 wins


# ── check_kill_filters (stateless convenience wrapper) ────────────────────

class TestCheckKillFilters:
    """Stateless function takes spread_median_24h and depth_1h_avg directly."""

    def _base_kwargs(self):
        bids, asks = _make_book()
        return dict(
            symbol="BTC/USD",
            bids=bids,
            asks=asks,
            spread_median_24h=2.0,
            depth_1h_avg=10.0,
            last_price_time=_NOW - 1.0,
            candle_volume=1.0,
            volume_sma20=10.0,
            funding_opportunities=[],
            daily_pnl_pct=0.0,
        )

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_all_pass(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        is_killed, reason = check_kill_filters(**self._base_kwargs())
        assert not is_killed
        assert reason == ""

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_funding_extreme(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["funding_opportunities"] = [{"symbol": "BTCUSDT", "rate_8h": 0.5}]
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "FUNDING_EXTREME" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_dead_hour(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _DEAD_HOUR

        is_killed, reason = check_kill_filters(**self._base_kwargs())
        assert is_killed
        assert "LOW_LIQUIDITY" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_spread_too_wide(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 4.0)  # spread=8 > 3×2
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "SPREAD_TOO_WIDE" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_thin_book(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["bids"], kwargs["asks"] = _make_book(5, 50_000.0, 1.0, 0.05)  # depth=0.5 < 20% of 10
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "THIN_BOOK" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_ws_stale(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["last_price_time"] = _NOW - 10.0   # 10s ago > 5s threshold
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "WS_STALE" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_whale_print(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["candle_volume"] = 200.0
        kwargs["volume_sma20"]  = 10.0
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "WHALE_PRINT" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_daily_loss(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _MONDAY_NOON

        kwargs = self._base_kwargs()
        kwargs["daily_pnl_pct"] = -0.05
        is_killed, reason = check_kill_filters(**kwargs)
        assert is_killed
        assert "DAILY_LOSS" in reason

    @patch("src.kill_filters.datetime")
    @patch("src.kill_filters.time")
    def test_weekend(self, mock_time, mock_dt):
        mock_time.time.return_value = _NOW
        mock_dt.now.return_value = _SATURDAY

        is_killed, reason = check_kill_filters(**self._base_kwargs())
        assert is_killed
        assert "WEEKEND" in reason
