"""
Unit tests for src/probability_gate.py

Every trade entry is gated here — zero test coverage on this module is a
live-trading risk. This file covers:

Core math:
  _stack(): empty, single-edge, multi-edge stacking, clip at 0.97
  _kelly(): R:R cases, negative fraction clamped to 0, rr=0 guard
  _classify_tier(): all four tiers, boundary conditions, MAX_TRADE_USD cap

Edge evaluators (present/absent + p_win values):
  _ofi_edge:         aligned/opposing/None/thresholds (0.10, 0.20, 0.35)
  _lead_lag_edge:    aligned/opposing, strength thresholds (0.4, 0.6)
  _regime_edge:      buy/sell × regime variants, MR entry paths
  _rsi_edge:         buy/sell × oversold/overbought thresholds
  _adx_edge:         weak/solid/strong (18, 22, 30)
  _htf_edge:         None/positive/negative, thresholds (0, 5)
  _funding_edge:     None/favorable/unfavorable for both sides
  _gold_edge:        None/weak-corr/small-move/inverse/positive regimes
  _contagion_edge:   BTC (n/a), non-BTC aligned/opposing
  _confidence_edge:  all four tiers (85, 70, 55, below)

ProbabilityGate.evaluate():
  - combined_p reflects present edges
  - rejected when combined_p < min_p
  - not rejected when combined_p >= min_p
  - size_scale <= 1.0 always
  - size_scale == 0.0 when kelly yields 0
  - tier / target_usd / trail_style populated from _classify_tier
  - is_macro_driven set when gold/contagion edge is present
  - present_edges property
"""

import pytest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

from src.probability_gate import (
    _stack, _kelly, _classify_tier,
    _ofi_edge, _lead_lag_edge, _regime_edge, _rsi_edge,
    _adx_edge, _htf_edge, _funding_edge, _gold_edge,
    _contagion_edge, _confidence_edge,
    ProbabilityGate, Edge, TradeReasoning,
    MIN_PROBABILITY, KELLY_REF, TIERS, MAX_TRADE_USD,
)


# ── helpers ───────────────────────────────────────────────────────────────────

@dataclass
class _Sig:
    """Minimal fake signal that the gate can introspect."""
    confidence:     float          = 75.0
    ofi:            Optional[float] = 0.25
    lead_lag_dir:   Optional[str]  = "BUY"
    regime:         str            = "TRENDING_UP"
    rsi:            float          = 35.0
    adx:            float          = 28.0
    funding_rate:   Optional[float] = None
    atr:            Optional[float] = 500.0
    close:          Optional[float] = 50_000.0

    def stop_loss_pct(self):  return 0.01   # 1%
    def take_profit_pct(self): return 0.02  # 2%


def _macro(corr_30d=0.0, gold_1d=0.0, corr_strength=0.0, is_inverse=False):
    m = MagicMock()
    m.btc_gold_corr_30d = corr_30d
    m.gold_change_1d    = gold_1d
    m.corr_strength     = corr_strength
    m.is_inverse_regime = is_inverse
    return m


# ── _stack ─────────────────────────────────────────────────────────────────────

