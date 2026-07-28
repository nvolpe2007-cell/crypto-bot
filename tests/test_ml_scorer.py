"""
Unit tests for src/ml_scorer.py

Covers:
- _record_to_vec: length, correct value extraction, regime encoding, direction flag
- _features_to_vec: length, defaults for missing keys, regime encoding, bool coercion
- FEATURE_NAMES length matches both vec functions
- MLScorer.__init__: handles missing model file (no crash, model=None)
- MLScorer.__init__: handles corrupt model file (no crash, model=None)
- MLScorer.should_retrain: False when fewer than MIN_TRADES records
- MLScorer.should_retrain: False when enough records but interval not met
- MLScorer.should_retrain: True when records >= MIN_TRADES and gap >= RETRAIN_INTERVAL
- MLScorer.predict_win_prob: returns None when model not loaded
- MLScorer.blend_confidence: returns rule_confidence unchanged when no model
- MLScorer.blend_confidence: blends 55/45 when model present
- MLScorer.blend_confidence: clips result to [0, 100]
- MLScorer._save/_load: round-trips model + scaler + n_trades
- MLScorer._save: silent failure on bad path (no crash)
- MLScorer.train: returns False when fewer than MIN_TRADES records
- MLScorer.train: returns False on ImportError (xgboost absent)
- MLScorer.train: successful training sets model and updates _n_at_last_train
- MLScorer.train: handles training exception gracefully
"""

import os
import pickle
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest

