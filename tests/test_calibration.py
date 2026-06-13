"""
Unit tests for src/calibration.py (ProbabilityCalibrator).

ProbabilityCalibrator sits on the hot path: probability_gate.py routes its
reject threshold and Kelly sizing through `calibrate(combined_p)` for every
candidate trade, so a bug here (e.g. clipping wrong, shrink miscomputed,
stale-model records leaking into the fit) silently mis-sizes every live
position. The module previously had a `--selftest` CLI but no pytest
coverage.

Covers:
  - identity passthrough while inactive (no file, too few samples, <2 unique x)
  - fit(): isotonic monotonicity, over-confidence pulled toward truth, shrink
  - calibrate(): blend formula and _P_FLOOR / _P_CEIL clipping
  - _brier / _ece / _reliability_bins helpers
  - fit_from_journal(): MIN_MODEL_VERSION filtering, zero/None prob_win exclusion
  - maybe_refit(): growth-gated refit
  - save()/load(): persistence round trip, missing/corrupt file handling
"""

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from src.calibration import (
    ProbabilityCalibrator,
    MIN_MODEL_VERSION,
    _P_FLOOR,
    _P_CEIL,
    _brier,
    _ece,
    _reliability_bins,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _miscalibrated_samples(n=400, seed=0, overconfidence=0.10):
    """raw_p ~ U(0.5, 0.8); true win rate = raw_p - overconfidence.

    Mirrors src/calibration.py's own `_selftest`: the source is over-confident,
    so a correct fit should pull predictions down toward the true rate.
    """
    rng = np.random.default_rng(seed)
    raw = rng.uniform(0.50, 0.80, size=n)
    won = rng.uniform(size=n) < np.clip(raw - overconfidence, 0.0, 1.0)
    return list(zip(raw.tolist(), won.tolist()))


def _journal(records):
    return SimpleNamespace(records=records)


def _record(prob_win, won, version=MIN_MODEL_VERSION):
    return SimpleNamespace(prob_win=prob_win, won=won, prob_model_version=version)


# ── inactive / identity passthrough ─────────────────────────────────────────

class TestInactiveIdentity:
    def test_fresh_instance_is_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        assert not cal.is_active

    def test_calibrate_is_identity_when_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        for p in (0.0, 0.3, 0.55, 0.97, 1.0):
            assert cal.calibrate(p) == p

    def test_load_missing_file_does_not_raise(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "does_not_exist.json"))
        assert not cal.is_active

    def test_load_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{not valid json")
        cal = ProbabilityCalibrator(path=str(path))
        assert not cal.is_active


# ── fit(): small-sample / degenerate guards ─────────────────────────────────

class TestFitSmallSampleGuards:
    def test_below_min_samples_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, path=str(tmp_path / "c.json"))
        samples = [(0.6, True)] * 10 + [(0.65, False)] * 10  # n=20 < 40
        report = cal.fit(samples)
        assert report.active is False
        assert report.n == 20
        assert not cal.is_active

    def test_zero_and_none_prob_excluded_from_n(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        samples = [(0.0, True), (None, False), (0.6, True), (0.7, False), (0.65, True)]
        report = cal.fit(samples)
        # Only the 3 samples with p > 0 count toward n
        assert report.n == 3

    def test_single_unique_value_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=10, path=str(tmp_path / "c.json"))
        samples = [(0.6, True)] * 6 + [(0.6, False)] * 6  # n=12 but only 1 unique x
        report = cal.fit(samples)
        assert report.active is False
        assert not cal.is_active


# ── fit(): real isotonic fit (active) ───────────────────────────────────────

class TestFitActive:
    def test_overconfident_input_pulled_down(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200,
                                     path=str(tmp_path / "c.json"))
        report = cal.fit(_miscalibrated_samples(400))
        assert report.active is True
        assert cal.is_active
        assert cal.calibrate(0.75) < 0.75

    def test_monotonic_over_grid(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200,
                                     path=str(tmp_path / "c.json"))
        cal.fit(_miscalibrated_samples(400))
        grid = np.linspace(0.5, 0.8, 25)
        out = [cal.calibrate(g) for g in grid]
        assert all(b >= a - 1e-9 for a, b in zip(out, out[1:]))

    def test_shrink_factor_scales_with_n_over_full_trust(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=400,
                                     path=str(tmp_path / "c.json"))
        cal.fit(_miscalibrated_samples(200))  # n == full_trust_n / 2
        assert cal._shrink == pytest.approx(0.5)

    def test_full_trust_when_n_exceeds_full_trust_n(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=100,
                                     path=str(tmp_path / "c.json"))
        cal.fit(_miscalibrated_samples(400))
        assert cal._shrink == 1.0

    def test_report_brier_improves(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200,
                                     path=str(tmp_path / "c.json"))
        report = cal.fit(_miscalibrated_samples(400))
        assert report.brier_cal <= report.brier_raw
        assert len(report.bins) > 0


# ── calibrate(): blend formula and clipping ─────────────────────────────────