class TestStack:
    def test_empty_returns_0_5(self):
        assert _stack([]) == pytest.approx(0.5)

    def test_single_edge_equals_its_own_p(self):
        """Single edge should yield p_total == p (no inflation from stacking)."""
        for p in (0.52, 0.58, 0.62, 0.70):
            assert _stack([p]) == pytest.approx(p, rel=1e-9)

    def test_two_edges_greater_than_either_alone(self):
        p1, p2 = 0.60, 0.58
        result = _stack([p1, p2])
        assert result > max(p1, p2)

    def test_stacking_is_commutative(self):
        assert _stack([0.62, 0.58]) == pytest.approx(_stack([0.58, 0.62]))

    def test_clipped_at_0_97(self):
        # Many strong edges should still not exceed 0.97
        result = _stack([0.95] * 10)
        assert result == pytest.approx(0.97)

    def test_below_0_5_edge_clamped_up(self):
        # p < 0.5 is clamped to 0.5 → same as a neutral edge
        result_clamped = _stack([0.3])
        result_neutral = _stack([0.5])
        assert result_clamped == pytest.approx(result_neutral)

    def test_three_moderate_edges_above_threshold(self):
        result = _stack([0.58, 0.60, 0.57])
        assert result >= MIN_PROBABILITY

    def test_math_formula(self):
        """Correlation-shrunk log-odds: logit(P) = (1−λ)·max logit + λ·Σ logit,
        with p_eff clamped to [0.5, 0.95] and λ = EDGE_CORRELATION_LAMBDA."""
        import math
        from src.probability_gate import EDGE_CORRELATION_LAMBDA as lam
        probs = [0.62, 0.58]
        logits = [math.log(p / (1.0 - p)) for p in probs]
        total_logit = (1.0 - lam) * max(logits) + lam * sum(logits)
        expected = 1.0 / (1.0 + math.exp(-total_logit))
        assert _stack(probs) == pytest.approx(expected)

    def test_lambda_one_recovers_pure_log_odds(self):
        """λ=1.0 must reduce to the independent naive-Bayes stack (Σ logit)."""
        import math
        probs = [0.62, 0.58, 0.55]
        total_logit = sum(math.log(p / (1.0 - p)) for p in probs)
        expected = 1.0 / (1.0 + math.exp(-total_logit))
        assert _stack(probs, corr_lambda=1.0) == pytest.approx(expected)

    def test_shrinkage_is_below_pure_log_odds(self):
        """The default shrunk combiner must be strictly more conservative than
        the un-shrunk (λ=1) stack whenever ≥2 edges are present."""
        probs = [0.60, 0.58, 0.57]
        assert _stack(probs) < _stack(probs, corr_lambda=1.0)

    def test_shrinkage_grows_sublinearly_in_edge_count(self):
        """Adding identical correlated edges should add less each time —
        the marginal lift of the 4th 0.55 edge < lift of the 2nd."""
        lift_2 = _stack([0.55, 0.55]) - _stack([0.55])
        lift_4 = _stack([0.55] * 4) - _stack([0.55] * 3)
        assert 0 < lift_4 < lift_2

    def test_log_odds_is_more_conservative_than_noisy_or(self):
        """Regression guard: the new combiner must NOT saturate the way noisy-OR
        did. Three barely-above-coinflip edges should stay well under the old
        0.90 noisy-OR result."""
        probs = [0.53, 0.53, 0.53]
        result = _stack(probs)
        noisy_or = 1.0 - (1.0 - 0.53) ** 3   # ≈ 0.896
        assert result < noisy_or
        assert result < 0.65   # below the gate's MIN_PROBABILITY


# ── _kelly ─────────────────────────────────────────────────────────────────────

class TestKelly:
    def test_rr_zero_returns_zero(self):
        assert _kelly(0.6, 0.0) == 0.0

    def test_negative_rr_returns_zero(self):
        assert _kelly(0.6, -1.0) == 0.0

    def test_standard_case(self):
        # f* = (p*(b+1) - 1) / b  with p=0.6, b=2.0
        # f* = (0.6*3 - 1) / 2 = 0.8/2 = 0.4
        assert _kelly(0.6, 2.0) == pytest.approx(0.4)

    def test_breakeven_trade_returns_zero(self):
        # p=0.5, b=1 (1:1 R:R) → f* = (0.5*2 - 1)/1 = 0 → max(0, 0) = 0
        assert _kelly(0.5, 1.0) == pytest.approx(0.0)

    def test_losing_edge_clamped_to_zero(self):
        # p=0.4, b=1 → f* negative → clamped
        assert _kelly(0.4, 1.0) == 0.0

    def test_higher_rr_increases_size(self):
        f1 = _kelly(0.6, 1.0)
        f2 = _kelly(0.6, 2.0)
        f3 = _kelly(0.6, 3.0)
        assert f1 < f2 < f3

    def test_higher_p_increases_size(self):
        f1 = _kelly(0.55, 2.0)
        f2 = _kelly(0.65, 2.0)
        assert f1 < f2