import src.ml_scorer as _ml_module
from src.ml_scorer import (
    MLScorer,
    FEATURE_NAMES,
    MIN_TRADES,
    RETRAIN_INTERVAL,
    _REGIME_ENC,
    _record_to_vec,
    _features_to_vec,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class _PicklableModel:
    """Minimal picklable stand-in for an XGBClassifier (for disk-I/O tests)."""
    feature_importances_ = np.ones(len(FEATURE_NAMES)) / len(FEATURE_NAMES)

    def fit(self, X, y, **kwargs):
        return self

    def predict_proba(self, X):
        return np.array([[0.3, 0.7]] * len(X))


class _PicklableScaler:
    """Minimal picklable stand-in for a StandardScaler (for disk-I/O tests)."""
    def transform(self, X):
        return np.zeros_like(np.array(X), dtype=float)

    def fit_transform(self, X):
        return np.zeros_like(np.array(X), dtype=float)


def _rec(**overrides):
    """Minimal TradeRecord-like object with all fields ml_scorer reads."""
    defaults = dict(
        rsi=55.0, adx=28.0, volume_ratio=1.2, atr_pct=0.5,
        ema100_gap=0.3, ema200_gap=0.8,
        hour_utc=12, day_of_week=2,
        ofi=0.05, lead_lag_strength=0.02, lead_lag_aligned=True,
        regime="TRENDING_UP", regime_confidence=0.8,
        funding_rate=0.0003,
        ofi_score=10.0, lead_lag_score=8.0, regime_score=15.0,
        confidence=65.0,
        direction="buy",
        won=True,
    )
    defaults.update(overrides)
    r = SimpleNamespace(**defaults)
    return r


def _make_journal(n_records=0, won_pattern=None):
    """Return a mock journal whose .records list has n_records entries."""
    journal = MagicMock()
    if won_pattern is None:
        won_pattern = [i % 2 == 0 for i in range(n_records)]
    journal.records = [
        _rec(won=won_pattern[i] if i < len(won_pattern) else True)
        for i in range(n_records)
    ]
    return journal


def _make_scorer(tmp_path, n_records=0):
    """Construct an MLScorer backed by an empty temp model file dir."""
    journal = _make_journal(n_records)
    with patch.object(_ml_module, "MODEL_FILE", str(tmp_path / "ml_model.pkl")):
        scorer = MLScorer(journal)
    return scorer, journal


# ── _record_to_vec ────────────────────────────────────────────────────────────

class TestRecordToVec:
    def test_length_matches_feature_names(self):
        vec = _record_to_vec(_rec())
        assert len(vec) == len(FEATURE_NAMES)

    def test_basic_values_extracted(self):
        r = _rec(rsi=42.0, adx=30.0, direction="sell")
        vec = _record_to_vec(r)
        assert vec[0] == 42.0       # rsi
        assert vec[1] == 30.0       # adx
        assert vec[-1] == 0.0       # is_buy: direction=='sell' → 0

    def test_buy_direction_is_one(self):
        vec = _record_to_vec(_rec(direction="buy"))
        assert vec[-1] == 1.0

    def test_regime_encoded_correctly(self):
        for regime, expected in _REGIME_ENC.items():
            vec = _record_to_vec(_rec(regime=regime))
            assert vec[FEATURE_NAMES.index("regime_encoded")] == float(expected)

    def test_unknown_regime_defaults_to_zero(self):
        vec = _record_to_vec(_rec(regime="XYZZY"))
        assert vec[FEATURE_NAMES.index("regime_encoded")] == 0.0

    def test_lead_lag_aligned_bool_to_float(self):
        vec_true  = _record_to_vec(_rec(lead_lag_aligned=True))
        vec_false = _record_to_vec(_rec(lead_lag_aligned=False))
        assert vec_true[FEATURE_NAMES.index("lead_lag_aligned")]  == 1.0
        assert vec_false[FEATURE_NAMES.index("lead_lag_aligned")] == 0.0

    def test_none_ofi_becomes_zero(self):
        vec = _record_to_vec(_rec(ofi=None))
        assert vec[FEATURE_NAMES.index("ofi")] == 0.0

    def test_none_lead_lag_strength_becomes_zero(self):
        vec = _record_to_vec(_rec(lead_lag_strength=None))
        assert vec[FEATURE_NAMES.index("lead_lag_strength")] == 0.0

    def test_all_values_are_numeric(self):
        vec = _record_to_vec(_rec())
        for v in vec:
            assert isinstance(v, (int, float)), f"non-numeric: {v}"


# ── _features_to_vec ─────────────────────────────────────────────────────────

class TestFeaturesToVec:
    def test_length_matches_feature_names(self):
        vec = _features_to_vec({})
        assert len(vec) == len(FEATURE_NAMES)

    def test_defaults_for_all_missing_keys(self):
        vec = _features_to_vec({})
        assert vec[FEATURE_NAMES.index("rsi")]          == 50.0
        assert vec[FEATURE_NAMES.index("adx")]          == 20.0
        assert vec[FEATURE_NAMES.index("volume_ratio")] == 1.0
        assert vec[FEATURE_NAMES.index("atr_pct")]      == 1.0

    def test_provided_values_overrides_defaults(self):
        vec = _features_to_vec({"rsi": 70.0, "adx": 35.0})
        assert vec[FEATURE_NAMES.index("rsi")] == 70.0
        assert vec[FEATURE_NAMES.index("adx")] == 35.0

    def test_regime_encoded_correctly(self):
        for regime, expected in _REGIME_ENC.items():
            vec = _features_to_vec({"regime": regime})
            assert vec[FEATURE_NAMES.index("regime_encoded")] == float(expected)

    def test_unknown_regime_defaults_to_zero(self):
        vec = _features_to_vec({"regime": "XYZZY"})
        assert vec[FEATURE_NAMES.index("regime_encoded")] == 0.0

    def test_is_buy_true_by_default(self):
        vec = _features_to_vec({})
        assert vec[FEATURE_NAMES.index("is_buy")] == 1.0

    def test_is_buy_false_when_set(self):
        vec = _features_to_vec({"is_buy": False})
        assert vec[FEATURE_NAMES.index("is_buy")] == 0.0

    def test_none_ofi_falls_back_to_zero(self):
        vec = _features_to_vec({"ofi": None})
        assert vec[FEATURE_NAMES.index("ofi")] == 0.0

    def test_all_values_are_numeric(self):
        vec = _features_to_vec({
            "rsi": 60.0, "regime": "RANGING", "lead_lag_aligned": True,
            "is_buy": True, "ofi": None,
        })
        for v in vec:
            assert isinstance(v, (int, float)), f"non-numeric: {v!r}"


# ── MLScorer init / file handling ─────────────────────────────────────────────

class TestMLScorerInit:
    def test_no_model_file_starts_clean(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        assert scorer._model is None
        assert scorer._scaler is None
        assert scorer._n_at_last_train == 0

    def test_corrupt_model_file_starts_clean(self, tmp_path):
        model_path = tmp_path / "ml_model.pkl"
        model_path.write_bytes(b"not a valid pickle at all !!!")
        journal = _make_journal(0)
        with patch.object(_ml_module, "MODEL_FILE", str(model_path)):
            scorer = MLScorer(journal)
        assert scorer._model is None

    def test_valid_model_file_loads_correctly(self, tmp_path):
        model_path = tmp_path / "ml_model.pkl"
        fake_model  = _PicklableModel()
        fake_scaler = _PicklableScaler()
        with open(model_path, "wb") as f:
            pickle.dump({"model": fake_model, "scaler": fake_scaler, "n_trades": 40}, f)
        journal = _make_journal(0)
        with patch.object(_ml_module, "MODEL_FILE", str(model_path)):
            scorer = MLScorer(journal)
        assert isinstance(scorer._model, _PicklableModel)
        assert isinstance(scorer._scaler, _PicklableScaler)
        assert scorer._n_at_last_train == 40

    def test_save_then_load_roundtrip(self, tmp_path):
        model_path = str(tmp_path / "ml_model.pkl")
        journal = _make_journal(0)
        with patch.object(_ml_module, "MODEL_FILE", model_path):
            scorer = MLScorer(journal)
            scorer._model = _PicklableModel()
            scorer._scaler = _PicklableScaler()
            scorer._n_at_last_train = 55
            scorer._save()
            # Fresh instance loads it back
            scorer2 = MLScorer(journal)
        assert scorer2._n_at_last_train == 55
        assert scorer2._model is not None
        assert scorer2._scaler is not None

    def test_save_silent_on_bad_path(self, tmp_path):
        journal = _make_journal(0)
        with patch.object(_ml_module, "MODEL_FILE", "/nonexistent/deep/path/model.pkl"):
            scorer = MLScorer(journal)
            scorer._model = MagicMock()
            scorer._save()   # must not raise


# ── should_retrain ────────────────────────────────────────────────────────────

class TestShouldRetrain:
    def test_false_below_min_trades(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES - 1)
        assert scorer.should_retrain() is False

    def test_false_exactly_at_min_but_no_interval_gap(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES)
        scorer._n_at_last_train = MIN_TRADES   # just trained at this count
        assert scorer.should_retrain() is False

    def test_true_when_gap_reaches_interval(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES + RETRAIN_INTERVAL)
        scorer._n_at_last_train = MIN_TRADES
        assert scorer.should_retrain() is True

    def test_false_when_one_short_of_interval(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES + RETRAIN_INTERVAL - 1)
        scorer._n_at_last_train = MIN_TRADES
        assert scorer.should_retrain() is False

    def test_true_when_never_trained_and_has_enough_records(self, tmp_path):
        # _n_at_last_train starts at 0; MIN_TRADES records → gap = MIN_TRADES >= RETRAIN_INTERVAL
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES)
        scorer._n_at_last_train = 0
        assert scorer.should_retrain() is True


# ── predict_win_prob ──────────────────────────────────────────────────────────

class TestPredictWinProb:
    def test_returns_none_when_no_model(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        assert scorer.predict_win_prob({"rsi": 55.0}) is None

    def test_returns_none_when_scaler_missing(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        scorer._model = MagicMock()
        scorer._scaler = None
        assert scorer.predict_win_prob({"rsi": 55.0}) is None

    def test_returns_float_when_model_available(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        fake_scaler = MagicMock()
        fake_scaler.transform.return_value = np.zeros((1, len(FEATURE_NAMES)))
        fake_model = MagicMock()
        fake_model.predict_proba.return_value = np.array([[0.3, 0.7]])
        scorer._model  = fake_model
        scorer._scaler = fake_scaler
        prob = scorer.predict_win_prob({"rsi": 55.0})
        assert prob == pytest.approx(0.7)

    def test_returns_none_on_prediction_exception(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        fake_scaler = MagicMock()
        fake_scaler.transform.side_effect = ValueError("bad input")
        scorer._model  = MagicMock()
        scorer._scaler = fake_scaler
        # Must not raise, must return None
        assert scorer.predict_win_prob({}) is None


# ── blend_confidence ──────────────────────────────────────────────────────────

class TestBlendConfidence:
    def test_passthrough_when_no_model(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        result = scorer.blend_confidence(72.0, {"rsi": 60.0})
        assert result == pytest.approx(72.0)

    def test_blends_55_45_when_model_present(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        # P(win) = 0.80 → ml_conf = 80.0
        # blended = 0.55 * 60 + 0.45 * 80 = 33 + 36 = 69
        fake_scaler = MagicMock()
        fake_scaler.transform.return_value = np.zeros((1, len(FEATURE_NAMES)))
        fake_model = MagicMock()
        fake_model.predict_proba.return_value = np.array([[0.2, 0.8]])
        scorer._model  = fake_model
        scorer._scaler = fake_scaler
        result = scorer.blend_confidence(60.0, {})
        assert result == pytest.approx(0.55 * 60.0 + 0.45 * 80.0)

    def test_clips_to_zero_floor(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        fake_scaler = MagicMock()
        fake_scaler.transform.return_value = np.zeros((1, len(FEATURE_NAMES)))
        fake_model = MagicMock()
        fake_model.predict_proba.return_value = np.array([[1.0, 0.0]])  # P(win)=0
        scorer._model  = fake_model
        scorer._scaler = fake_scaler
        result = scorer.blend_confidence(0.0, {})
        assert result >= 0.0

    def test_clips_to_hundred_ceiling(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        fake_scaler = MagicMock()
        fake_scaler.transform.return_value = np.zeros((1, len(FEATURE_NAMES)))
        fake_model = MagicMock()
        fake_model.predict_proba.return_value = np.array([[0.0, 1.0]])  # P(win)=1
        scorer._model  = fake_model
        scorer._scaler = fake_scaler
        result = scorer.blend_confidence(100.0, {})
        assert result <= 100.0

    def test_passthrough_when_predict_returns_none(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path)
        # Scaler raises so predict_win_prob returns None
        fake_scaler = MagicMock()
        fake_scaler.transform.side_effect = RuntimeError("boom")
        scorer._model  = MagicMock()
        scorer._scaler = fake_scaler
        result = scorer.blend_confidence(55.0, {})
        assert result == pytest.approx(55.0)


# ── train ─────────────────────────────────────────────────────────────────────

class TestTrain:
    def test_returns_false_below_min_trades(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES - 1)
        assert scorer.train() is False
        assert scorer._model is None

    def test_returns_false_on_import_error(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES)
        # Simulate xgboost not installed
        with patch.dict(sys.modules, {"xgboost": None}):
            result = scorer.train()
        assert result is False
        assert scorer._model is None

    def test_returns_false_on_unexpected_exception(self, tmp_path):
        scorer, _ = _make_scorer(tmp_path, n_records=MIN_TRADES)
        broken_module = MagicMock()
        broken_module.XGBClassifier.side_effect = RuntimeError("GPU exploded")
        with patch.dict(sys.modules, {"xgboost": broken_module}):
            result = scorer.train()
        assert result is False

    def test_successful_training_sets_model(self, tmp_path):
        model_path = str(tmp_path / "ml_model.pkl")
        scorer, journal = _make_scorer(tmp_path, n_records=MIN_TRADES)
        journal.records = [_rec(won=(i % 3 != 0)) for i in range(MIN_TRADES)]

        fake_model = _PicklableModel()
        fake_xgb = MagicMock()
        fake_xgb.XGBClassifier.return_value = fake_model

        fake_scaler = _PicklableScaler()
        fake_sklearn_preprocessing = MagicMock()
        fake_sklearn_preprocessing.StandardScaler.return_value = fake_scaler

        fake_sklearn_model_selection = MagicMock()

        with patch.dict(sys.modules, {
            "xgboost": fake_xgb,
            "sklearn.preprocessing": fake_sklearn_preprocessing,
            "sklearn.model_selection": fake_sklearn_model_selection,
        }):
            with patch.object(_ml_module, "MODEL_FILE", model_path):
                result = scorer.train()

        assert result is True
        assert scorer._model is fake_model
        assert scorer._n_at_last_train == MIN_TRADES

    def test_model_persisted_to_disk_after_training(self, tmp_path):
        model_path = str(tmp_path / "ml_model.pkl")
        scorer, journal = _make_scorer(tmp_path, n_records=MIN_TRADES)
        journal.records = [_rec(won=(i % 3 != 0)) for i in range(MIN_TRADES)]

        # Use picklable stubs so _save() actually succeeds
        fake_model = _PicklableModel()
        fake_scaler = _PicklableScaler()

        fake_xgb = MagicMock()
        fake_xgb.XGBClassifier.return_value = fake_model

        fake_sklearn_preprocessing = MagicMock()
        fake_sklearn_preprocessing.StandardScaler.return_value = fake_scaler

        fake_sklearn_model_selection = MagicMock()

        with patch.dict(sys.modules, {
            "xgboost": fake_xgb,
            "sklearn.preprocessing": fake_sklearn_preprocessing,
            "sklearn.model_selection": fake_sklearn_model_selection,
        }):
            with patch.object(_ml_module, "MODEL_FILE", model_path):
                scorer.train()

        assert os.path.exists(model_path)
        with open(model_path, "rb") as f:
            saved = pickle.load(f)
        assert "model" in saved
        assert "scaler" in saved
        assert saved["n_trades"] == MIN_TRADES