class TestCalibrateBlendAndClipping:
    def test_blend_formula(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.3, 0.5, 0.7, 0.9])
        cal._y = np.array([0.4, 0.55, 0.65, 0.85])
        raw_p = 0.6
        cal_val = float(np.interp(raw_p, cal._x, cal._y))
        for shrink in (0.0, 0.25, 0.5, 1.0):
            cal._shrink = shrink
            expected = min(_P_CEIL, max(_P_FLOOR, shrink * cal_val + (1 - shrink) * raw_p))
            assert cal.calibrate(raw_p) == pytest.approx(expected)

    def test_output_clipped_to_ceiling(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.5, 0.6, 0.7])
        cal._y = np.array([0.98, 0.98, 0.98])
        cal._shrink = 1.0
        assert cal.calibrate(0.6) == _P_CEIL

    def test_output_clipped_to_floor(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.5, 0.6, 0.7])
        cal._y = np.array([0.02, 0.02, 0.02])
        cal._shrink = 1.0
        assert cal.calibrate(0.6) == _P_FLOOR

    def test_extrapolation_clamped_by_np_interp(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.5, 0.6, 0.7])
        cal._y = np.array([0.55, 0.60, 0.65])
        cal._shrink = 1.0
        # below the lowest knot -> clamps to y[0]
        assert cal.calibrate(0.1) == pytest.approx(0.55)
        # above the highest knot -> clamps to y[-1]
        assert cal.calibrate(0.99) == pytest.approx(0.65)


# ── _brier / _ece / _reliability_bins helpers ───────────────────────────────

class TestMetricHelpers:
    def test_brier_perfect_predictions(self):
        assert _brier(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == 0.0

    def test_brier_known_value(self):
        pred = np.array([0.5, 0.5])
        actual = np.array([1.0, 0.0])
        assert _brier(pred, actual) == pytest.approx(0.25)

    def test_brier_empty(self):
        assert _brier(np.array([]), np.array([])) == 0.0

    def test_ece_empty(self):
        assert _ece(np.array([]), np.array([])) == 0.0

    def test_ece_perfect_calibration_is_zero(self):
        pred = np.array([0.1, 0.1, 0.9, 0.9])
        actual = np.array([0.1, 0.1, 0.9, 0.9])
        assert _ece(pred, actual) == pytest.approx(0.0, abs=1e-9)

    def test_reliability_bins_basic(self):
        pred = np.array([0.1, 0.2, 0.8, 0.9])
        actual = np.array([0.0, 0.0, 1.0, 1.0])
        bins = _reliability_bins(pred, actual, n_bins=2)
        assert len(bins) == 2
        for _lo, _hi, n, _mp, _ma in bins:
            assert n == 2

    def test_reliability_bins_empty(self):
        assert _reliability_bins(np.array([]), np.array([])) == []


# ── fit_from_journal(): version filtering ───────────────────────────────────

class TestFitFromJournal:
    def test_filters_old_model_versions(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        old = [_record(0.6 + 0.001 * i, i % 2 == 0, version=MIN_MODEL_VERSION - 1)
               for i in range(50)]
        new = [_record(0.5 + 0.01 * (i % 10), i % 3 == 0, version=MIN_MODEL_VERSION)
               for i in range(10)]
        journal = _journal(old + new)
        report = cal.fit_from_journal(journal)
        # Only the 10 current-model-version records should be used
        assert report.n == 10

    def test_n_seen_tracks_total_records_including_excluded(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=100, path=str(tmp_path / "c.json"))
        old = [_record(0.6, True, version=MIN_MODEL_VERSION - 1) for _ in range(5)]
        new = [_record(0.6, True, version=MIN_MODEL_VERSION) for _ in range(3)]
        journal = _journal(old + new)
        cal.fit_from_journal(journal)
        assert cal._n_seen == len(old) + len(new)


# ── maybe_refit(): growth-gated refit ───────────────────────────────────────

class TestMaybeRefit:
    def test_no_refit_below_threshold(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=200, refit_every=10,
                                     path=str(tmp_path / "c.json"))
        journal = _journal([_record(0.6, True) for _ in range(5)])
        assert cal.maybe_refit(journal) is False
        assert cal._n_seen == 0

    def test_refit_triggers_after_growth(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=200, refit_every=10,
                                     path=str(tmp_path / "c.json"))
        journal = _journal([_record(0.6, True) for _ in range(10)])
        assert cal.maybe_refit(journal) is True
        assert cal._n_seen == 10
        # No further growth -> no second refit
        assert cal.maybe_refit(journal) is False


# ── save()/load(): persistence ──────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        path = str(tmp_path / "c.json")
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=path)
        cal.fit(_miscalibrated_samples(400))
        assert cal.is_active

        loaded = ProbabilityCalibrator(path=path)
        assert loaded.is_active
        assert loaded._n_fit == cal._n_fit
        assert loaded._shrink == pytest.approx(cal._shrink)
        np.testing.assert_allclose(loaded._x, cal._x)
        np.testing.assert_allclose(loaded._y, cal._y)
        for p in (0.5, 0.6, 0.75, 0.8):
            assert loaded.calibrate(p) == pytest.approx(cal.calibrate(p))

    def test_save_creates_parent_directories(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "c.json")
        cal = ProbabilityCalibrator(min_samples=5, path=path)
        cal.fit([(0.5, True), (0.6, False), (0.7, True), (0.55, False), (0.65, True)])
        assert os.path.exists(path)

    def test_inactive_save_persists_n_seen(self, tmp_path):
        path = str(tmp_path / "c.json")
        cal = ProbabilityCalibrator(min_samples=40, path=path)
        cal.fit([(0.6, True)] * 5)
        with open(path) as f:
            data = json.load(f)
        assert data['x'] is None
        assert data['y'] is None
        assert data['n_seen'] == 5


# ── CalibrationReport.render() ──────────────────────────────────────────────

class TestCalibrationReportRender:
    def test_render_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, path=str(tmp_path / "c.json"))
        report = cal.fit([(0.6, True)] * 5 + [(0.7, False)] * 5)
        text = report.render()
        assert "INACTIVE" in text
        assert str(report.n) in text

    def test_render_active(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200,
                                     path=str(tmp_path / "c.json"))
        report = cal.fit(_miscalibrated_samples(400))
        text = report.render()
        assert "ACTIVE" in text
        assert "Brier" in text
        assert "Reliability" in text