# ── _classify_tier ─────────────────────────────────────────────────────────────

class TestClassifyTier:
    def test_conviction_tier(self):
        name, usd, hold, trail = _classify_tier(0.82, 6)
        assert name == "conviction"
        assert trail == "ema50_4h"

    def test_position_tier(self):
        name, usd, hold, trail = _classify_tier(0.77, 4)
        assert name == "position"
        assert trail == "ema50_4h"

    def test_swing_tier(self):
        name, usd, hold, trail = _classify_tier(0.67, 3)
        assert name == "swing"
        assert trail == "ema21_1h"

    def test_scalp_tier_default(self):
        name, usd, hold, trail = _classify_tier(0.59, 2)
        assert name == "scalp"
        assert trail == "atr_stop"

    def test_scalp_when_p_high_but_few_edges(self):
        # High probability but only 1 edge → falls through to scalp
        name, _, _, _ = _classify_tier(0.90, 1)
        assert name == "scalp"

    def test_conviction_p_not_met_falls_to_next(self):
        # p=0.77 is < 0.80 (conviction) but ≥ 0.75 (position) with 4+ edges
        name, _, _, _ = _classify_tier(0.77, 5)
        assert name == "position"

    def test_max_trade_usd_cap(self):
        _, usd, _, _ = _classify_tier(0.90, 10)
        assert usd <= MAX_TRADE_USD

    def test_scalp_usd_cap(self):
        _, usd, _, _ = _classify_tier(0.50, 0)
        assert usd <= MAX_TRADE_USD

    def test_returns_tuple_of_four(self):
        result = _classify_tier(0.65, 3)
        assert len(result) == 4


# ── _ofi_edge ─────────────────────────────────────────────────────────────────

class TestOfiEdge:
    def test_none_returns_absent(self):
        e = _ofi_edge(None, True)
        assert e.present is False

    def test_opposing_ofi_absent(self):
        e = _ofi_edge(-0.5, True)   # negative OFI on a long
        assert e.present is False

    def test_weak_ofi_absent(self):
        e = _ofi_edge(0.05, True)   # below 0.10 threshold
        assert e.present is False

    def test_mild_ofi_present(self):
        e = _ofi_edge(0.15, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.54)

    def test_moderate_ofi_present(self):
        e = _ofi_edge(0.25, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.58)

    def test_heavy_ofi_present(self):
        e = _ofi_edge(0.40, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.64)

    def test_short_aligned_negative_ofi(self):
        e = _ofi_edge(-0.25, False)  # short with negative OFI = aligned
        assert e.present is True

    def test_short_opposing_positive_ofi(self):
        e = _ofi_edge(0.25, False)   # short with positive OFI = opposing
        assert e.present is False

    def test_exactly_at_threshold_0_10(self):
        e = _ofi_edge(0.10, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.54)

    def test_exactly_at_threshold_0_20(self):
        e = _ofi_edge(0.20, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.58)

    def test_exactly_at_threshold_0_35(self):
        e = _ofi_edge(0.35, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.64)


# ── _lead_lag_edge ────────────────────────────────────────────────────────────

