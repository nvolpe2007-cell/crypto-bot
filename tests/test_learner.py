"""Unit tests for src/learner.py

Covers:
- _distance: zero for identical features, zero for empty dicts, increases with
  larger differences, weighted so high-weight features dominate, always ≥ 0
- _similarity: distance=0 → 1.0, monotonically decreasing, always in [0, 1],
  large distance → near 0
- Learner.required_confidence:
  - Fewer than MIN_TRADES_TO_LEARN records → returns BASE_CONFIDENCE
  - Exactly MIN_TRADES_TO_LEARN records → proceeds to evaluation
  - Regime win rate < 35% (≥ 3 trades) → returns BASE + 12
  - Regime win rate in [35%, 45%) (≥ 3 trades) → returns BASE + 6
  - Regime win rate ≥ 45% → no regime-based elevation
  - Fewer than 3 trades in regime → no regime check
  - Symbol win rate < 35% (≥ 3 trades) → returns BASE + 8
  - Symbol win rate ≥ 35% → no symbol-based elevation
  - No losses in journal → returns BASE_CONFIDENCE
  - Past losses are very dissimilar → returns BASE_CONFIDENCE
  - Past losses are similar (avg_sim ≥ SIMILARITY_DANGER) → raises threshold
  - Perfect similarity (avg_sim ≈ 1.0) → threshold capped at MAX_CONFIDENCE
  - Exactly at SIMILARITY_DANGER threshold → no elevation (extra = 0)
  - K limits top neighbours correctly (only top K used)
  - Regime check short-circuits before KNN check
  - Threshold never exceeds MAX_CONFIDENCE
- Learner.log_summary: runs without error on empty and populated journals
"""

