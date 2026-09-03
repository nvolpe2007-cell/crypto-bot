"""
Unit tests for src/orderflow_indicator.py

The OFI tests matter most: the Cont-Kukanov-Stoikov sign conventions are easy to
get subtly backwards, and a sign error there is invisible downstream (it just
makes the indicator quietly anti-predictive). So each of the six price cases is
pinned with a hand-computed expected value, independent of the implementation.
"""

import numpy as np
import pandas as pd
import pytest

from src.orderflow_indicator import (
    tick_rule_side,
    signed_volume_from_ticks,
    bar_signed_volume,
    cumulative_volume_delta,
    book_imbalance,
    ofi_from_books,
    rolling_zscore,
    cvd_price_divergence,
)


# ── tick_rule_side ────────────────────────────────────────────────────────────

class TestTickRuleSide:
    def test_upticks_are_buys_downticks_are_sells(self):
        s = tick_rule_side(pd.Series([100.0, 101.0, 100.5]))
        assert s.tolist() == [0.0, 1.0, -1.0]

    def test_flat_prints_carry_the_previous_sign(self):
        s = tick_rule_side(pd.Series([100.0, 101.0, 101.0, 101.0]))
        assert s.tolist() == [0.0, 1.0, 1.0, 1.0]

    def test_leading_trades_are_zero_not_guessed(self):
        # No prior price => no basis for a side. Must not fabricate one.
        s = tick_rule_side(pd.Series([100.0, 100.0, 100.0]))
        assert s.tolist() == [0.0, 0.0, 0.0]

    def test_sign_flips_are_tracked(self):
        s = tick_rule_side(pd.Series([10.0, 11.0, 10.0, 12.0, 12.0]))
        assert s.tolist() == [0.0, 1.0, -1.0, 1.0, 1.0]

    def test_single_trade(self):
        assert tick_rule_side(pd.Series([42.0])).tolist() == [0.0]


# ── signed_volume_from_ticks ──────────────────────────────────────────────────

class TestSignedVolumeFromTicks:
    def test_explicit_side_column_wins(self):
        # Price is flat, so the tick rule alone would yield 0 for everything.
        # The real side column must still produce signed flow.
        trades = pd.DataFrame({
            "price": [100.0, 100.0, 100.0],
            "qty": [1.0, 2.0, 3.0],
            "side": ["buy", "sell", "buy"],
        })
        assert signed_volume_from_ticks(trades).tolist() == [1.0, -2.0, 3.0]

    def test_side_is_case_and_alias_insensitive(self):
        trades = pd.DataFrame({
            "price": [1.0, 1.0, 1.0, 1.0],
            "qty": [1.0, 1.0, 1.0, 1.0],
            "side": ["BUY", "Sell", "b", "S"],
        })
        assert signed_volume_from_ticks(trades).tolist() == [1.0, -1.0, 1.0, -1.0]

    def test_numeric_side_column(self):
        trades = pd.DataFrame({
            "price": [1.0, 1.0], "qty": [5.0, 7.0], "side": [1, -1],
        })
        assert signed_volume_from_ticks(trades).tolist() == [5.0, -7.0]

    def test_falls_back_to_tick_rule_without_side(self):
        trades = pd.DataFrame({
            "price": [100.0, 101.0, 100.0],
            "qty": [1.0, 2.0, 3.0],
        })
        out = signed_volume_from_ticks(trades, side_col=None)
        assert out.tolist() == [0.0, 2.0, -3.0]

    def test_partial_side_column_uses_tick_rule_only_for_gaps(self):
        # Real side where known, inferred where null — neither source discarded.
        trades = pd.DataFrame({
            "price": [100.0, 101.0, 102.0],
            "qty": [1.0, 2.0, 3.0],
            "side": ["sell", None, None],
        })
        out = signed_volume_from_ticks(trades)
        assert out.tolist() == [-1.0, 2.0, 3.0]

    def test_empty_tape(self):
        assert signed_volume_from_ticks(pd.DataFrame()).empty