class TestLeadLagEdge:
    def test_opposing_direction_absent(self):
        e = _lead_lag_edge("SELL", 0.8, True)   # want BUY but got SELL
        assert e.present is False

    def test_none_direction_absent(self):
        e = _lead_lag_edge(None, 0.8, True)
        assert e.present is False

    def test_aligned_strong_p(self):
        e = _lead_lag_edge("BUY", 0.65, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.62)

    def test_aligned_moderate_p(self):
        e = _lead_lag_edge("BUY", 0.45, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.57)

    def test_aligned_weak_p(self):
        e = _lead_lag_edge("BUY", 0.2, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.53)

    def test_sell_aligned_with_sell_dir(self):
        e = _lead_lag_edge("SELL", 0.7, False)
        assert e.present is True
        assert e.p_win == pytest.approx(0.62)

    def test_strength_at_boundary_0_6(self):
        e = _lead_lag_edge("BUY", 0.6, True)
        assert e.p_win == pytest.approx(0.62)

    def test_strength_at_boundary_0_4(self):
        e = _lead_lag_edge("BUY", 0.4, True)
        assert e.p_win == pytest.approx(0.57)


# ── _regime_edge ──────────────────────────────────────────────────────────────

class TestRegimeEdge:
    def test_buy_trending_up(self):
        e = _regime_edge("TRENDING_UP", True, "main")
        assert e.present is True
        assert e.p_win == pytest.approx(0.62)

    def test_sell_trending_down(self):
        e = _regime_edge("TRENDING_DOWN", False, "main")
        assert e.present is True
        assert e.p_win == pytest.approx(0.60)

    def test_sell_crash(self):
        e = _regime_edge("CRASH", False, "main")
        assert e.present is True
        assert e.p_win == pytest.approx(0.58)

    def test_buy_trending_down_counter_trend_absent(self):
        e = _regime_edge("TRENDING_DOWN", True, "main")
        assert e.present is False

    def test_ranging_main_absent(self):
        e = _regime_edge("RANGING", True, "main")
        assert e.present is False

    def test_volatile_main_absent(self):
        e = _regime_edge("VOLATILE", True, "main")
        assert e.present is False

    def test_mr_path_ranging_present(self):
        e = _regime_edge("RANGING", True, "mr")
        assert e.present is True
        assert e.p_win == pytest.approx(0.60)

    def test_mr_extreme_ranging_present(self):
        e = _regime_edge("RANGING", True, "mr-extreme")
        assert e.present is True

    def test_mr_volatile_present(self):
        e = _regime_edge("VOLATILE", True, "mr")
        assert e.present is True
        assert e.p_win == pytest.approx(0.54)

    def test_mr_trending_up_absent(self):
        # MR against a trend has no edge
        e = _regime_edge("TRENDING_UP", True, "mr")
        assert e.present is False

    def test_buy_crash_absent(self):
        e = _regime_edge("CRASH", True, "main")
        assert e.present is False


# ── _rsi_edge ─────────────────────────────────────────────────────────────────

class TestRsiEdge:
    def test_buy_deeply_oversold(self):
        e = _rsi_edge(25.0, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.60)

    def test_buy_oversold_pullback(self):
        e = _rsi_edge(35.0, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.56)

    def test_buy_overbought_absent(self):
        e = _rsi_edge(72.0, True)
        assert e.present is False

    def test_buy_neutral_absent(self):
        e = _rsi_edge(50.0, True)
        assert e.present is False

    def test_sell_deeply_overbought(self):
        e = _rsi_edge(75.0, False)
        assert e.present is True
        assert e.p_win == pytest.approx(0.58)

    def test_sell_overbought(self):
        e = _rsi_edge(65.0, False)
        assert e.present is True
        assert e.p_win == pytest.approx(0.55)

    def test_sell_oversold_absent(self):
        e = _rsi_edge(28.0, False)
        assert e.present is False

    def test_sell_neutral_absent(self):
        e = _rsi_edge(50.0, False)
        assert e.present is False

    def test_buy_at_boundary_27(self):
        e = _rsi_edge(27.0, True)
        assert e.p_win == pytest.approx(0.60)

    def test_buy_at_boundary_38(self):
        e = _rsi_edge(38.0, True)
        assert e.p_win == pytest.approx(0.56)

    def test_sell_at_boundary_73(self):
        e = _rsi_edge(73.0, False)
        assert e.p_win == pytest.approx(0.58)

    def test_sell_at_boundary_62(self):
        e = _rsi_edge(62.0, False)
        assert e.p_win == pytest.approx(0.55)


