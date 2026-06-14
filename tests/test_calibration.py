"""
Unit tests for src/calibration.py — ProbabilityCalibrator.

This module sits directly in the live decision pipeline: paper_trading.py
attaches a ProbabilityCalibrator to the ProbabilityGate, and every entry's
``calibrated_p`` (which drives the reject threshold and Kelly sizing) flows
through it. It had zero test coverage. This file covers:

  _brier / _ece / _reliability_bins: the diagnostic helpers
  ProbabilityCalibrator.fit():
    - inactive (identity) below min_samples
    - inactive when raw inputs lack variation
    - active + monotonic + shrink-weighted once enough varied data exists
    - over-confident sources get pulled toward the empirical rate
    - output always clamped to [_P_FLOOR, _P_CEIL]
  fit_from_journal(): filters by prob_model_version, _n_seen tracks ALL records
  maybe_refit(): only refits once the journal has grown by refit_every
  save()/load(): persistence round trip, and graceful handling of missing/
    corrupt files
  CalibrationReport.render(): inactive and active text
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from src.calibration import (
    MIN_MODEL_VERSION,
    CalibrationReport,
    ProbabilityCalibrator,
    _brier,
    _ece,
    _P_CEIL,
    _P_FLOOR,
    _reliability_bins,
)


def _rec(prob_win=0.0, won=False, version=MIN_MODEL_VERSION):
    return SimpleNamespace(prob_win=prob_win, won=won, prob_model_version=version)


def _journal(records):
    return SimpleNamespace(records=records)


def _synthetic_samples(n=400, lo=0.50, hi=0.80, bias=0.10, seed=0):
    """(raw_p, won) pairs where the source over-states P(win) by `bias`."""
    rng = np.random.default_rng(seed)
    raw = rng.uniform(lo, hi, size=n)
    won = rng.uniform(size=n) < np.clip(raw - bias, 0, 1)
    return list(zip(raw.tolist(), won.tolist()))


# ── helper functions ──────────────────────────────────────────────────────


class TestBrier:
    def test_empty(self):
        assert _brier(np.array([]), np.array([])) == 0.0

    def test_perfect_predictions(self):
        pred = np.array([1.0, 0.0, 1.0, 0.0])
        actual = np.array([1.0, 0.0, 1.0, 0.0])
        assert _brier(pred, actual) == 0.0

    def test_worst_case(self):
        pred = np.array([1.0, 0.0])
        actual = np.array([0.0, 1.0])
        assert _brier(pred, actual) == pytest.approx(1.0)


class TestECE:
    def test_empty(self):
        assert _ece(np.array([]), np.array([])) == 0.0

    def test_perfect_calibration_is_zero(self):
        # Every prediction equals the bin's actual mean → zero gap.
        pred = np.array([0.5, 0.5, 0.5, 0.5])
        actual = np.array([1.0, 0.0, 1.0, 0.0])
        assert _ece(pred, actual) == pytest.approx(0.0, abs=1e-9)

    def test_systematic_overconfidence_is_nonzero(self):
        pred = np.array([0.9, 0.9, 0.9, 0.9])
        actual = np.array([1.0, 0.0, 0.0, 0.0])
        assert _ece(pred, actual) == pytest.approx(0.65, abs=1e-9)


class TestReliabilityBins:
    def test_empty_input(self):
        assert _reliability_bins(np.array([]), np.array([])) == []

    def test_basic_binning(self):
        pred = np.array([0.1, 0.1, 0.9, 0.9])
        actual = np.array([0.0, 0.0, 1.0, 1.0])
        bins = _reliability_bins(pred, actual, n_bins=2)
        assert len(bins) == 2
        lo0, hi0, n0, mp0, ma0 = bins[0]
        assert n0 == 2
        assert mp0 == pytest.approx(0.1)
        assert ma0 == pytest.approx(0.0)
        lo1, hi1, n1, mp1, ma1 = bins[1]
        assert n1 == 2
        assert mp1 == pytest.approx(0.9)
        assert ma1 == pytest.approx(1.0)


# ── identity / inactive behaviour ───────────────────────────────────────────


class TestIdentityPassthrough:
    def test_fresh_calibrator_is_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        assert not cal.is_active

    def test_calibrate_is_identity_when_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        for p in (0.0, 0.5, 0.62, 1.0):
            assert cal.calibrate(p) == p


# ── fit() ────────────────────────────────────────────────────────────────────


class TestFit:
    def test_below_min_samples_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"), min_samples=40)
        report = cal.fit(_synthetic_samples(n=10))
        assert report.active is False
        assert report.n == 10
        assert not cal.is_active
        assert cal.calibrate(0.7) == 0.7

    def test_no_variation_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"), min_samples=5)
        # All raw_p identical → len(unique) < 2 → inactive regardless of n.
        samples = [(0.6, True), (0.6, False), (0.6, True), (0.6, False), (0.6, True)]
        report = cal.fit(samples)
        assert report.active is False
        assert not cal.is_active

    def test_zero_probabilities_excluded(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"), min_samples=2)
        # raw_p == 0.0 means "no probability recorded" and must be dropped.
        samples = [(0.0, True), (0.0, False), (0.6, True), (0.7, False)]
        report = cal.fit(samples)
        assert report.n == 2

    def test_enough_varied_data_becomes_active(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        report = cal.fit(_synthetic_samples(n=400))
        assert report.active is True
        assert report.n == 400
        assert cal.is_active
        assert report.shrink == pytest.approx(1.0)  # n >= full_trust_n
        # Reduces Brier/ECE vs. the raw, over-confident source.
        assert report.brier_cal <= report.brier_raw
        assert report.ece_cal <= report.ece_raw

    def test_shrink_scales_with_sample_size(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        report = cal.fit(_synthetic_samples(n=100))
        assert report.active is True
        assert report.shrink == pytest.approx(0.5)

    def test_overconfident_source_pulled_down(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        cal.fit(_synthetic_samples(n=400, lo=0.50, hi=0.80, bias=0.10))
        assert cal.calibrate(0.75) < 0.75

    def test_calibrate_is_monotonic(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        cal.fit(_synthetic_samples(n=400))
        grid = np.linspace(0.5, 0.8, 25)
        out = [cal.calibrate(g) for g in grid]
        assert all(b >= a - 1e-9 for a, b in zip(out, out[1:]))

    def test_calibrate_clamped_to_bounds(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        # Hand-craft an active state whose knots would extrapolate outside
        # [_P_FLOOR, _P_CEIL] without the clamp.
        cal._x = np.array([0.0, 1.0])
        cal._y = np.array([0.0, 1.0])
        cal._shrink = 1.0
        assert cal.is_active
        assert cal.calibrate(0.0) == pytest.approx(_P_FLOOR)
        assert cal.calibrate(1.0) == pytest.approx(_P_CEIL)


# ── fit_from_journal() ───────────────────────────────────────────────────────


class TestFitFromJournal:
    def test_filters_by_model_version(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"), min_samples=5)
        old = [_rec(p, w, version=MIN_MODEL_VERSION - 1)
               for p, w in _synthetic_samples(n=50)]
        new = [_rec(p, w, version=MIN_MODEL_VERSION)
               for p, w in _synthetic_samples(n=3, seed=1)]
        report = cal.fit_from_journal(_journal(old + new))
        # Only the 3 current-version records are usable -> below min_samples.
        assert report.n == 3
        assert report.active is False

    def test_n_seen_tracks_total_records_not_filtered(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"), min_samples=5)
        old = [_rec(p, w, version=0) for p, w in _synthetic_samples(n=20)]
        new = [_rec(p, w, version=MIN_MODEL_VERSION)
               for p, w in _synthetic_samples(n=2, seed=1)]
        cal.fit_from_journal(_journal(old + new))
        assert cal._n_seen == len(old) + len(new)

    def test_active_with_enough_current_version_records(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        records = [_rec(p, w, version=MIN_MODEL_VERSION)
                   for p, w in _synthetic_samples(n=200)]
        report = cal.fit_from_journal(_journal(records))
        assert report.active is True
        assert cal.is_active

    def test_empty_journal(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        report = cal.fit_from_journal(_journal([]))
        assert report.active is False
        assert report.n == 0
        assert cal._n_seen == 0


# ── maybe_refit() ────────────────────────────────────────────────────────────


class TestMaybeRefit:
    def test_no_refit_below_growth_threshold(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=5, refit_every=10)
        records = [_rec(p, w) for p, w in _synthetic_samples(n=9)]
        assert cal.maybe_refit(_journal(records)) is False
        assert cal._n_seen == 0  # unchanged — no fit happened

    def test_refit_once_growth_threshold_met(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=5, refit_every=10)
        records = [_rec(p, w) for p, w in _synthetic_samples(n=10)]
        assert cal.maybe_refit(_journal(records)) is True
        assert cal._n_seen == 10

    def test_no_double_refit_until_grown_again(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=5, refit_every=10)
        records = [_rec(p, w) for p, w in _synthetic_samples(n=10)]
        assert cal.maybe_refit(_journal(records)) is True
        # Same journal, no growth -> no refit.
        assert cal.maybe_refit(_journal(records)) is False
        # Grown by less than refit_every -> still no refit.
        more = records + [_rec(p, w) for p, w in _synthetic_samples(n=5, seed=2)]
        assert cal.maybe_refit(_journal(more)) is False
        # Grown by >= refit_every since last fit -> refits.
        lots = more + [_rec(p, w) for p, w in _synthetic_samples(n=5, seed=3)]
        assert cal.maybe_refit(_journal(lots)) is True
        assert cal._n_seen == len(lots)


# ── persistence ──────────────────────────────────────────────────────────────


class TestPersistence:
    def test_save_then_load_round_trip(self, tmp_path):
        path = str(tmp_path / "calib.json")
        cal = ProbabilityCalibrator(path=path, min_samples=40, full_trust_n=200)
        cal.fit(_synthetic_samples(n=400))
        assert cal.is_active

        reloaded = ProbabilityCalibrator(path=path)
        assert reloaded.is_active
        assert reloaded._n_fit == cal._n_fit
        assert reloaded._shrink == pytest.approx(cal._shrink)
        np.testing.assert_allclose(reloaded._x, cal._x)
        np.testing.assert_allclose(reloaded._y, cal._y)
        assert reloaded.calibrate(0.65) == pytest.approx(cal.calibrate(0.65))

    def test_load_missing_file_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "does_not_exist.json"))
        assert not cal.is_active

    def test_load_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "calib.json"
        path.write_text("not valid json {{{")
        cal = ProbabilityCalibrator(path=str(path))
        assert not cal.is_active

    def test_save_writes_expected_shape(self, tmp_path):
        path = tmp_path / "calib.json"
        cal = ProbabilityCalibrator(path=str(path), min_samples=40, full_trust_n=200)
        cal.fit(_synthetic_samples(n=400))
        on_disk = json.loads(path.read_text())
        assert on_disk["n_fit"] == 400
        assert on_disk["shrink"] == pytest.approx(1.0)
        assert isinstance(on_disk["x"], list) and isinstance(on_disk["y"], list)
        assert len(on_disk["x"]) == len(on_disk["y"]) >= 2


# ── CalibrationReport.render() ───────────────────────────────────────────────


class TestCalibrationReportRender:
    def test_inactive_render_mentions_sample_count(self):
        report = CalibrationReport(n=12, active=False)
        text = report.render()
        assert "INACTIVE" in text
        assert "12" in text

    def test_active_render_includes_diagnostics(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"),
                                     min_samples=40, full_trust_n=200)
        report = cal.fit(_synthetic_samples(n=400))
        text = report.render()
        assert "ACTIVE" in text
        assert "Brier" in text
        assert "ECE" in text
        assert "Reliability" in text