# ── bar_signed_volume ─────────────────────────────────────────────────────────

class TestBarSignedVolume:
    def test_close_at_high_is_full_positive(self):
        df = pd.DataFrame({"high": [10.0], "low": [8.0], "close": [10.0],
                           "volume": [100.0]})
        assert bar_signed_volume(df).iloc[0] == pytest.approx(100.0)

    def test_close_at_low_is_full_negative(self):
        df = pd.DataFrame({"high": [10.0], "low": [8.0], "close": [8.0],
                           "volume": [100.0]})
        assert bar_signed_volume(df).iloc[0] == pytest.approx(-100.0)

    def test_close_at_midpoint_is_zero(self):
        df = pd.DataFrame({"high": [10.0], "low": [8.0], "close": [9.0],
                           "volume": [100.0]})
        assert bar_signed_volume(df).iloc[0] == pytest.approx(0.0)

    def test_doji_bar_does_not_divide_by_zero(self):
        df = pd.DataFrame({"high": [5.0], "low": [5.0], "close": [5.0],
                           "volume": [100.0]})
        out = bar_signed_volume(df)
        assert out.iloc[0] == 0.0
        assert np.isfinite(out.iloc[0])

    def test_bounded_by_volume(self):
        rng = np.random.default_rng(0)
        n = 200
        low = rng.uniform(90, 100, n)
        high = low + rng.uniform(0.1, 5, n)
        close = rng.uniform(low, high)
        vol = rng.uniform(1, 1000, n)
        df = pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})
        out = bar_signed_volume(df)
        assert (out.abs() <= pd.Series(vol) + 1e-9).all()


# ── cumulative_volume_delta ───────────────────────────────────────────────────

class TestCumulativeVolumeDelta:
    def test_running_sum(self):
        cvd = cumulative_volume_delta(pd.Series([1.0, -2.0, 3.0]))
        assert cvd.tolist() == [1.0, -1.0, 2.0]

    def test_nan_treated_as_no_flow(self):
        cvd = cumulative_volume_delta(pd.Series([1.0, np.nan, 2.0]))
        assert cvd.tolist() == [1.0, 1.0, 3.0]

    def test_balanced_flow_returns_to_zero(self):
        cvd = cumulative_volume_delta(pd.Series([5.0, -5.0]))
        assert cvd.iloc[-1] == 0.0


# ── book_imbalance ────────────────────────────────────────────────────────────

class TestBookImbalance:
    def test_all_bid_is_plus_one(self):
        assert book_imbalance([10.0], [0.0]).iloc[0] == 1.0

    def test_all_ask_is_minus_one(self):
        assert book_imbalance([0.0], [10.0]).iloc[0] == -1.0

    def test_balanced_is_zero(self):
        assert book_imbalance([5.0], [5.0]).iloc[0] == 0.0

    def test_empty_book_is_zero_not_nan(self):
        out = book_imbalance([0.0], [0.0])
        assert out.iloc[0] == 0.0
        assert out.notna().all()

    def test_bounded_range(self):
        rng = np.random.default_rng(1)
        b = rng.uniform(0, 100, 500)
        a = rng.uniform(0, 100, 500)
        out = book_imbalance(b, a)
        assert (out >= -1.0).all() and (out <= 1.0).all()


# ── ofi_from_books — the six cases, hand-computed ─────────────────────────────

