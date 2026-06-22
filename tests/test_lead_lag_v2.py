"""
Unit tests for src/lead_lag_v2.py

LeadLagV2 fires when BTC OFI reaches threshold AND price confirms direction,
then lets lag-instrument callers check entry eligibility for `window_seconds`
via two methods:
  - check_lag_entry(lag_price): convenience method that captures the lag's
    reference price lazily on the first call after a fire, then enforces the
    repricing guard against that captured baseline on every later call.
  - check_lag_entry_with_fire_price(current, at_fire): the same guard when the
    caller already tracks the lag price at fire time itself.

Covers:
- update_lead: no fire below threshold, no fire without price confirmation,
  fires on threshold + confirmation, refires/extends window same direction
- check_lag_entry: False with no fire / after expiry; first call captures
  baseline and allows; later calls enforce the reprice_bps guard against that
  captured baseline (this is the bug fix — it used to always return True)
- check_lag_entry_with_fire_price: explicit baseline guard, zero-price escape
  hatch, expiry clears state
- get_direction / is_expired / get_fire_event / time_remaining_ms / reset /
  summary: state plumbing
"""

import pytest

from src.lead_lag_v2 import LeadLagV2, LeadLagFireEvent


class _Clock:
    """Callable that returns a controllable fake timestamp."""
    def __init__(self, start: float = 1_000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture()
def clock():
    return _Clock(start=1_000.0)


@pytest.fixture()
def lead(clock, monkeypatch):
    monkeypatch.setattr('src.lead_lag_v2.time.time', clock)
    return LeadLagV2(ofi_threshold=0.35, window_seconds=30.0,
                      reprice_bps=15.0, confirm_bps=0.5)


BTC_BASE = 50_000.0
ETH_BASE = 3_000.0


def _fire(lead: LeadLagV2, clock: _Clock, direction: int = 1,
          ofi: float = 0.4) -> None:
    """Prime the baseline, then fire a signal in the given direction."""
    lead.update_lead(0.0, BTC_BASE)
    clock.advance(1.0)
    move = BTC_BASE * 0.0010 * direction  # 10 bps move, well past confirm_bps
    lead.update_lead(ofi * direction, BTC_BASE + move)


# ── update_lead ────────────────────────────────────────────────────────────

class TestUpdateLead:
    def test_no_fire_below_threshold(self, lead, clock):
        lead.update_lead(0.0, BTC_BASE)
        clock.advance(1.0)
        event = lead.update_lead(0.10, BTC_BASE + 50)
        assert event is None
        assert lead.get_direction() == 0

    def test_no_fire_without_price_confirmation(self, lead, clock):
        lead.update_lead(0.0, BTC_BASE)
        clock.advance(1.0)
        # Strong OFI says "up" but price actually moved down — no confirmation.
        event = lead.update_lead(0.5, BTC_BASE - 50)
        assert event is None

    def test_fires_on_threshold_and_confirmation_buy(self, lead, clock):
        _fire(lead, clock, direction=1)
        assert lead.get_direction() == 1

    def test_fires_on_threshold_and_confirmation_sell(self, lead, clock):
        _fire(lead, clock, direction=-1)
        assert lead.get_direction() == -1

    def test_refire_same_direction_extends_window(self, lead, clock):
        _fire(lead, clock, direction=1)
        clock.advance(20.0)
        assert not lead.is_expired()
        _fire(lead, clock, direction=1)
        # Window restarted on refire — still alive after another 20s (>30s total).
        clock.advance(20.0)
        assert not lead.is_expired()


# ── check_lag_entry (the bug fix) ───────────────────────────────────────────

class TestCheckLagEntry:
    def test_false_with_no_active_fire(self, lead):
        assert lead.check_lag_entry(ETH_BASE) is False
        assert lead.lag_move_bps == 0.0

    def test_false_after_expiry(self, lead, clock):
        _fire(lead, clock, direction=1)
        clock.advance(31.0)
        assert lead.check_lag_entry(ETH_BASE) is False

    def test_first_call_captures_baseline_and_allows(self, lead, clock):
        _fire(lead, clock, direction=1)
        assert lead.check_lag_entry(ETH_BASE) is True
        assert lead._fire.lag_price_at_fire == ETH_BASE

    def test_second_call_within_threshold_allows(self, lead, clock):
        _fire(lead, clock, direction=1)
        lead.check_lag_entry(ETH_BASE)            # captures baseline
        # Lag moved only 5 bps from the captured baseline — within 15 bps guard.
        moved = ETH_BASE * (1 + 0.0005)
        assert lead.check_lag_entry(moved) is True

    def test_second_call_beyond_threshold_rejects(self, lead, clock):
        _fire(lead, clock, direction=1)
        lead.check_lag_entry(ETH_BASE)             # captures baseline
        # Lag already repriced 20 bps from the captured baseline — past the 15bps guard.
        moved = ETH_BASE * (1 + 0.0020)
        assert lead.check_lag_entry(moved) is False
        assert lead.lag_move_bps == pytest.approx(20.0, abs=0.1)

    def test_baseline_resets_on_refire(self, lead, clock):
        _fire(lead, clock, direction=1)
        lead.check_lag_entry(ETH_BASE)
        clock.advance(5.0)
        _fire(lead, clock, direction=1)             # refire — new fire event
        # New baseline captured fresh from this call, even though price differs
        # from the original ETH_BASE used before the refire.
        new_baseline = ETH_BASE * 1.01
        assert lead.check_lag_entry(new_baseline) is True
        assert lead._fire.lag_price_at_fire == new_baseline


# ── check_lag_entry_with_fire_price ─────────────────────────────────────────

class TestCheckLagEntryWithFirePrice:
    def test_false_with_no_active_fire(self, lead):
        assert lead.check_lag_entry_with_fire_price(ETH_BASE, ETH_BASE) is False

    def test_within_threshold_allows(self, lead, clock):
        _fire(lead, clock, direction=1)
        moved = ETH_BASE * (1 + 0.0005)   # 5 bps
        assert lead.check_lag_entry_with_fire_price(moved, ETH_BASE) is True

    def test_beyond_threshold_rejects(self, lead, clock):
        _fire(lead, clock, direction=1)
        moved = ETH_BASE * (1 + 0.0020)   # 20 bps
        assert lead.check_lag_entry_with_fire_price(moved, ETH_BASE) is False

    def test_zero_price_is_an_escape_hatch(self, lead, clock):
        _fire(lead, clock, direction=1)
        assert lead.check_lag_entry_with_fire_price(ETH_BASE, 0.0) is True
        assert lead.check_lag_entry_with_fire_price(0.0, ETH_BASE) is True

    def test_expiry_clears_fire_and_rejects(self, lead, clock):
        _fire(lead, clock, direction=1)
        clock.advance(31.0)
        assert lead.check_lag_entry_with_fire_price(ETH_BASE, ETH_BASE) is False
        assert lead.get_fire_event() is None


# ── state plumbing ───────────────────────────────────────────────────────────

class TestStatePlumbing:
    def test_get_direction_zero_before_fire(self, lead):
        assert lead.get_direction() == 0

    def test_get_direction_clears_on_expiry(self, lead, clock):
        _fire(lead, clock, direction=-1)
        clock.advance(31.0)
        assert lead.get_direction() == 0

    def test_is_expired_true_with_no_fire(self, lead):
        assert lead.is_expired() is True

    def test_get_fire_event_returns_event_while_active(self, lead, clock):
        _fire(lead, clock, direction=1)
        event = lead.get_fire_event()
        assert isinstance(event, LeadLagFireEvent)
        assert event.fire_direction == 1

    def test_time_remaining_ms_counts_down(self, lead, clock):
        _fire(lead, clock, direction=1)
        remaining_before = lead.time_remaining_ms()
        clock.advance(10.0)
        remaining_after = lead.time_remaining_ms()
        assert remaining_after < remaining_before
        assert remaining_after == pytest.approx(20_000, abs=10)

    def test_time_remaining_ms_zero_when_expired(self, lead, clock):
        _fire(lead, clock, direction=1)
        clock.advance(31.0)
        assert lead.time_remaining_ms() == 0.0

    def test_reset_clears_fire(self, lead, clock):
        _fire(lead, clock, direction=1)
        lead.reset()
        assert lead.get_fire_event() is None
        assert lead.lag_move_bps == 0.0

    def test_summary_no_signal(self, lead):
        assert lead.summary() == "no active lead-lag signal"

    def test_summary_active_signal(self, lead, clock):
        _fire(lead, clock, direction=1)
        s = lead.summary()
        assert "BUY" in s
        assert "LEAD-LAG" in s
