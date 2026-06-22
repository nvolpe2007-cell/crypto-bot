"""Unit tests for src/daily_circuit.py

Covers:
- can_enter: (True, '') when under limit; (False, reason) when halted
- record_outcome: wins increment wins; losses increment losses
- Just-halted flag: fires exactly once when losses first hits max_losses
- Already-halted: subsequent losses do NOT re-fire just_halted
- Day rollover: state resets to zero on a new UTC date
- State persistence: save/load JSON round-trip
- Missing state file: treated as fresh day
- status(): dict contains correct halted, losses, wins, max_losses, date_utc
"""

import json
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

from src.daily_circuit import DailyCircuitBreaker, CircuitState


# ── helpers ───────────────────────────────────────────────────────────────────

_DATE_A = "2024-06-01"
_DATE_B = "2024-06-02"


def _real_dt(date_str: str) -> datetime:
    """Return noon UTC on the given YYYY-MM-DD date."""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)


def _patch_now(date_str: str):
    """Patch src.daily_circuit.datetime so .now() returns a real datetime."""
    real_dt = _real_dt(date_str)
    mock_cls = MagicMock()
    mock_cls.now.return_value = real_dt
    return patch("src.daily_circuit.datetime", mock_cls)


@pytest.fixture
def state_file(tmp_path):
    """Redirect STATE_FILE to a temp path; yield the path string."""
    f = str(tmp_path / "daily_circuit.json")
    with patch("src.daily_circuit.STATE_FILE", f):
        yield f


def _make_breaker(date_str: str, max_losses: int = 2,
                  state_file_path: str = None) -> DailyCircuitBreaker:
    """
    Construct a DailyCircuitBreaker anchored to *date_str*.

    If *state_file_path* is given the file I/O runs for real (use with tmp_path).
    Otherwise both _load and _save are patched to no-ops so the test is fully
    in-memory.
    """
    if state_file_path:
        with _patch_now(date_str), \
             patch("src.daily_circuit.STATE_FILE", state_file_path):
            return DailyCircuitBreaker(max_losses=max_losses)
    else:
        with _patch_now(date_str), \
             patch.object(DailyCircuitBreaker, "_load", return_value=None), \
             patch.object(DailyCircuitBreaker, "_save"):
            return DailyCircuitBreaker(max_losses=max_losses)


# ── can_enter ─────────────────────────────────────────────────────────────────