class TestOfiFromBooks:
    """
    Each case uses exactly two snapshots so the expected e_1 can be read
    straight off the Cont-Kukanov-Stoikov definition by hand.
    """

    def test_first_element_is_zero_no_prior_snapshot(self):
        out = ofi_from_books([100.0, 100.0], [1.0, 1.0], [101.0, 101.0], [1.0, 1.0])
        assert out.iloc[0] == 0.0

    def test_bid_price_up_is_positive_qb_now(self):
        # Pb 100 -> 101 (up). e = +qb_n = +7
        out = ofi_from_books([100.0, 101.0], [5.0, 7.0], [102.0, 102.0], [3.0, 3.0])
        # ask flat contributes qa_n-1 - qa_n = 3 - 3 = 0
        assert out.iloc[1] == pytest.approx(7.0)

    def test_bid_price_down_is_negative_qb_prev(self):
        # Pb 101 -> 100 (down). e = -qb_n-1 = -5
        out = ofi_from_books([101.0, 100.0], [5.0, 7.0], [102.0, 102.0], [3.0, 3.0])
        assert out.iloc[1] == pytest.approx(-5.0)

    def test_bid_flat_is_size_change_at_the_bid(self):
        # Pb flat. e = qb_n - qb_n-1 = 9 - 4 = +5
        out = ofi_from_books([100.0, 100.0], [4.0, 9.0], [102.0, 102.0], [3.0, 3.0])
        assert out.iloc[1] == pytest.approx(5.0)

    def test_ask_price_down_is_negative_qa_now(self):
        # Pa 102 -> 101 (down) => sell pressure. e = -qa_n = -6
        out = ofi_from_books([100.0, 100.0], [4.0, 4.0], [102.0, 101.0], [3.0, 6.0])
        assert out.iloc[1] == pytest.approx(-6.0)

    def test_ask_price_up_is_positive_qa_prev(self):
        # Pa 101 -> 102 (up) => buy pressure. e = +qa_n-1 = +3
        out = ofi_from_books([100.0, 100.0], [4.0, 4.0], [101.0, 102.0], [3.0, 6.0])
        assert out.iloc[1] == pytest.approx(3.0)

    def test_ask_flat_is_negated_size_change_at_the_ask(self):
        # Pa flat. e = qa_n-1 - qa_n = 3 - 8 = -5  (size ADDED to the ask is
        # sell-side pressure, hence the negation relative to the bid case)
        out = ofi_from_books([100.0, 100.0], [4.0, 4.0], [102.0, 102.0], [3.0, 8.0])
        assert out.iloc[1] == pytest.approx(-5.0)

    def test_static_book_produces_zero_flow(self):
        out = ofi_from_books([100.0] * 5, [4.0] * 5, [101.0] * 5, [3.0] * 5)
        assert (out == 0.0).all()

    def test_sign_antisymmetry_bid_lift_vs_ask_drop(self):
        # A book where both sides step up should not net to zero by accident;
        # this pins that the two sides are not double-counted with one sign.
        up = ofi_from_books([100.0, 101.0], [5.0, 5.0], [101.0, 102.0], [5.0, 5.0])
        # bid up: +5 ; ask up: +5  => +10, unambiguous buy pressure
        assert up.iloc[1] == pytest.approx(10.0)

        down = ofi_from_books([101.0, 100.0], [5.0, 5.0], [102.0, 101.0], [5.0, 5.0])
        # bid down: -5 ; ask down: -5 => -10
        assert down.iloc[1] == pytest.approx(-10.0)

    def test_is_additive_over_windows(self):
        rng = np.random.default_rng(7)
        n = 300
        pb = 100 + np.cumsum(rng.choice([-1.0, 0.0, 1.0], n)) * 0.01
        pa = pb + 0.02
        qb = rng.uniform(1, 10, n)
        qa = rng.uniform(1, 10, n)
        e = ofi_from_books(pb, qb, pa, qa)
        assert e.iloc[:150].sum() + e.iloc[150:].sum() == pytest.approx(e.sum())

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="equal length"):
            ofi_from_books([1.0, 2.0], [1.0], [3.0, 4.0], [1.0, 1.0])

    def test_single_snapshot_is_zero(self):
        out = ofi_from_books([100.0], [1.0], [101.0], [1.0])
        assert out.tolist() == [0.0]

    def test_empty_input(self):
        assert ofi_from_books([], [], [], []).empty


# ── rolling_zscore ────────────────────────────────────────────────────────────

