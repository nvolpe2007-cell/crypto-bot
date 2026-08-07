"""Every standalone paper-arm script persists its own data/<arm>_state.json via a
bare `_save_state(state)` that used to do `json.dumps(state, indent=2)` with no
NaN/Inf sanitization. json.dumps's default allow_nan=True does NOT raise on a
NaN/Inf float — it silently writes the non-standard `NaN`/`Infinity` literal,
which json.load parses straight back into float('nan')/float('inf') on the next
run with no error. Since NaN comparisons are always False in Python, a NaN that
reaches a persisted position/price field can silently disable stop-loss/take-
profit checks on it forever, surviving every subsequent restart (the same bug
class fixed for src/paper_trading.py's _save_open_positions in PR #62).

These tests prove every arm's _save_state now routes through the shared
src.state.sanitize_for_json helper before writing, for every script that owns
its own state file.
"""
import json
import math

import pytest

from src.state import sanitize_for_json

import brain_overseer
import brain_paper
import btc_trend_paper
import conf_paper
import kelly_trend_paper
import lev_perp_paper
import pairs_paper
import regime_arm
import swing_paper
import tsmom_ls_paper
import tsmom_paper

ARM_MODULES = [
    brain_overseer, brain_paper, btc_trend_paper, conf_paper, kelly_trend_paper,
    lev_perp_paper, pairs_paper, regime_arm, swing_paper, tsmom_ls_paper, tsmom_paper,
]


class TestSanitizeForJson:
    def test_nan_float_becomes_none(self):
        assert sanitize_for_json(float("nan")) is None

    def test_inf_and_neg_inf_become_none(self):
        assert sanitize_for_json(float("inf")) is None
        assert sanitize_for_json(float("-inf")) is None

    def test_finite_float_passes_through_unchanged(self):
        assert sanitize_for_json(3.14) == 3.14

    def test_recurses_into_nested_dicts_and_lists(self):
        out = sanitize_for_json({"a": [1.0, float("nan"), {"b": float("inf")}]})
        assert out == {"a": [1.0, None, {"b": None}]}

    def test_non_float_values_untouched(self):
        out = sanitize_for_json({"s": "x", "i": 1, "n": None, "b": True})
        assert out == {"s": "x", "i": 1, "n": None, "b": True}


@pytest.mark.parametrize("mod", ARM_MODULES, ids=lambda m: m.__name__)
class TestArmSaveStateSanitization:
    def test_nan_written_as_null_not_bare_nan_literal(self, mod, tmp_path, monkeypatch):
        target = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_FILE", target)

        mod._save_state({"equity": float("nan"), "positions": {}})

        raw = target.read_text()
        assert "NaN" not in raw
        assert json.loads(raw)["equity"] is None

    def test_inf_written_as_null_not_bare_infinity_literal(self, mod, tmp_path, monkeypatch):
        target = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_FILE", target)

        mod._save_state({"equity": float("inf"), "drawdown": float("-inf")})

        raw = target.read_text()
        assert "Infinity" not in raw
        loaded = json.loads(raw)
        assert loaded["equity"] is None
        assert loaded["drawdown"] is None

    def test_round_trip_does_not_resurrect_nan(self, mod, tmp_path, monkeypatch):
        """The whole point: a value that round-trips as NaN compares False
        forever (nan < x and nan >= x are both False), silently disabling any
        price/stop check gated on it. Confirm the reloaded value is a clean
        None, never a float that fails math.isnan()-style detection silently."""
        target = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_FILE", target)

        mod._save_state({"entry_price": float("nan")})

        reloaded = json.loads(target.read_text())["entry_price"]
        assert reloaded is None
        assert not isinstance(reloaded, float)

    def test_finite_values_unaffected(self, mod, tmp_path, monkeypatch):
        target = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_FILE", target)

        mod._save_state({"equity": 1234.56, "closed": [{"pnl": -12.5}]})

        loaded = json.loads(target.read_text())
        assert loaded["equity"] == 1234.56
        assert loaded["closed"][0]["pnl"] == -12.5