# ── _adx_edge ─────────────────────────────────────────────────────────────────

class TestAdxEdge:
    def test_very_strong_trend(self):
        e = _adx_edge(32.0)
        assert e.present is True
        assert e.p_win == pytest.approx(0.57)

    def test_solid_trend(self):
        e = _adx_edge(25.0)
        assert e.present is True
        assert e.p_win == pytest.approx(0.54)

    def test_weak_trend_absent(self):
        e = _adx_edge(15.0)
        assert e.present is False

    def test_at_boundary_30(self):
        e = _adx_edge(30.0)
        assert e.p_win == pytest.approx(0.57)

    def test_at_boundary_22(self):
        e = _adx_edge(22.0)
        assert e.p_win == pytest.approx(0.54)

    def test_just_below_22_absent(self):
        e = _adx_edge(21.9)
        assert e.present is False


# ── _htf_edge ─────────────────────────────────────────────────────────────────

class TestHtfEdge:
    def test_none_absent(self):
        e = _htf_edge(None, True)
        assert e.present is False

    def test_strongly_aligned(self):
        e = _htf_edge(8.0, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.58)

    def test_mildly_aligned(self):
        e = _htf_edge(3.0, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.53)

    def test_strongly_against_absent(self):
        e = _htf_edge(-8.0, True)
        assert e.present is False

    def test_neutral_absent(self):
        e = _htf_edge(0.0, True)
        assert e.present is False

    def test_at_boundary_5(self):
        # htf_alignment > 5 → 0.58
        e = _htf_edge(5.1, True)
        assert e.p_win == pytest.approx(0.58)

    def test_just_at_5_mildly_aligned(self):
        # htf_alignment == 5 is NOT > 5, so falls to second branch (> 0 → 0.53)
        e = _htf_edge(5.0, True)
        assert e.p_win == pytest.approx(0.53)


# ── _funding_edge ─────────────────────────────────────────────────────────────

class TestFundingEdge:
    def test_none_absent(self):
        e = _funding_edge(None, True)
        assert e.present is False

    def test_buy_very_negative_funding_favorable(self):
        # rate * 1095 * 100 = apy %; < -5% APY is favorable for longs
        rate = -0.0001  # apy ~ -10.95%
        e = _funding_edge(rate, True)
        assert e.present is True
        assert e.p_win == pytest.approx(0.55)

    def test_buy_positive_funding_absent(self):
        rate = 0.0001   # longs pay, unfavorable
        e = _funding_edge(rate, True)
        assert e.present is False

    def test_sell_high_funding_favorable(self):
        # rate * 1095 * 100 > 15% APY → longs pay, shorts collect
        rate = 0.0002   # apy ~ 21.9%
        e = _funding_edge(rate, False)
        assert e.present is True
        assert e.p_win == pytest.approx(0.55)

    def test_sell_negative_funding_absent(self):
        rate = -0.0001  # shorts pay, unfavorable
        e = _funding_edge(rate, False)
        assert e.present is False

    def test_buy_zero_funding_absent(self):
        e = _funding_edge(0.0, True)
        assert e.present is False


# ── _gold_edge ────────────────────────────────────────────────────────────────