class TestCanEnter:
    def test_allows_entry_with_no_losses(self):
        cb = _make_breaker(_DATE_A)
        with _patch_now(_DATE_A):
            allowed, reason = cb.can_enter()
        assert allowed is True
        assert reason == ""

    def test_allows_entry_below_limit(self):
        cb = _make_breaker(_DATE_A, max_losses=3)
        cb.state.losses = 2                          # one below limit
        with _patch_now(_DATE_A):
            allowed, _ = cb.can_enter()
        assert allowed is True

    def test_blocks_entry_at_limit(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2                          # at limit
        cb.state.halted_at = 0.0
        with _patch_now(_DATE_A), patch("src.daily_circuit.time") as mock_time:
            mock_time.time.return_value = 3600.0     # 60 min since halt
            allowed, reason = cb.can_enter()
        assert allowed is False
        assert "daily circuit" in reason.lower()

    def test_block_message_includes_loss_count(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2
        cb.state.halted_at = 0.0
        with _patch_now(_DATE_A), patch("src.daily_circuit.time") as mock_time:
            mock_time.time.return_value = 60.0
            _, reason = cb.can_enter()
        assert "2" in reason


# ── record_outcome ────────────────────────────────────────────────────────────

class TestRecordOutcome:
    def test_win_increments_wins(self):
        cb = _make_breaker(_DATE_A)
        with _patch_now(_DATE_A):
            cb.record_outcome(won=True, pnl=5.0, symbol="BTC/USD")
        assert cb.state.wins == 1
        assert cb.state.losses == 0

    def test_loss_increments_losses(self):
        cb = _make_breaker(_DATE_A)
        with _patch_now(_DATE_A):
            cb.record_outcome(won=False, pnl=-3.0, symbol="BTC/USD")
        assert cb.state.losses == 1
        assert cb.state.wins == 0

    def test_just_halted_fires_at_max_losses(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 1                          # one loss already recorded
        with _patch_now(_DATE_A), patch("src.daily_circuit.time") as mock_time:
            mock_time.time.return_value = 9999.0
            just_halted, msg = cb.record_outcome(won=False, pnl=-5.0, symbol="ETH/USD")
        assert just_halted is True
        assert "ETH/USD" in msg
        assert cb.state.halted_at == 9999.0

    def test_just_halted_does_not_fire_before_limit(self):
        cb = _make_breaker(_DATE_A, max_losses=3)
        cb.state.losses = 0
        with _patch_now(_DATE_A):
            just_halted, _ = cb.record_outcome(won=False, pnl=-2.0, symbol="BTC/USD")
        assert just_halted is False

    def test_just_halted_fires_only_once(self):
        """A second loss beyond max_losses must NOT re-set halted_at."""
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2
        cb.state.halted_at = 111.0                   # already halted
        with _patch_now(_DATE_A):
            just_halted, _ = cb.record_outcome(won=False, pnl=-1.0, symbol="SOL/USD")
        assert just_halted is False
        assert cb.state.halted_at == 111.0            # unchanged

    def test_win_after_halt_does_not_clear_halt(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2
        cb.state.halted_at = 5000.0
        with _patch_now(_DATE_A):
            just_halted, _ = cb.record_outcome(won=True, pnl=10.0, symbol="BTC/USD")
        assert just_halted is False
        assert cb.state.wins == 1
        with _patch_now(_DATE_A), patch("src.daily_circuit.time") as mock_time:
            mock_time.time.return_value = 6000.0
            allowed, _ = cb.can_enter()
        assert allowed is False                       # still halted


# ── day rollover ──────────────────────────────────────────────────────────────

class TestDayRollover:
    def test_no_reset_on_same_day(self):
        cb = _make_breaker(_DATE_A, max_losses=5)
        cb.state.losses = 3
        with _patch_now(_DATE_A):
            cb._roll_if_needed()
        assert cb.state.losses == 3

    def test_resets_losses_on_new_day(self):
        cb = _make_breaker(_DATE_A, max_losses=5)
        cb.state.losses = 3
        cb.state.wins = 1
        with _patch_now(_DATE_B), \
             patch.object(cb, "_save"):
            cb._roll_if_needed()
        assert cb.state.losses == 0
        assert cb.state.wins == 0

    def test_resets_halt_on_new_day(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2
        cb.state.halted_at = 1234.0
        with _patch_now(_DATE_B), \
             patch.object(cb, "_save"):
            cb._roll_if_needed()
        with _patch_now(_DATE_B):
            allowed, _ = cb.can_enter()
        assert allowed is True

    def test_new_day_date_updated(self):
        cb = _make_breaker(_DATE_A)
        with _patch_now(_DATE_B), \
             patch.object(cb, "_save"):
            cb._roll_if_needed()
        assert cb.state.date_utc == _DATE_B


# ── persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_load_round_trip(self, state_file):
        with _patch_now(_DATE_A):
            cb = DailyCircuitBreaker(max_losses=3)

        # Record a loss so state is non-trivial, then save
        with _patch_now(_DATE_A), \
             patch("src.daily_circuit.time") as mock_time:
            mock_time.time.return_value = 42.0
            cb.record_outcome(won=False, pnl=-2.0, symbol="BTC/USD")

        # Re-load from disk
        with _patch_now(_DATE_A):
            cb2 = DailyCircuitBreaker(max_losses=3)

        assert cb2.state.losses == 1
        assert cb2.state.date_utc == _DATE_A

    def test_missing_file_starts_fresh(self, state_file):
        assert not os.path.exists(state_file)
        with _patch_now(_DATE_A):
            cb = DailyCircuitBreaker(max_losses=2)
        assert cb.state.losses == 0
        assert cb.state.wins == 0

    def test_save_is_atomic_no_leftover_tmp_file(self, state_file):
        """save() must write via a .tmp file + os.replace, never leaving the
        .tmp behind and never leaving STATE_FILE partially written."""
        with _patch_now(_DATE_A):
            cb = DailyCircuitBreaker(max_losses=3)
            cb.record_outcome(won=False, pnl=-1.0, symbol="BTC/USD")
        assert os.path.exists(state_file)
        assert not os.path.exists(state_file + ".tmp")
        with open(state_file) as f:
            d = json.load(f)  # must be valid, complete JSON
        assert d["losses"] == 1

    def test_corrupted_file_logs_warning_and_starts_fresh(self, state_file, caplog):
        """A truncated/corrupt state file (e.g. from a crash mid-write before the
        atomic-save fix) must not silently look identical to a missing file —
        it should be logged loudly, even though the safe fallback is still to
        start the day fresh rather than wedge the bot halted forever."""
        with open(state_file, "w") as f:
            f.write('{"date_utc": "2024-06-01", "losses": 2,')  # truncated JSON

        with _patch_now(_DATE_A):
            cb = DailyCircuitBreaker(max_losses=2)

        assert cb.state.losses == 0
        assert cb.state.wins == 0
        assert any("unreadable" in r.message for r in caplog.records)

    def test_missing_file_does_not_log_warning(self, state_file, caplog):
        """Genuinely missing file (first-ever run) is the normal case and
        should NOT trigger the corruption warning."""
        assert not os.path.exists(state_file)
        with _patch_now(_DATE_A):
            DailyCircuitBreaker(max_losses=2)
        assert not any("unreadable" in r.message for r in caplog.records)


# ── status() ─────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_not_halted(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 1
        cb.state.wins = 3
        with _patch_now(_DATE_A):
            s = cb.status()
        assert s["halted"] is False
        assert s["losses"] == 1
        assert s["wins"] == 3
        assert s["max_losses"] == 2
        assert s["date_utc"] == _DATE_A

    def test_status_halted(self):
        cb = _make_breaker(_DATE_A, max_losses=2)
        cb.state.losses = 2
        with _patch_now(_DATE_A):
            s = cb.status()
        assert s["halted"] is True

    def test_status_has_all_expected_keys(self):
        cb = _make_breaker(_DATE_A)
        with _patch_now(_DATE_A):
            s = cb.status()
        assert set(s.keys()) >= {"halted", "losses", "wins", "max_losses", "date_utc"}
