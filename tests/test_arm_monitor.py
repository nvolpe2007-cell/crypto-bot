"""Tests for scripts/arm_monitor.py -- the event-driven "did anything cross
a meaningful threshold" alerter that complements weekly_report.py's periodic
digest. Pure-logic tests only (verdict_category, diff) plus state-file I/O
via tmp_path; never calls the real proof_scorecard/network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import arm_monitor as am


class TestVerdictCategory:
    def test_fantasy(self):
        assert am.verdict_category("FANTASY (not executable on a US Kraken-spot account)") == "FANTASY"

    def test_not_proven_sample(self):
        assert am.verdict_category("NOT PROVEN — only 11 trades (need 30+)") == "NOT_PROVEN"

    def test_not_proven_tstat(self):
        assert am.verdict_category("NOT PROVEN — clustered t=1.20 (need >2.0; ...)") == "NOT_PROVEN"

    def test_failed(self):
        assert am.verdict_category("FAILED — negative expectancy (-0.088/trade)") == "FAILED"

    def test_proven_single(self):
        assert am.verdict_category("PROVEN (single) — NOT family-wise robust: ...") == "PROVEN_SINGLE"

    def test_proven_full(self):
        assert am.verdict_category("PROVEN ✓ exp=+0.05/trade ...") == "PROVEN"


class TestDiffFirstRun:
    def test_new_arm_with_trades_alerts(self):
        prev = {}
        curr = {"lev_perp": {"n": 11, "category": "NOT_PROVEN", "verdict": "NOT PROVEN — only 11 trades (need 30+)"}}
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "lev_perp" in lines[0] and "new arm" in lines[0]

    def test_new_arm_zero_trades_silent(self):
        prev = {}
        curr = {"fresh_arm": {"n": 0, "category": "NOT_PROVEN", "verdict": "NOT PROVEN — only 0 trades (need 30+)"}}
        assert am.diff(prev, curr) == []


class TestDiffSampleFloor:
    def test_crossing_n30_alerts(self):
        prev = {"arm": {"n": 27, "category": "NOT_PROVEN", "verdict": "NOT PROVEN — only 27 trades (need 30+)"}}
        curr = {"arm": {"n": 31, "category": "NOT_PROVEN", "verdict": "NOT PROVEN — clustered t=1.10 (need >2.0)"}}
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "crossed n=30" in lines[0]

    def test_staying_below_floor_silent(self):
        prev = {"arm": {"n": 5, "category": "NOT_PROVEN", "verdict": "x"}}
        curr = {"arm": {"n": 12, "category": "NOT_PROVEN", "verdict": "y"}}
        assert am.diff(prev, curr) == []

    def test_staying_above_floor_no_duplicate_alert(self):
        # already crossed on a prior run -- must not re-alert every run after
        prev = {"arm": {"n": 35, "category": "NOT_PROVEN", "verdict": "x"}}
        curr = {"arm": {"n": 40, "category": "NOT_PROVEN", "verdict": "y"}}
        assert am.diff(prev, curr) == []


class TestDiffVerdictCategory:
    def test_not_proven_to_proven_alerts(self):
        prev = {"arm": {"n": 35, "category": "NOT_PROVEN", "verdict": "NOT PROVEN — clustered t=1.5"}}
        curr = {"arm": {"n": 40, "category": "PROVEN", "verdict": "PROVEN ✓ exp=+0.05/trade"}}
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "📈" in lines[0] and "NOT_PROVEN → PROVEN" in lines[0]

    def test_regression_proven_single_to_failed_alerts(self):
        prev = {"arm": {"n": 40, "category": "PROVEN_SINGLE", "verdict": "PROVEN (single) ..."}}
        curr = {"arm": {"n": 45, "category": "FAILED", "verdict": "FAILED — negative expectancy"}}
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "📉" in lines[0]

    def test_no_category_change_silent(self):
        prev = {"arm": {"n": 40, "category": "NOT_PROVEN", "verdict": "a"}}
        curr = {"arm": {"n": 45, "category": "NOT_PROVEN", "verdict": "b"}}
        assert am.diff(prev, curr) == []

    def test_floor_cross_and_category_change_dont_double_fire(self):
        # crossing n=30 already produces one alert -- a simultaneous category
        # change on the SAME event should not ALSO fire the generic transition
        # line (that would be two alerts for one underlying event)
        prev = {"arm": {"n": 25, "category": "NOT_PROVEN", "verdict": "x"}}
        curr = {"arm": {"n": 31, "category": "FAILED", "verdict": "FAILED — negative expectancy"}}
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "crossed n=30" in lines[0]


class TestMultipleArms:
    def test_only_changed_arms_produce_lines(self):
        prev = {
            "arm_a": {"n": 27, "category": "NOT_PROVEN", "verdict": "x"},
            "arm_b": {"n": 50, "category": "NOT_PROVEN", "verdict": "y"},
        }
        curr = {
            "arm_a": {"n": 31, "category": "NOT_PROVEN", "verdict": "z"},
            "arm_b": {"n": 55, "category": "NOT_PROVEN", "verdict": "y2"},
        }
        lines = am.diff(prev, curr)
        assert len(lines) == 1
        assert "arm_a" in lines[0]


class TestStateFileIO:
    def test_load_state_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(am, "STATE_FILE", tmp_path / "nonexistent.json")
        assert am.load_state() == {}

    def test_save_then_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(am, "STATE_FILE", tmp_path / "state.json")
        data = {"arm": {"n": 10, "category": "NOT_PROVEN", "verdict": "x"}}
        am.save_state(data)
        assert am.load_state() == data

    def test_save_is_atomic_tmp_file_cleaned_up(self, monkeypatch, tmp_path):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(am, "STATE_FILE", state_file)
        am.save_state({"a": 1})
        assert state_file.exists()
        assert not state_file.with_suffix(".json.tmp").exists()


class TestSnapshotArms:
    def test_uses_proof_scorecard_build_arms_and_verdict(self, monkeypatch):
        fake_arm = {"label": "test_arm", "n": 42, "executable": True,
                    "expectancy": 0.5, "t_stat": 3.0, "t_clustered": 3.0, "eff_n": 42}
        monkeypatch.setattr(am.ps, "build_arms", lambda: ([fake_arm], 1, am.ps.T_MIN))
        snap = am.snapshot_arms()
        assert "test_arm" in snap
        assert snap["test_arm"]["n"] == 42
        assert snap["test_arm"]["category"] in ("PROVEN", "PROVEN_SINGLE")