class TestGoldEdge:
    def test_none_macro_absent(self):
        e = _gold_edge(None, True)
        assert e.present is False

    def test_weak_correlation_absent(self):
        m = _macro(corr_30d=0.3, gold_1d=1.0)
        e = _gold_edge(m, True)
        assert e.present is False

    def test_small_gold_move_absent(self):
        m = _macro(corr_30d=-0.6, gold_1d=0.3)
        e = _gold_edge(m, True)
        assert e.present is False

    def test_inverse_regime_gold_down_buy_edge(self):
        # inverse regime: gold down → crypto up → favorable for BUY
        m = _macro(corr_30d=-0.7, gold_1d=-1.2)
        e = _gold_edge(m, True)
        assert e.present is True
        assert e.p_win > 0.5

    def test_inverse_regime_gold_down_sell_absent(self):
        m = _macro(corr_30d=-0.7, gold_1d=-1.2)
        e = _gold_edge(m, False)   # gold down → crypto up → against short
        assert e.present is False

    def test_inverse_regime_gold_up_sell_edge(self):
        # inverse: gold up → crypto down → favorable for SHORT
        m = _macro(corr_30d=-0.7, gold_1d=1.5)
        e = _gold_edge(m, False)
        assert e.present is True

    def test_positive_regime_gold_up_buy_edge(self):
        # positive corr: gold up → crypto up → favorable for BUY
        m = _macro(corr_30d=0.7, gold_1d=1.5)
        e = _gold_edge(m, True)
        assert e.present is True

    def test_positive_regime_gold_down_buy_absent(self):
        m = _macro(corr_30d=0.7, gold_1d=-1.5)
        e = _gold_edge(m, True)
        assert e.present is False

    def test_p_win_bounded_by_magnitude_cap(self):
        m = _macro(corr_30d=-0.9, gold_1d=-10.0)
        e = _gold_edge(m, True)
        assert e.p_win <= 0.62


# ── _contagion_edge ───────────────────────────────────────────────────────────

class TestContagionEdge:
    def test_btc_symbol_always_absent(self):
        m = _macro(corr_strength=0.8, gold_1d=-1.5, is_inverse=True)
        e = _contagion_edge("BTC/USD", m, True)
        assert e.present is False

    def test_none_macro_absent(self):
        e = _contagion_edge("ETH/USD", None, True)
        assert e.present is False

    def test_weak_corr_strength_absent(self):
        m = _macro(corr_strength=0.3, gold_1d=-1.5, is_inverse=True)
        e = _contagion_edge("ETH/USD", m, True)
        assert e.present is False

    def test_small_gold_move_absent(self):
        m = _macro(corr_strength=0.8, gold_1d=-0.2, is_inverse=True)
        e = _contagion_edge("ETH/USD", m, True)
        assert e.present is False

    def test_eth_aligned_buy(self):
        # inverse regime + gold down → crypto up → buy is aligned
        m = _macro(corr_strength=0.8, gold_1d=-1.5, is_inverse=True)
        e = _contagion_edge("ETH/USD", m, True)
        assert e.present is True
        assert e.p_win > 0.5

    def test_sol_aligned_p_higher_than_eth(self):
        # SOL has higher beta (1.3x) than ETH (1.05x)
        m = _macro(corr_strength=0.8, gold_1d=-1.5, is_inverse=True)
        e_eth = _contagion_edge("ETH/USD", m, True)
        e_sol = _contagion_edge("SOL/USD", m, True)
        # Both present, SOL has slightly higher p_win due to higher beta
        assert e_sol.p_win >= e_eth.p_win

    def test_non_btc_opposing_absent(self):
        # inverse + gold down → crypto up, but we're shorting → absent
        m = _macro(corr_strength=0.8, gold_1d=-1.5, is_inverse=True)
        e = _contagion_edge("ETH/USD", m, False)
        assert e.present is False


# ── _confidence_edge ──────────────────────────────────────────────────────────

class TestConfidenceEdge:
    def test_very_strong(self):
        e = _confidence_edge(90.0)
        assert e.present is True
        assert e.p_win == pytest.approx(0.60)

    def test_strong(self):
        e = _confidence_edge(75.0)
        assert e.present is True
        assert e.p_win == pytest.approx(0.56)

    def test_above_threshold(self):
        e = _confidence_edge(60.0)
        assert e.present is True
        assert e.p_win == pytest.approx(0.53)

    def test_marginal_absent(self):
        e = _confidence_edge(40.0)
        assert e.present is False

    def test_at_boundary_85(self):
        e = _confidence_edge(85.0)
        assert e.p_win == pytest.approx(0.60)

    def test_at_boundary_70(self):
        e = _confidence_edge(70.0)
        assert e.p_win == pytest.approx(0.56)

    def test_at_boundary_55(self):
        e = _confidence_edge(55.0)
        assert e.p_win == pytest.approx(0.53)

    def test_just_below_55_absent(self):
        e = _confidence_edge(54.9)
        assert e.present is False


