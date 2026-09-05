"""
Unit tests for src/adaptive_stats.py

The headline test is `test_calibrated_threshold_holds_false_promotion_rate`: it
re-runs the noise experiment that failed in the review (52% false promotions at the
spec's |IC| > 0.03) and asserts the calibrated threshold actually delivers the
false-alarm rate it claims. A calibration function that isn't checked against its
own null is just a different arbitrary number.
"""

import math

import numpy as np
import pytest

from src.adaptive_stats import (
    effective_sample_size,
    ic_null_sd,
    ic_promotion_threshold,
    cost_adjusted_score,
    shrink_to_equal_weights,
    cusum_change_detector,
    toxicity_dampener,
)


# ── effective_sample_size ─────────────────────────────────────────────────────

class TestEffectiveSampleSize:
    def test_overlap_divides_the_count(self):
        # the reviewed spec: 1s poll, 300s horizon
        assert effective_sample_size(3000, 300, 1) == pytest.approx(10.0)

    def test_spec_full_confidence_point_is_a_third_of_an_observation(self):
        assert effective_sample_size(100, 300, 1) == pytest.approx(1 / 3, rel=1e-6)

    def test_non_overlapping_is_one_to_one(self):
        assert effective_sample_size(500, 300, 300) == pytest.approx(500.0)

    def test_poll_longer_than_horizon_never_inflates(self):
        # sampling slower than the horizon cannot give MORE than n independent obs
        assert effective_sample_size(500, 60, 300) == pytest.approx(500.0)

    def test_zero_logged(self):
        assert effective_sample_size(0, 300, 1) == 0.0

    def test_invalid_args_raise(self):
        with pytest.raises(ValueError):
            effective_sample_size(100, 0, 1)
        with pytest.raises(ValueError):
            effective_sample_size(100, 300, 0)


# ── ic_null_sd ────────────────────────────────────────────────────────────────

class TestIcNullSd:
    def test_matches_fisher_form(self):
        assert ic_null_sd(103) == pytest.approx(0.1)

    def test_shrinks_with_more_data(self):
        assert ic_null_sd(10_000) < ic_null_sd(1_000) < ic_null_sd(100)

    def test_too_little_data_is_infinite(self):
        assert math.isinf(ic_null_sd(3))
        assert math.isinf(ic_null_sd(0))


# ── ic_promotion_threshold ────────────────────────────────────────────────────

class TestIcPromotionThreshold:
    def test_more_candidates_demands_a_higher_bar(self):
        one = ic_promotion_threshold(1000, n_candidates=1)
        ten = ic_promotion_threshold(1000, n_candidates=10)
        assert ten > one

    def test_more_data_lowers_the_bar(self):
        assert ic_promotion_threshold(10_000) < ic_promotion_threshold(1_000)

    def test_insufficient_data_is_unpromotable(self):
        # the spec promoted after 500 logged ticks == 1.7 effective observations
        n_eff = effective_sample_size(500, 300, 1)
        assert math.isinf(ic_promotion_threshold(n_eff))

    def test_spec_threshold_is_far_below_the_calibrated_one(self):
        # even granting a generous 1000 effective observations, 0.03 is too low
        assert ic_promotion_threshold(1000, n_candidates=1) > 0.03

    def test_invalid_args_raise(self):
        with pytest.raises(ValueError):
            ic_promotion_threshold(1000, n_candidates=0)
        with pytest.raises(ValueError):
            ic_promotion_threshold(1000, target_fwer=0.0)

    def test_calibrated_threshold_holds_false_promotion_rate(self):
        """
        The review's experiment, re-run against the calibrated gate.

        Non-overlapping samples so n_eff == n, pure-noise candidate, and the
        promotion rate must land near the 5% we asked for rather than the 52%
        the hand-picked 0.03 threshold produced.
        """
        rng = np.random.default_rng(7)
        n = 400
        trials = 3000
        thresh = ic_promotion_threshold(n, n_candidates=1, target_fwer=0.05)

        promoted = 0
        for _ in range(trials):
            sig = rng.normal(0, 1, n)
            fwd = rng.normal(0, 1, n)          # independent => zero true IC
            ic = np.corrcoef(sig, fwd)[0, 1]
            if abs(ic) > thresh:
                promoted += 1

        rate = promoted / trials
        assert 0.02 < rate < 0.09, f"false-promotion rate {rate:.1%} off target 5%"

    def test_family_wise_correction_controls_the_family(self):
        """With k candidates, the chance ANY noise candidate promotes stays ~5%."""
        rng = np.random.default_rng(11)
        n, k, trials = 400, 8, 1500
        thresh = ic_promotion_threshold(n, n_candidates=k, target_fwer=0.05)

        families_with_a_promotion = 0
        for _ in range(trials):
            fwd = rng.normal(0, 1, n)
            if any(abs(np.corrcoef(rng.normal(0, 1, n), fwd)[0, 1]) > thresh
                   for _ in range(k)):
                families_with_a_promotion += 1

        rate = families_with_a_promotion / trials
        assert rate < 0.12, f"family-wise rate {rate:.1%} should stay near 5%"


# ── cost_adjusted_score ───────────────────────────────────────────────────────

