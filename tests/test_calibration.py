"""Tests for the isotonic probability calibrator (src/calibration.py).

ProbabilityCalibrator sits on the hot path: probability_gate.py routes its
reject threshold and Kelly sizing through `calibrate(combined_p)` for every
candidate trade once attached (src/paper_trading.py constructs one from the
live journal). The module had zero direct test coverage before this file —
a regression here would silently mis-size or mis-gate every live trade
without any test failing.
"""

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from src.calibration import (
    ProbabilityCalibrator,
    CalibrationReport,
    MIN_MODEL_VERSION,
    _P_FLOOR,
    _P_CEIL,
    _brier,
    _ece,
    _reliability_bins,
)


def _record(prob_win, won, version=MIN_MODEL_VERSION):
    return SimpleNamespace(prob_win=prob_win, won=won, prob_model_version=version)


def _journal(records):
    return SimpleNamespace(records=records)


def _overconfident_samples(n=400, seed=1, bias=0.12):
    """raw_p ~ U(0.5, 0.85); true outcome rate = raw_p - bias (over-confident
    source). A correct isotonic fit should pull predictions down toward truth."""
    rng = np.random.default_rng(seed)
    raw = rng.uniform(0.50, 0.85, size=n)
    won = rng.uniform(size=n) < np.clip(raw - bias, 0.0, 1.0)
    return list(zip(raw.tolist(), won.tolist()))


# ── identity passthrough while inactive ─────────────────────────────────────