class TestRollingZscore:
    def test_warmup_is_nan(self):
        z = rolling_zscore(pd.Series(np.arange(10.0)), window=5)
        assert z.iloc[:4].isna().all()
        assert z.iloc[4:].notna().all()

    def test_flat_window_is_zero_not_inf(self):
        z = rolling_zscore(pd.Series([3.0] * 10), window=5)
        assert np.isfinite(z.iloc[4:]).all()
        assert (z.iloc[4:] == 0.0).all()

    def test_matches_manual_calculation(self):
        s = pd.Series([1.0, 2.0, 3.0, 10.0, 5.0])
        z = rolling_zscore(s, window=3)
        w = s.iloc[2:5]  # last window
        expected = (s.iloc[4] - w.mean()) / w.std(ddof=0)
        assert z.iloc[4] == pytest.approx(expected)

    def test_outlier_scores_high(self):
        s = pd.Series([1.0] * 20 + [50.0])
        z = rolling_zscore(s, window=20)
        assert z.iloc[-1] > 3.0


# ── cvd_price_divergence ──────────────────────────────────────────────────────

class TestCvdPriceDivergence:
    def test_price_down_cvd_up_is_positive(self):
        n = 30
        price = pd.Series(np.linspace(100, 90, n))
        cvd = pd.Series(np.linspace(0, 100, n))
        out = cvd_price_divergence(price, cvd, lookback=10)
        assert out.iloc[-1] == 1.0

    def test_price_up_cvd_down_is_negative(self):
        n = 30
        price = pd.Series(np.linspace(90, 100, n))
        cvd = pd.Series(np.linspace(100, 0, n))
        out = cvd_price_divergence(price, cvd, lookback=10)
        assert out.iloc[-1] == -1.0

    def test_agreement_is_zero(self):
        n = 30
        price = pd.Series(np.linspace(90, 100, n))
        cvd = pd.Series(np.linspace(0, 100, n))
        out = cvd_price_divergence(price, cvd, lookback=10)
        assert out.iloc[-1] == 0.0

    def test_warmup_is_nan(self):
        n = 30
        price = pd.Series(np.linspace(90, 100, n))
        cvd = pd.Series(np.linspace(0, 100, n))
        out = cvd_price_divergence(price, cvd, lookback=10)
        assert out.iloc[:10].isna().all()

    def test_flat_series_is_zero(self):
        price = pd.Series([100.0] * 30)
        cvd = pd.Series([5.0] * 30)
        out = cvd_price_divergence(price, cvd, lookback=10)
        assert (out.iloc[10:] == 0.0).all()


# ── integration: the tape -> CVD -> divergence pipeline ───────────────────────

class TestPipeline:
    def test_tape_to_divergence_end_to_end(self):
        # Price grinds down while buyers keep lifting => bullish divergence.
        n = 60
        prices = np.linspace(100, 95, n)
        trades = pd.DataFrame({
            "price": prices,
            "qty": np.full(n, 2.0),
            "side": ["buy"] * n,
        })
        sv = signed_volume_from_ticks(trades)
        cvd = cumulative_volume_delta(sv)
        div = cvd_price_divergence(pd.Series(prices), cvd, lookback=10)

        assert cvd.iloc[-1] == pytest.approx(2.0 * n)
        assert div.iloc[-1] == 1.0

    def test_bar_proxy_pipeline_runs(self):
        rng = np.random.default_rng(3)
        n = 100
        low = rng.uniform(90, 100, n)
        high = low + rng.uniform(0.5, 3, n)
        close = rng.uniform(low, high)
        df = pd.DataFrame({
            "high": high, "low": low, "close": close,
            "volume": rng.uniform(10, 100, n),
        })
        cvd = cumulative_volume_delta(bar_signed_volume(df))
        z = rolling_zscore(cvd, window=20)
        assert len(cvd) == n
        assert np.isfinite(cvd).all()
        assert z.iloc[19:].notna().all()