class TestCostAdjustedScore:
    def test_correct_direction_beyond_cost_scores_positive(self):
        assert cost_adjusted_score(1.0, 0.02, 0.0052) == pytest.approx(0.0148)

    def test_correct_direction_inside_cost_still_loses(self):
        # right about direction, move smaller than the round trip => negative
        assert cost_adjusted_score(1.0, 0.002, 0.0052) < 0

    def test_short_side_symmetric(self):
        assert cost_adjusted_score(-1.0, -0.02, 0.0052) == pytest.approx(0.0148)

    def test_wrong_direction_pays_both(self):
        assert cost_adjusted_score(1.0, -0.02, 0.0052) == pytest.approx(-0.0252)

    def test_no_view_costs_nothing(self):
        assert cost_adjusted_score(0.0, -0.05, 0.0052) == 0.0

    def test_magnitude_ranks_above_frequency(self):
        """
        The behaviour hit rate gets wrong: a signal right 7/10 times on small moves
        must rank BELOW one right 3/10 times on large ones.
        """
        cost = 0.0052
        frequent_small = [cost_adjusted_score(1.0, r, cost)
                          for r in [0.003] * 7 + [-0.003] * 3]
        rare_large = [cost_adjusted_score(1.0, r, cost)
                      for r in [0.05] * 3 + [-0.004] * 7]
        assert sum(frequent_small) < 0 < sum(rare_large)

    def test_nan_inputs_are_neutral(self):
        assert cost_adjusted_score(float("nan"), 0.01, 0.005) == 0.0
        assert cost_adjusted_score(1.0, float("nan"), 0.005) == 0.0


# ── shrink_to_equal_weights ───────────────────────────────────────────────────

class TestShrinkToEqualWeights:
    def test_no_evidence_gives_equal_weights(self):
        out = shrink_to_equal_weights({"a": 1.0, "b": 0.0, "c": 0.0}, n_eff=0)
        assert out["a"] == pytest.approx(1 / 3)
        assert out["b"] == pytest.approx(1 / 3)

    def test_lots_of_evidence_approaches_the_estimate(self):
        out = shrink_to_equal_weights({"a": 0.9, "b": 0.1}, n_eff=100_000)
        assert out["a"] == pytest.approx(0.9, abs=0.01)

    def test_half_shrink_at_prior_strength(self):
        out = shrink_to_equal_weights({"a": 1.0, "b": 0.0}, n_eff=50,
                                      prior_strength=50)
        assert out["a"] == pytest.approx(0.75, abs=1e-9)

    def test_always_normalised(self):
        for n_eff in (0, 10, 1000):
            out = shrink_to_equal_weights({"a": 3.0, "b": 1.0, "c": 0.5}, n_eff)
            assert sum(out.values()) == pytest.approx(1.0)

    def test_all_zero_estimates_fall_back_to_equal(self):
        out = shrink_to_equal_weights({"a": 0.0, "b": 0.0}, n_eff=10_000)
        assert out["a"] == pytest.approx(0.5)

    def test_empty(self):
        assert shrink_to_equal_weights({}, 100) == {}


# ── cusum_change_detector ─────────────────────────────────────────────────────

class TestCusumChangeDetector:
    def test_detects_a_real_downward_shift(self):
        rng = np.random.default_rng(3)
        stream = np.concatenate([rng.normal(0.5, 1.0, 300),
                                 rng.normal(-2.0, 1.0, 200)])
        fired, _, idx = cusum_change_detector(stream, target=0.5)
        assert fired
        assert idx >= 300, "must not fire before the shift happens"

    def test_quiet_on_a_stationary_stream(self):
        """
        The failure being replaced: the Sharpe-ratio test fired on ~31% of
        stationary streams. This must be far quieter.
        """
        rng = np.random.default_rng(5)
        fires = sum(
            cusum_change_detector(rng.normal(0.02, 1.0, 500), target=0.02)[0]
            for _ in range(400)
        )
        rate = fires / 400
        assert rate < 0.05, f"false-alarm rate {rate:.1%} too high for a monitor"

    def test_slack_absorbs_small_drift(self):
        rng = np.random.default_rng(9)
        mild = rng.normal(0.4, 1.0, 400)   # target 0.5, drift well inside slack
        assert not cusum_change_detector(mild, target=0.5, slack_sd=0.5)[0]

    def test_too_short_never_fires(self):
        assert cusum_change_detector([1.0], target=0.0) == (False, 0.0, -1)

    def test_zero_variance_never_fires(self):
        assert not cusum_change_detector([1.0] * 50, target=1.0)[0]

    def test_ignores_non_finite(self):
        fired, _, _ = cusum_change_detector(
            [0.1, float("nan"), 0.2, float("inf"), 0.15], target=0.1)
        assert fired is False


# ── toxicity_dampener ─────────────────────────────────────────────────────────

class TestToxicityDampener:
    def test_zero_toxicity_is_full_size(self):
        assert toxicity_dampener(0.0) == pytest.approx(1.0)

    def test_max_toxicity_is_half_size(self):
        assert toxicity_dampener(1.0) == pytest.approx(0.5)

    def test_percent_scale_is_handled_not_inverted(self):
        # THE BUG: 1.0 - 95*0.5 = -46.5 would flip the trade's direction
        assert toxicity_dampener(95.0) == pytest.approx(0.525)

    def test_never_negative_for_any_input(self):
        for p in (-50, -1, 0, 0.5, 1, 42, 100, 1000, float("inf")):
            assert toxicity_dampener(p) >= 0.5

    def test_never_amplifies(self):
        for p in (-5, 0, 0.3, 1, 100):
            assert toxicity_dampener(p) <= 1.0

    def test_custom_max_reduction(self):
        assert toxicity_dampener(1.0, max_reduction=0.8) == pytest.approx(0.2)
