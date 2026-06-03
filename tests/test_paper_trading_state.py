"""
Unit tests for _save_adaptations / _load_adaptations in src/paper_trading.py.

Covers:
- _save_adaptations: writes correct JSON, creates directory, leaves no .tmp file,
  does not raise on I/O errors, uses atomic rename (no partial-write corruption)
- _load_adaptations: loads values from an existing file, no-ops cleanly when file
  is missing, logs a warning and keeps defaults when JSON is corrupt,
  merges partial files over existing defaults
"""

import copy
import json
import os
import pytest
from unittest.mock import patch

from src.paper_trading import _adapt, _load_adaptations, _save_adaptations, _ADAPT_FILE


@pytest.fixture(autouse=True)
def reset_adapt():
    original = copy.deepcopy(_adapt)
    yield
    _adapt.clear()
    _adapt.update(original)
    for k in list(_adapt.keys()):
        if k not in original:
            del _adapt[k]


# ── _save_adaptations ─────────────────────────────────────────────────────────

class TestSaveAdaptations:
    def test_writes_correct_data(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        _adapt['min_confidence'] = 42.0
        _adapt['loss_streak'] = 2
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _save_adaptations()
        data = json.loads(adapt_path.read_text())
        assert data['min_confidence'] == pytest.approx(42.0)
        assert data['loss_streak'] == 2

    def test_creates_directory_if_missing(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        assert not adapt_path.parent.exists()
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _save_adaptations()
        assert adapt_path.exists()

    def test_no_tmp_file_left_after_success(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _save_adaptations()
        assert not (tmp_path / "logs" / "strategy_adaptations.json.tmp").exists()

    def test_updated_at_field_added(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _save_adaptations()
        data = json.loads(adapt_path.read_text())
        assert 'updated_at' in data

    def test_no_raise_on_write_error(self):
        with patch('builtins.open', side_effect=PermissionError("denied")):
            _save_adaptations()  # must not raise

    def test_atomic_replace_used(self, tmp_path):
        """Verify os.replace is called (atomic rename, not a direct overwrite)."""
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)), \
             patch('os.replace', wraps=os.replace) as mock_replace:
            _save_adaptations()
        mock_replace.assert_called_once()
        # Destination argument must be the real file path (not the .tmp)
        _, dest = mock_replace.call_args[0]
        assert dest == str(adapt_path)


# ── _load_adaptations ─────────────────────────────────────────────────────────

class TestLoadAdaptations:
    def test_loads_values_from_file(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        adapt_path.parent.mkdir(parents=True)
        adapt_path.write_text(json.dumps({'min_confidence': 55.0, 'loss_streak': 3}))
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _load_adaptations()
        assert _adapt['min_confidence'] == pytest.approx(55.0)
        assert _adapt['loss_streak'] == 3

    def test_no_error_when_file_missing(self, tmp_path):
        nonexistent = str(tmp_path / "logs" / "adapt.json")
        with patch('src.paper_trading._ADAPT_FILE', nonexistent):
            _load_adaptations()  # must not raise

    def test_defaults_preserved_when_file_missing(self, tmp_path):
        _adapt['min_confidence'] = 35.0
        nonexistent = str(tmp_path / "logs" / "adapt.json")
        with patch('src.paper_trading._ADAPT_FILE', nonexistent):
            _load_adaptations()
        assert _adapt['min_confidence'] == pytest.approx(35.0)

    def test_corrupt_json_logs_warning_and_preserves_defaults(self, tmp_path, caplog):
        import logging
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        adapt_path.parent.mkdir(parents=True)
        adapt_path.write_text("NOT VALID JSON {{{")
        _adapt['min_confidence'] = 35.0
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)), \
             caplog.at_level(logging.WARNING):
            _load_adaptations()
        assert _adapt['min_confidence'] == pytest.approx(35.0)
        assert any('ADAPT' in r.message or 'Failed' in r.message
                   for r in caplog.records)

    def test_partial_file_merges_over_defaults(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        adapt_path.parent.mkdir(parents=True)
        adapt_path.write_text(json.dumps({'min_confidence': 48.0}))
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _load_adaptations()
        assert _adapt['min_confidence'] == pytest.approx(48.0)
        assert 'loss_streak' in _adapt  # other default keys still present

    def test_load_after_save_roundtrip(self, tmp_path):
        adapt_path = tmp_path / "logs" / "strategy_adaptations.json"
        _adapt['min_confidence'] = 41.0
        _adapt['win_streak'] = 7
        with patch('src.paper_trading._ADAPT_FILE', str(adapt_path)):
            _save_adaptations()
            _adapt['min_confidence'] = 99.0  # corrupt in-memory state
            _adapt['win_streak'] = 0
            _load_adaptations()
        assert _adapt['min_confidence'] == pytest.approx(41.0)
        assert _adapt['win_streak'] == 7