class TestInactiveIdentity:
    def test_fresh_instance_with_no_file_is_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        assert not cal.is_active

    def test_calibrate_returns_input_unchanged_when_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "calib.json"))
        for p in (0.0, 0.3, 0.55, 0.8, 0.97, 1.0):
            assert cal.calibrate(p) == p

    def test_missing_file_does_not_raise(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "nope.json"))
        assert not cal.is_active

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{this is not json")
        cal = ProbabilityCalibrator(path=str(path))
        assert not cal.is_active

    def test_file_with_x_but_no_y_stays_inactive(self, tmp_path):
        path = tmp_path / "half.json"
        path.write_text(json.dumps({"x": [0.5, 0.6], "y": None, "n_fit": 10}))
        cal = ProbabilityCalibrator(path=str(path))
        assert not cal.is_active

    def test_single_knot_is_not_active(self, tmp_path):
        # is_active requires >= 2 knots even if x/y are present
        path = tmp_path / "one_knot.json"
        path.write_text(json.dumps({"x": [0.6], "y": [0.55], "n_fit": 50, "shrink": 1.0}))
        cal = ProbabilityCalibrator(path=str(path))
        assert not cal.is_active
        assert cal.calibrate(0.6) == 0.6


# ── fit(): small-sample / degenerate guards ─────────────────────────────────

class TestFitGuards:
    def test_below_min_samples_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, path=str(tmp_path / "c.json"))
        samples = [(0.6, True)] * 10 + [(0.65, False)] * 9  # n=19 < 40
        report = cal.fit(samples)
        assert report.active is False
        assert report.n == 19
        assert not cal.is_active

    def test_degenerate_single_unique_x_stays_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=10, path=str(tmp_path / "c.json"))
        samples = [(0.6, True)] * 30 + [(0.6, False)] * 30  # all same raw_p
        report = cal.fit(samples)
        assert report.active is False
        assert not cal.is_active

    def test_zero_and_none_probs_excluded_from_sample_count(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        samples = [(0.0, True), (0.0, False), (None, True)] + [(0.6, True), (0.65, False)] * 3
        report = cal.fit(samples)
        assert report.n == 6  # only the six (0.6/0.65, *) pairs count

    def test_fit_persists_inactive_state_to_disk(self, tmp_path):
        path = tmp_path / "c.json"
        cal = ProbabilityCalibrator(min_samples=40, path=str(path))
        cal.fit([(0.6, True)] * 5)
        assert path.exists()
        on_disk = json.loads(path.read_text())
        assert on_disk["x"] is None and on_disk["y"] is None

    def test_sklearn_unavailable_falls_back_to_inactive(self, tmp_path, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "sklearn.isotonic" or name == "sklearn":
                raise ImportError("sklearn not installed (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        cal = ProbabilityCalibrator(min_samples=10, path=str(tmp_path / "c.json"))
        samples = _overconfident_samples(n=200)
        report = cal.fit(samples)
        assert report.active is False
        assert not cal.is_active


# ── fit(): the real (sklearn-backed) path ───────────────────────────────────

class TestFitWithIsotonicRegression:
    def test_overconfident_source_is_pulled_down(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=str(tmp_path / "c.json"))
        report = cal.fit(_overconfident_samples(n=400, bias=0.12))
        assert report.active is True
        assert cal.is_active
        assert cal.calibrate(0.80) < 0.80

    def test_calibration_curve_is_monotonic(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=str(tmp_path / "c.json"))
        cal.fit(_overconfident_samples(n=400, bias=0.12))
        grid = np.linspace(0.50, 0.85, 25)
        out = [cal.calibrate(float(g)) for g in grid]
        assert all(b >= a - 1e-9 for a, b in zip(out, out[1:]))

    def test_shrink_scales_with_sample_count(self, tmp_path):
        small = ProbabilityCalibrator(min_samples=40, full_trust_n=400, path=str(tmp_path / "small.json"))
        small.fit(_overconfident_samples(n=40, seed=2))
        big = ProbabilityCalibrator(min_samples=40, full_trust_n=400, path=str(tmp_path / "big.json"))
        big.fit(_overconfident_samples(n=400, seed=2))
        assert small._shrink < big._shrink
        assert big._shrink == pytest.approx(1.0)

    def test_full_trust_n_zero_means_full_shrink_immediately(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=0, path=str(tmp_path / "c.json"))
        cal.fit(_overconfident_samples(n=50, seed=3))
        assert cal._shrink == 1.0

    def test_report_brier_improves_on_overconfident_source(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=str(tmp_path / "c.json"))
        report = cal.fit(_overconfident_samples(n=600, bias=0.15, seed=4))
        assert report.brier_cal <= report.brier_raw

    def test_active_fit_persists_knots_to_disk(self, tmp_path):
        path = tmp_path / "c.json"
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=str(path))
        cal.fit(_overconfident_samples(n=300, seed=5))
        on_disk = json.loads(path.read_text())
        assert isinstance(on_disk["x"], list) and len(on_disk["x"]) >= 2
        assert on_disk["n_fit"] == 300


# ── calibrate(): blend formula + clipping ───────────────────────────────────

class TestCalibrateBlendAndClip:
    def _make_active(self, tmp_path, shrink):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        # Bypass fit(): inject a simple known curve directly.
        cal._x = np.array([0.5, 0.6, 0.7, 0.8, 0.9])
        cal._y = np.array([0.5, 0.5, 0.5, 0.5, 0.5])  # flat curve: everything maps to 0.5
        cal._shrink = shrink
        return cal

    def test_full_shrink_uses_curve_value_only(self, tmp_path):
        cal = self._make_active(tmp_path, shrink=1.0)
        assert cal.calibrate(0.7) == pytest.approx(0.5)

    def test_zero_shrink_is_pure_identity(self, tmp_path):
        cal = self._make_active(tmp_path, shrink=0.0)
        assert cal.calibrate(0.7) == pytest.approx(0.7)

    def test_half_shrink_averages_curve_and_raw(self, tmp_path):
        cal = self._make_active(tmp_path, shrink=0.5)
        # curve says 0.5, raw is 0.7 -> blended = 0.5*0.5 + 0.5*0.7 = 0.6
        assert cal.calibrate(0.7) == pytest.approx(0.6)

    def test_output_never_below_floor(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.5, 0.6, 0.7])
        cal._y = np.array([0.1, 0.1, 0.1])  # curve wants to go below _P_FLOOR
        cal._shrink = 1.0
        assert cal.calibrate(0.6) == _P_FLOOR

    def test_output_never_above_ceil(self, tmp_path):
        cal = ProbabilityCalibrator(path=str(tmp_path / "c.json"))
        cal._x = np.array([0.5, 0.6, 0.7])
        cal._y = np.array([0.999, 0.999, 0.999])  # curve wants to go above _P_CEIL
        cal._shrink = 1.0
        assert cal.calibrate(0.6) == _P_CEIL

    def test_extrapolation_clamps_at_curve_ends(self, tmp_path):
        cal = self._make_active(tmp_path, shrink=1.0)
        # np.interp clamps outside [x.min(), x.max()] to the boundary y value
        assert cal.calibrate(0.99) == pytest.approx(0.5)
        assert cal.calibrate(0.01) == pytest.approx(0.5)


# ── fit_from_journal(): model-version filtering + _n_seen bookkeeping ──────

class TestFitFromJournal:
    def test_excludes_records_below_min_model_version(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        records = (
            [_record(0.9, True, version=MIN_MODEL_VERSION - 1) for _ in range(20)]
            + [_record(0.6, True, version=MIN_MODEL_VERSION) for _ in range(3)]
            + [_record(0.65, False, version=MIN_MODEL_VERSION) for _ in range(3)]
        )
        report = cal.fit_from_journal(_journal(records))
        assert report.n == 6  # only the MIN_MODEL_VERSION+ records count

    def test_n_seen_tracks_total_records_not_usable_sample_count(self, tmp_path):
        # Documented invariant: maybe_refit() gates on TOTAL journal growth, so a
        # large pool of version-excluded legacy records must still count toward
        # _n_seen, or it would trigger a refit on every single call.
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        records = (
            [_record(0.9, True, version=MIN_MODEL_VERSION - 1) for _ in range(50)]
            + [_record(0.6, True, version=MIN_MODEL_VERSION) for _ in range(3)]
            + [_record(0.65, False, version=MIN_MODEL_VERSION) for _ in range(3)]
        )
        cal.fit_from_journal(_journal(records))
        assert cal._n_seen == len(records) == 56

    def test_zero_and_none_prob_win_treated_as_unusable(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=3, path=str(tmp_path / "c.json"))
        records = [
            _record(None, True), _record(0.0, False),
            _record(0.6, True), _record(0.65, False), _record(0.7, True),
        ]
        report = cal.fit_from_journal(_journal(records))
        assert report.n == 3

    def test_empty_journal_is_inactive(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, path=str(tmp_path / "c.json"))
        report = cal.fit_from_journal(_journal([]))
        assert report.active is False
        assert cal._n_seen == 0


# ── maybe_refit(): growth-gated refit ───────────────────────────────────────

class TestMaybeRefit:
    def test_no_refit_below_growth_threshold(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, refit_every=25, path=str(tmp_path / "c.json"))
        records = [_record(0.6, True) for _ in range(10)]
        cal.fit_from_journal(_journal(records))  # _n_seen = 10
        records.extend(_record(0.6, True) for _ in range(5))  # +5, < refit_every
        assert cal.maybe_refit(_journal(records)) is False

    def test_refit_triggers_once_growth_threshold_met(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, refit_every=25, path=str(tmp_path / "c.json"))
        records = [_record(0.6, True) for _ in range(10)]
        cal.fit_from_journal(_journal(records))  # _n_seen = 10
        records.extend(_record(0.65, False) for _ in range(25))  # +25 == refit_every
        assert cal.maybe_refit(_journal(records)) is True
        assert cal._n_seen == 35

    def test_maybe_refit_is_a_noop_call_on_static_journal(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=5, refit_every=25, path=str(tmp_path / "c.json"))
        records = [_record(0.6, True) for _ in range(10)]
        cal.fit_from_journal(_journal(records))
        assert cal.maybe_refit(_journal(records)) is False  # no growth at all


# ── save()/load(): persistence round trip ───────────────────────────────────

class TestPersistence:
    def test_round_trip_preserves_active_state(self, tmp_path):
        path = str(tmp_path / "c.json")
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=path)
        cal.fit(_overconfident_samples(n=300, seed=6))
        assert cal.is_active

        reloaded = ProbabilityCalibrator(path=path)
        assert reloaded.is_active
        assert reloaded._n_fit == cal._n_fit
        np.testing.assert_array_equal(reloaded._x, cal._x)
        np.testing.assert_array_equal(reloaded._y, cal._y)
        assert reloaded.calibrate(0.75) == pytest.approx(cal.calibrate(0.75))

    def test_save_failure_does_not_raise(self, tmp_path):
        # Directory as the "file" path -> open() for writing fails; save() must
        # swallow it (calibration must never be able to crash the trading loop).
        bad_path = tmp_path / "is_a_dir"
        bad_path.mkdir()
        cal = ProbabilityCalibrator(path=str(bad_path))
        cal.fit([(0.6, True)] * 5)  # below min_samples -> hits save() via _deactivate path
        # no exception means the test passed


# ── helper functions: _brier / _ece / _reliability_bins ─────────────────────

class TestMetricHelpers:
    def test_brier_perfect_predictions_is_zero(self):
        pred = np.array([1.0, 0.0, 1.0])
        actual = np.array([1.0, 0.0, 1.0])
        assert _brier(pred, actual) == 0.0

    def test_brier_matches_hand_computation(self):
        pred = np.array([0.8, 0.2])
        actual = np.array([1.0, 0.0])
        # mean((0.8-1)^2, (0.2-0)^2) = mean(0.04, 0.04) = 0.04
        assert _brier(pred, actual) == pytest.approx(0.04)

    def test_brier_empty_is_zero(self):
        assert _brier(np.array([]), np.array([])) == 0.0

    def test_ece_perfect_calibration_is_zero(self):
        # Each bin's mean predicted probability matches its actual win rate
        # exactly (1/10 wins at pred=0.1, 9/10 wins at pred=0.9) -> ECE == 0.
        pred = np.array([0.1] * 10 + [0.9] * 10)
        actual = np.array([1.0] + [0.0] * 9 + [1.0] * 9 + [0.0])
        assert _ece(pred, actual) == pytest.approx(0.0)

    def test_ece_empty_is_zero(self):
        assert _ece(np.array([]), np.array([])) == 0.0

    def test_reliability_bins_cover_all_samples(self):
        rng = np.random.default_rng(7)
        pred = rng.uniform(0.5, 0.9, size=100)
        actual = (rng.uniform(size=100) < pred).astype(float)
        bins = _reliability_bins(pred, actual, n_bins=10)
        assert sum(nb for _, _, nb, _, _ in bins) == 100

    def test_reliability_bins_empty_input(self):
        assert _reliability_bins(np.array([]), np.array([])) == []


# ── CalibrationReport.render(): smoke tests ─────────────────────────────────

class TestReportRender:
    def test_inactive_report_mentions_need_more_samples(self):
        report = CalibrationReport(n=12, active=False)
        text = report.render()
        assert "INACTIVE" in text
        assert "12" in text

    def test_active_report_includes_brier_and_bins(self, tmp_path):
        cal = ProbabilityCalibrator(min_samples=40, full_trust_n=200, path=str(tmp_path / "c.json"))
        report = cal.fit(_overconfident_samples(n=300, seed=8))
        text = report.render()
        assert "ACTIVE" in text
        assert "Brier" in text
        assert "Reliability" in text