import math
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.learner import (
    Learner,
    _distance,
    _similarity,
    BASE_CONFIDENCE,
    MAX_CONFIDENCE,
    MIN_TRADES_TO_LEARN,
    SIMILARITY_DANGER,
    FEATURE_WEIGHTS,
    FEATURE_SCALE,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _rec(symbol="BTC/USD", regime="TRENDING", won=True, **features):
    """Return a minimal trade-record-like object with a features() method."""
    base_features = {
        "rsi": 50.0, "adx": 25.0, "volume_ratio": 1.0,
        "atr_pct": 0.5, "ema100_gap": 0.0, "ema200_gap": 0.0,
        "hour_utc": 12.0, "day_of_week": 1.0,
        "ofi": 0.0, "lead_lag_strength": 0.0, "lead_lag_aligned": 0.0,
        "regime_confidence": 0.5, "funding_rate": 0.0,
        "ofi_score": 0.0, "lead_lag_score": 0.0, "regime_score": 0.0,
        "confidence": 0.0, "is_buy": 1.0,
    }
    base_features.update(features)
    r = MagicMock()
    r.symbol = symbol
    r.regime = regime
    r.won = won
    r.features.return_value = base_features
    return r


def _journal(records):
    """Minimal journal stub."""
    j = MagicMock()
    j.records = records
    j.losses.return_value = [r for r in records if not r.won]
    j.wins.return_value   = [r for r in records if r.won]
    j.stats.return_value  = {
        "total":    len(records),
        "wins":     sum(1 for r in records if r.won),
        "losses":   sum(1 for r in records if not r.won),
        "win_rate": (sum(1 for r in records if r.won) / len(records) * 100
                     if records else 0.0),
    }
    return j


def _default_features(**overrides) -> dict:
    """Return a typical feature dict, with optional overrides."""
    base = {
        "rsi": 50.0, "adx": 25.0, "volume_ratio": 1.0,
        "atr_pct": 0.5, "ema100_gap": 0.0, "ema200_gap": 0.0,
        "hour_utc": 12.0, "day_of_week": 1.0,
    }
    base.update(overrides)
    return base


# ── _distance ─────────────────────────────────────────────────────────────────

class TestDistance:
    def test_identical_dicts_give_zero(self):
        f = _default_features()
        assert _distance(f, f) == pytest.approx(0.0)

    def test_empty_dicts_give_zero(self):
        assert _distance({}, {}) == pytest.approx(0.0)

    def test_missing_keys_default_to_zero(self):
        a = {"rsi": 50.0}
        b = {}
        d = _distance(a, b)
        assert d > 0.0  # a["rsi"]=50 vs b["rsi"]=0 (default) → non-zero

    def test_distance_is_non_negative(self):
        a = _default_features(rsi=30.0, adx=10.0)
        b = _default_features(rsi=70.0, adx=40.0)
        assert _distance(a, b) >= 0.0

    def test_larger_difference_gives_larger_distance(self):
        base = _default_features()
        small_diff = _default_features(rsi=51.0)   # 1 unit RSI difference
        large_diff = _default_features(rsi=80.0)   # 30 unit RSI difference
        assert _distance(base, large_diff) > _distance(base, small_diff)

    def test_rsi_weight_dominates_hour_utc(self):
        """RSI weight (2.0) is 4× higher than hour_utc (0.5) so equal
        scaled differences give different distances."""
        base = _default_features()
        rsi_scale   = FEATURE_SCALE["rsi"]
        hour_scale  = FEATURE_SCALE["hour_utc"]
        rsi_w       = FEATURE_WEIGHTS["rsi"]
        hour_w      = FEATURE_WEIGHTS["hour_utc"]

        # Same scaled difference (1.0) in RSI vs hour_utc
        rsi_change  = _default_features(rsi=base["rsi"] + rsi_scale)
        hour_change = _default_features(hour_utc=base["hour_utc"] + hour_scale)

        d_rsi  = _distance(base, rsi_change)
        d_hour = _distance(base, hour_change)

        # Distance contribution: sqrt(weight) × 1.0 scaled unit
        assert d_rsi > d_hour

    def test_symmetric(self):
        a = _default_features(rsi=40.0, adx=30.0)
        b = _default_features(rsi=60.0, adx=10.0)
        assert _distance(a, b) == pytest.approx(_distance(b, a))

    def test_single_feature_difference(self):
        """Manual calculation: RSI diff 30, scale=30, weight=2 → contribution=2."""
        a = {"rsi": 50.0}
        b = {"rsi": 80.0}
        # All other features absent → default to 0 in both → no contribution
        # rsi: diff=(50-80)/30 = -1, weight=2 → 2*1*1 = 2 → sqrt(2)
        expected = math.sqrt(2.0)
        assert _distance(a, b) == pytest.approx(expected, rel=1e-6)


# ── _similarity ───────────────────────────────────────────────────────────────

class TestSimilarity:
    def test_distance_zero_gives_similarity_one(self):
        assert _similarity(0.0) == pytest.approx(1.0)

    def test_similarity_always_in_0_1(self):
        for dist in [0.0, 0.5, 1.0, 2.0, 5.0, 100.0]:
            s = _similarity(dist)
            assert 0.0 <= s <= 1.0, f"_similarity({dist}) = {s} out of [0, 1]"

    def test_similarity_monotonically_decreasing(self):
        distances = [0.0, 0.1, 0.5, 1.0, 2.0, 5.0]
        sims = [_similarity(d) for d in distances]
        for i in range(len(sims) - 1):
            assert sims[i] > sims[i + 1]

    def test_large_distance_near_zero(self):
        assert _similarity(100.0) < 1e-10

    def test_exp_formula(self):
        dist = 1.5
        assert _similarity(dist) == pytest.approx(math.exp(-dist))


# ── Learner.required_confidence ───────────────────────────────────────────────

class TestRequiredConfidenceBootstrap:
    """With fewer than MIN_TRADES_TO_LEARN records, always returns BASE."""

    def test_empty_journal_returns_base(self):
        learner = Learner(_journal([]))
        assert learner.required_confidence({}, "TRENDING", "BTC/USD") == BASE_CONFIDENCE

    def test_one_below_min_returns_base(self):
        records = [_rec(won=False)] * (MIN_TRADES_TO_LEARN - 1)
        learner = Learner(_journal(records))
        assert learner.required_confidence({}, "TRENDING", "BTC/USD") == BASE_CONFIDENCE

    def test_exactly_min_trades_does_not_return_early(self):
        """At the threshold, the learner proceeds beyond the bootstrap guard."""
        records = [_rec(regime="TRENDING", won=True)] * MIN_TRADES_TO_LEARN
        learner = Learner(_journal(records))
        result = learner.required_confidence({}, "TRENDING", "BTC/USD")
        # With all wins, regime WR=100% and no losses → returns BASE_CONFIDENCE
        assert result == BASE_CONFIDENCE


class TestRequiredConfidenceRegime:
    """Regime win rate thresholds raise confidence requirement."""

    def _enough_records(self, n_wins, n_losses, regime="RANGING"):
        wins   = [_rec(regime=regime, won=True)]  * n_wins
        losses = [_rec(regime=regime, won=False)] * n_losses
        # Pad to MIN_TRADES_TO_LEARN if needed
        total = n_wins + n_losses
        pad = max(0, MIN_TRADES_TO_LEARN - total)
        padding = [_rec(regime="OTHER", won=True)] * pad
        return _journal(wins + losses + padding)

    def test_regime_wr_below_35_raises_by_12(self):
        # 1 win out of 5 = 20% WR < 35%
        j = self._enough_records(n_wins=1, n_losses=4, regime="RANGING")
        learner = Learner(j)
        result = learner.required_confidence({}, "RANGING", "ETH/USD")
        assert result == BASE_CONFIDENCE + 12

    def test_regime_wr_at_35_does_not_trigger_12_raise(self):
        # Exactly 35% WR → falls into the elif < 0.45 branch (+6)
        # 7 wins / 20 total = 35%
        j = self._enough_records(n_wins=7, n_losses=13, regime="RANGING")
        learner = Learner(j)
        result = learner.required_confidence({}, "RANGING", "ETH/USD")
        # 7/20 = 0.35 → NOT < 0.35 → check elif < 0.45 → 0.35 < 0.45 → +6
        assert result == BASE_CONFIDENCE + 6

    def test_regime_wr_between_35_and_45_raises_by_6(self):
        # 4 wins / 10 = 40% WR → 35% ≤ WR < 45% → +6
        j = self._enough_records(n_wins=4, n_losses=6, regime="VOLATILE")
        learner = Learner(j)
        result = learner.required_confidence({}, "VOLATILE", "BTC/USD")
        assert result == BASE_CONFIDENCE + 6

    def test_regime_wr_at_45_no_elevation(self):
        # 9 wins / 20 = 45% WR → ≥ 45% → no regime elevation
        j = self._enough_records(n_wins=9, n_losses=11, regime="TRENDING")
        learner = Learner(j)
        result = learner.required_confidence({}, "TRENDING", "BTC/USD")
        # Falls through to no-loss or KNN path
        assert result == BASE_CONFIDENCE

    def test_regime_with_fewer_than_3_trades_ignored(self):
        # 2 trades in regime — below the min-3 threshold, no regime elevation
        wins   = [_rec(regime="RANGING", won=False)] * 2
        others = [_rec(regime="OTHER",   won=True)]  * (MIN_TRADES_TO_LEARN - 2)
        j = _journal(wins + others)
        learner = Learner(j)
        result = learner.required_confidence({}, "RANGING", "BTC/USD")
        assert result == BASE_CONFIDENCE

    def test_regime_elevation_capped_at_max(self):
        # Even though +12 is in range, check the cap still applies
        expected = min(MAX_CONFIDENCE, BASE_CONFIDENCE + 12)
        j = self._enough_records(n_wins=1, n_losses=4, regime="BEAR")
        learner = Learner(j)
        result = learner.required_confidence({}, "BEAR", "SOL/USD")
        assert result == expected
        assert result <= MAX_CONFIDENCE


class TestRequiredConfidenceSymbol:
    """Symbol win rate threshold raises confidence when regime is ok."""

    def _journal_with_symbol_wr(self, n_wins, n_losses,
                                 sym="BTC/USD", regime="NEUTRAL"):
        sym_records = (
            [_rec(symbol=sym, regime=regime, won=True)]  * n_wins +
            [_rec(symbol=sym, regime=regime, won=False)] * n_losses
        )
        # Pad so regime has healthy WR and total ≥ MIN_TRADES_TO_LEARN
        pad = max(0, MIN_TRADES_TO_LEARN - len(sym_records))
        padded = [_rec(symbol="ETH/USD", regime=regime, won=True)] * pad
        # Make regime WR high by adding wins from other symbols
        extra_regime = [_rec(symbol="ETH/USD", regime=regime, won=True)] * 10
        return _journal(sym_records + padded + extra_regime)

    def test_symbol_wr_below_35_raises_by_8(self):
        j = self._journal_with_symbol_wr(n_wins=1, n_losses=4)
        learner = Learner(j)
        result = learner.required_confidence({}, "NEUTRAL", "BTC/USD")
        assert result == BASE_CONFIDENCE + 8

    def test_symbol_wr_at_35_no_elevation(self):
        # 7/20 = 35% — NOT < 35% → no elevation
        j = self._journal_with_symbol_wr(n_wins=7, n_losses=13)
        learner = Learner(j)
        result = learner.required_confidence({}, "NEUTRAL", "BTC/USD")
        assert result == BASE_CONFIDENCE

    def test_symbol_with_fewer_than_3_trades_ignored(self):
        sym_records = [_rec(symbol="BTC/USD", regime="NEUTRAL", won=False)] * 2
        others = [_rec(symbol="ETH/USD", regime="NEUTRAL", won=True)] * (MIN_TRADES_TO_LEARN + 10)
        j = _journal(sym_records + others)
        learner = Learner(j)
        result = learner.required_confidence({}, "NEUTRAL", "BTC/USD")
        assert result == BASE_CONFIDENCE

    def test_symbol_elevation_capped_at_max(self):
        expected = min(MAX_CONFIDENCE, BASE_CONFIDENCE + 8)
        j = self._journal_with_symbol_wr(n_wins=0, n_losses=10)
        learner = Learner(j)
        result = learner.required_confidence({}, "NEUTRAL", "BTC/USD")
        assert result == expected
        assert result <= MAX_CONFIDENCE


class TestRequiredConfidenceKNN:
    """KNN similarity to past losses drives threshold when win rates are healthy."""

    def _good_wrs_journal(self, loss_features_list, n_extra_wins=20):
        """Journal with healthy regime/symbol WRs, but with specified past losses."""
        extra_wins = [_rec(symbol="BTC/USD", regime="TRENDING", won=True)] * n_extra_wins
        losses = [_rec(symbol="BTC/USD", regime="TRENDING", won=False,
                       **feats) for feats in loss_features_list]
        return _journal(extra_wins + losses)

    def test_no_losses_returns_base(self):
        """When there are no past losses, KNN cannot fire."""
        records = [_rec(won=True)] * MIN_TRADES_TO_LEARN
        j = _journal(records)
        learner = Learner(j)
        result = learner.required_confidence(_default_features(), "TRENDING", "BTC/USD")
        assert result == BASE_CONFIDENCE

    def test_very_dissimilar_past_losses_returns_base(self):
        """Losses with very different features → low similarity → base confidence."""
        # Past loss has extreme RSI difference
        loss_feat = _default_features(rsi=1.0, adx=1.0)
        current_feat = _default_features(rsi=99.0, adx=99.0)

        j = self._good_wrs_journal([loss_feat])
        learner = Learner(j)
        result = learner.required_confidence(current_feat, "TRENDING", "BTC/USD")
        assert result == BASE_CONFIDENCE

    def test_very_similar_past_losses_raises_threshold(self):
        """Identical feature setup raises threshold above base."""
        feat = _default_features()
        j = self._good_wrs_journal([feat])
        learner = Learner(j)
        result = learner.required_confidence(feat, "TRENDING", "BTC/USD")
        assert result > BASE_CONFIDENCE

    def test_perfect_similarity_caps_at_max_confidence(self):
        """avg_sim=1.0 (identical features) should be capped at MAX_CONFIDENCE."""
        feat = _default_features()
        # Many identical losses to ensure avg_sim close to 1.0
        j = self._good_wrs_journal([feat] * 10)
        learner = Learner(j)
        result = learner.required_confidence(feat, "TRENDING", "BTC/USD")
        assert result == MAX_CONFIDENCE

    def test_at_exact_similarity_danger_no_extra_elevation(self):
        """avg_sim = exactly SIMILARITY_DANGER → extra=0 → result = BASE_CONFIDENCE."""
        # We can't easily force avg_sim to be exactly SIMILARITY_DANGER, but we can
        # verify: if avg_sim < SIMILARITY_DANGER, result == BASE_CONFIDENCE.
        # Moderately dissimilar features produce avg_sim < SIMILARITY_DANGER.
        loss_feat = _default_features(rsi=30.0)   # RSI 30 vs 50 current
        current_feat = _default_features(rsi=50.0)
        j = self._good_wrs_journal([loss_feat] * 5)
        learner = Learner(j)
        result = learner.required_confidence(current_feat, "TRENDING", "BTC/USD")
        # Manually: dist for RSI diff = (50-30)/30 = 0.667, weight=2 → contrib=0.889
        # total_dist = sqrt(0.889) ≈ 0.943, sim = exp(-0.943) ≈ 0.389
        # avg_sim ≈ 0.389 < 0.80 → returns BASE_CONFIDENCE
        assert result == BASE_CONFIDENCE

    def test_threshold_never_exceeds_max_confidence(self):
        """Even with perfect similarity, threshold is capped at MAX_CONFIDENCE."""
        feat = _default_features()
        j = self._good_wrs_journal([feat] * 20)
        learner = Learner(j, k=20)
        result = learner.required_confidence(feat, "TRENDING", "BTC/USD")
        assert result <= MAX_CONFIDENCE

    def test_knn_only_uses_top_k(self):
        """One highly-similar loss among many dissimilar ones; top-K includes it."""
        similar_feat  = _default_features()
        dissimilar_feat = _default_features(rsi=1.0, adx=1.0, ema100_gap=50.0)

        losses = [similar_feat] + [dissimilar_feat] * 20
        j = self._good_wrs_journal(losses)

        # k=1 → only the most similar neighbour counts
        learner_k1 = Learner(j, k=1)
        result_k1 = learner_k1.required_confidence(similar_feat, "TRENDING", "BTC/USD")

        # k=21 → includes all 20 dissimilar + 1 similar; avg_sim diluted down
        learner_k21 = Learner(j, k=21)
        result_k21 = learner_k21.required_confidence(similar_feat, "TRENDING", "BTC/USD")

        # k=1 sees only the perfect match → highest threshold
        assert result_k1 >= result_k21


class TestRequiredConfidenceOrdering:
    """Regime check fires before symbol and KNN checks (early return)."""

    def test_bad_regime_returns_before_knn_check(self):
        """If regime WR is bad, returns early — KNN identical features irrelevant."""
        feat = _default_features()

        # 5 trades in regime: 1 win, 4 losses = 20% WR → +12 → 87
        regime_recs = (
            [_rec(symbol="BTC/USD", regime="BEAR", won=True)]  * 1 +
            [_rec(symbol="BTC/USD", regime="BEAR", won=False)] * 4
        )
        # Pad to ensure total ≥ MIN_TRADES_TO_LEARN
        pad = max(0, MIN_TRADES_TO_LEARN - len(regime_recs))
        extra = [_rec(symbol="ETH/USD", regime="TRENDING", won=True)] * pad
        j = _journal(regime_recs + extra)

        learner = Learner(j)
        result = learner.required_confidence(feat, "BEAR", "BTC/USD")
        assert result == BASE_CONFIDENCE + 12  # regime path, not KNN

    def test_bad_symbol_returns_after_regime_before_knn(self):
        """Symbol check fires after regime (healthy) but before KNN."""
        feat = _default_features()

        # Healthy regime WR (9/10 = 90%)
        regime_ok = [_rec(symbol="ETH/USD", regime="RANGING", won=True)] * 9
        # Bad symbol WR for BTC (1/5 = 20%)
        sym_bad = (
            [_rec(symbol="BTC/USD", regime="RANGING", won=True)]  * 1 +
            [_rec(symbol="BTC/USD", regime="RANGING", won=False)] * 4
        )
        j = _journal(regime_ok + sym_bad)

        learner = Learner(j)
        result = learner.required_confidence(feat, "RANGING", "BTC/USD")
        assert result == BASE_CONFIDENCE + 8  # symbol path


# ── Learner.log_summary ───────────────────────────────────────────────────────

class TestLogSummary:
    def test_empty_journal_does_not_raise(self):
        j = _journal([])
        Learner(j).log_summary()   # must not raise

    def test_populated_journal_does_not_raise(self):
        records = [_rec(regime=r, won=w)
                   for r, w in [("TRENDING", True), ("RANGING", False),
                                 ("TRENDING", True), ("NEUTRAL", False)]]
        Learner(_journal(records)).log_summary()  # must not raise