# ── ProbabilityGate.evaluate ──────────────────────────────────────────────────

def _strong_sig():
    """Signal with many aligned edges — should clear MIN_PROBABILITY."""
    return _Sig(
        confidence=80.0,
        ofi=0.30,
        lead_lag_dir="BUY",
        regime="TRENDING_UP",
        rsi=33.0,
        adx=28.0,
        funding_rate=-0.0002,   # favorable for longs
    )


class TestProbabilityGateEvaluate:

    def _gate(self, min_p=0.58, kelly_ref=0.10):
        return ProbabilityGate(min_p=min_p, kelly_ref=kelly_ref)

    def test_returns_trade_reasoning(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert isinstance(r, TradeReasoning)

    def test_not_rejected_with_strong_signal(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.rejected is False

    def test_rejected_with_weak_signal(self):
        # Gate set very high; weak signal should be rejected
        gate = ProbabilityGate(min_p=0.99, kelly_ref=0.10)
        r = gate.evaluate(_Sig(confidence=30.0, ofi=None, lead_lag_dir=None,
                               regime="RANGING", rsi=50.0, adx=10.0),
                          is_buy=True)
        assert r.rejected is True

    def test_rejection_reason_populated_when_rejected(self):
        gate = ProbabilityGate(min_p=0.99, kelly_ref=0.10)
        r = gate.evaluate(_Sig(confidence=30.0, ofi=None, lead_lag_dir=None,
                               regime="RANGING", rsi=50.0, adx=10.0),
                          is_buy=True)
        assert r.rejection_reason is not None
        assert "P=" in r.rejection_reason

    def test_combined_p_within_bounds(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert 0.0 <= r.combined_p <= 1.0

    def test_size_scale_at_most_1(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.size_scale <= 1.0

    def test_size_scale_non_negative(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.size_scale >= 0.0

    def test_direction_long_for_buy(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.direction == "LONG"

    def test_direction_short_for_sell(self):
        sig = _Sig(
            confidence=80.0, ofi=-0.3,
            lead_lag_dir="SELL", regime="TRENDING_DOWN",
            rsi=75.0, adx=28.0
        )
        r = self._gate().evaluate(sig, is_buy=False)
        assert r.direction == "SHORT"

    def test_present_edges_property(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        for e in r.present_edges:
            assert e.present is True

    def test_all_edges_in_results(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert len(r.edges) > 0

    def test_tier_is_valid_string(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.tier in ("scalp", "swing", "position", "conviction")

    def test_target_usd_positive(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.target_usd > 0

    def test_target_usd_capped_by_max(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.target_usd <= MAX_TRADE_USD

    def test_hold_minutes_positive(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.hold_minutes > 0

    def test_trail_style_valid(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True)
        assert r.trail_style in ("atr_stop", "ema21_1h", "ema50_4h")

    def test_is_macro_driven_false_without_macro(self):
        r = self._gate().evaluate(_strong_sig(), is_buy=True, macro_state=None)
        assert r.is_macro_driven is False

    def test_is_macro_driven_true_with_active_macro(self):
        macro = _macro(corr_30d=-0.8, gold_1d=-2.0, corr_strength=0.9, is_inverse=True)
        r = self._gate().evaluate(
            _strong_sig(), is_buy=True,
            macro_state=macro, symbol="ETH/USD"
        )
        assert r.is_macro_driven is True

    def test_signal_rr_exception_falls_back_to_2(self):
        """If stop_loss_pct() / take_profit_pct() raise, gate uses R:R=2.0."""
        sig = _Sig()
        sig.stop_loss_pct = MagicMock(side_effect=AttributeError("no method"))
        # Should not raise; gate catches and defaults to rr=2.0
        r = self._gate().evaluate(sig, is_buy=True)
        assert isinstance(r, TradeReasoning)

    def test_more_edges_raise_combined_p(self):
        """Adding more present edges should increase combined_p."""
        # Minimal signal (few present edges)
        sig_few = _Sig(confidence=30.0, ofi=None, lead_lag_dir=None,
                       regime="RANGING", rsi=50.0, adx=10.0)
        # Rich signal (many present edges)
        sig_many = _strong_sig()
        gate = self._gate()
        r_few  = gate.evaluate(sig_few,  is_buy=True)
        r_many = gate.evaluate(sig_many, is_buy=True)
        assert r_many.combined_p > r_few.combined_p

    def test_large_kelly_ref_shrinks_size_scale(self):
        """A very large kelly_ref should produce a very small (near-zero) size_scale."""
        gate = ProbabilityGate(min_p=0.0, kelly_ref=10_000.0)
        r = gate.evaluate(_Sig(confidence=30.0, ofi=None, lead_lag_dir=None,
                               regime="RANGING", rsi=50.0, adx=10.0),
                          is_buy=True)
        assert r.size_scale < 0.01

    def test_entry_path_passed_to_regime_edge(self):
        """MR entry path changes regime edge for RANGING regime."""
        sig_ranging = _Sig(regime="RANGING", confidence=60.0,
                           ofi=None, lead_lag_dir=None, adx=10.0, rsi=50.0)
        gate = self._gate()
        r_main = gate.evaluate(sig_ranging, is_buy=True, entry_path="main")
        r_mr   = gate.evaluate(sig_ranging, is_buy=True, entry_path="mr")
        # regime edge is present in MR path but absent in main path
        regime_present_main = any(
            e.name == "regime" and e.present for e in r_main.edges
        )
        regime_present_mr = any(
            e.name == "regime" and e.present for e in r_mr.edges
        )
        assert regime_present_main is False
        assert regime_present_mr is True

    def test_combined_p_equals_stack_of_present_probs(self):
        """combined_p must equal _stack() applied to present edge p_wins."""
        sig = _strong_sig()
        gate = self._gate()
        r = gate.evaluate(sig, is_buy=True)
        expected = _stack([e.p_win for e in r.edges if e.present])
        assert r.combined_p == pytest.approx(expected, rel=1e-6)


# ── Edge.present attribute ────────────────────────────────────────────────────

class TestEdgeDataclass:
    def test_default_present_true(self):
        e = Edge("test", 0.60, "note")
        assert e.present is True

    def test_explicit_absent(self):
        e = Edge("test", 0.60, "note", present=False)
        assert e.present is False


# ── TradeReasoning.present_edges ──────────────────────────────────────────────

class TestTradeReasoningPresentEdges:
    def test_filters_absent_edges(self):
        edges = [
            Edge("a", 0.60, "note", present=True),
            Edge("b", 0.55, "note", present=False),
            Edge("c", 0.58, "note", present=True),
        ]
        r = TradeReasoning(
            direction="LONG", edges=edges, combined_p=0.70,
            kelly_fraction=0.2, quarter_kelly=0.05,
            size_scale=0.5, rejected=False,
        )
        assert len(r.present_edges) == 2
        assert all(e.present for e in r.present_edges)

    def test_empty_when_all_absent(self):
        edges = [Edge("a", 0.5, "n", present=False)]
        r = TradeReasoning(
            direction="LONG", edges=edges, combined_p=0.5,
            kelly_fraction=0.0, quarter_kelly=0.0,
            size_scale=0.0, rejected=True,
        )
        assert r.present_edges == []
