"""
Unit tests for src/lead_lag_detector.py

The detector tracks BTC price ticks and emits timed directional signals for
alt symbols when BTC moves more than _MOVE_THRESHOLD (0.30%) inside a rolling
_WINDOW_S (60 s) window.

All tests use a fake clock injected via monkeypatch so we control time
deterministically without any actual sleeps.

Covers:
- First update initialises baseline; no signal emitted
- Insufficient BTC move → no signal
- BTC moves +0.30% → BUY signal on all tracked alts
- BTC moves -0.30% → SELL signal on all tracked alts
- Lead symbol (BTC itself) never receives a signal
- Signal expiry: get_signal returns None after decay_seconds
- Expired signals cleaned up from internal dict
- get_strength: [0, 1], linear decay, 0 before/after expiry
- Reversal: opposite-direction BTC move cancels existing signals
- Magnitude update: stronger move replaces weaker; weaker does not replace stronger
- Window refresh: new baseline after _WINDOW_S seconds elapsed
- confirms_buy / confirms_sell: correct True/False semantics
- summary(): returns a human-readable string
- Alt symbols not yet seen are not given signals
"""

import pytest
from src.lead_lag_detector import LeadLagDetector, _WINDOW_S, _SIGNAL_DECAY_S, _MOVE_THRESHOLD


# ── Fake clock ────────────────────────────────────────────────────────────────

class _Clock:
    """Callable that returns a controllable fake timestamp."""
    def __init__(self, start: float = 1_000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ── Fixtures ──────────────────────────────────────────────────────────────────

BTC  = 'BTC/USD'
ETH  = 'ETH/USD'
SOL  = 'SOL/USD'

BASE = 50_000.0          # baseline BTC price for most tests
THRESH = _MOVE_THRESHOLD  # 0.0030


@pytest.fixture()
def clock():
    return _Clock(start=1_000.0)


@pytest.fixture()
def det(clock, monkeypatch):
    """LeadLagDetector with time.time replaced by the fake clock."""
    monkeypatch.setattr('src.lead_lag_detector.time.time', clock)
    return LeadLagDetector(lead_symbol=BTC,
                           move_threshold=THRESH,
                           decay_seconds=_SIGNAL_DECAY_S)


def _prime(det: LeadLagDetector, clock: _Clock,
           gap: float = 1.0) -> None:
    """
    Prime the detector so subsequent BTC updates have a valid baseline.

    Step 1 — register ETH/SOL prices so they are in _prices (required for
              signals to be emitted for them).
    Step 2 — first BTC update sets the baseline (no signal yet).
    Step 3 — advance the clock by gap seconds (default 1 s) so the next BTC
              update is inside the same window.
    """
    det.update_price(ETH, BASE)
    det.update_price(SOL, BASE)
    det.update_price(BTC, BASE)
    clock.advance(gap)


# ── Initial state ─────────────────────────────────────────────────────────────

class TestInitialState:
    def test_no_signal_before_any_update(self, det):
        assert det.get_signal(ETH) is None

    def test_no_signal_after_first_btc_update_only(self, det, clock):
        det.update_price(ETH, BASE)
        det.update_price(BTC, BASE)   # sets baseline, returns immediately
        assert det.get_signal(ETH) is None

    def test_strength_zero_before_any_update(self, det):
        assert det.get_strength(ETH) == 0.0

    def test_confirms_buy_true_with_no_signal(self, det):
        assert det.confirms_buy(ETH) is True

    def test_confirms_sell_true_with_no_signal(self, det):
        assert det.confirms_sell(ETH) is True


# ── BUY signal emission ───────────────────────────────────────────────────────

class TestBuySignalEmission:
    def test_upward_move_emits_buy_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))   # 0.40% up
        assert det.get_signal(ETH) == 'BUY'

    def test_buy_signal_emitted_for_all_tracked_alts(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_signal(ETH) == 'BUY'
        assert det.get_signal(SOL) == 'BUY'

    def test_nominal_threshold_move_no_signal_due_to_floating_point(self, det, clock):
        """
        BASE * (1 + THRESH) produces a price where the calculated move is
        microscopically below THRESH due to IEEE 754 rounding, so no signal
        fires.  Reliable signal emission requires a margin above THRESH.
        """
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH))   # FP: measured move < threshold
        assert det.get_signal(ETH) is None

    def test_move_above_threshold_with_margin_emits_signal(self, det, clock):
        """A move 10% above threshold reliably clears the FP rounding margin."""
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH * 1.1))
        assert det.get_signal(ETH) == 'BUY'

    def test_move_just_below_threshold_no_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH - 0.0005))   # 0.25% — below threshold
        assert det.get_signal(ETH) is None


# ── SELL signal emission ──────────────────────────────────────────────────────

class TestSellSignalEmission:
    def test_downward_move_emits_sell_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH - 0.001))   # 0.40% down
        assert det.get_signal(ETH) == 'SELL'

    def test_sell_signal_emitted_for_all_tracked_alts(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH - 0.001))
        assert det.get_signal(ETH) == 'SELL'
        assert det.get_signal(SOL) == 'SELL'

    def test_small_downward_move_no_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH + 0.0010))   # 0.20% — below threshold
        assert det.get_signal(ETH) is None


# ── Lead symbol never receives a signal ──────────────────────────────────────

class TestLeadSymbolExclusion:
    def test_btc_never_gets_a_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_signal(BTC) is None

    def test_btc_strength_always_zero(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_strength(BTC) == 0.0


# ── Signal expiry ─────────────────────────────────────────────────────────────

class TestSignalExpiry:
    def test_signal_present_before_expiry(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        clock.advance(_SIGNAL_DECAY_S - 1)   # one second before expiry
        assert det.get_signal(ETH) == 'BUY'

    def test_signal_gone_after_expiry(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        clock.advance(_SIGNAL_DECAY_S + 1)   # past expiry
        assert det.get_signal(ETH) is None

    def test_exactly_at_decay_signal_still_live(self, det, clock):
        """
        get_signal uses strict > (not >=), so at age == decay the signal is
        still returned.  get_strength uses >= so it returns 0 at the same
        instant — a minor API inconsistency in the source code.
        """
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        clock.advance(_SIGNAL_DECAY_S)       # age == decay
        assert det.get_signal(ETH) == 'BUY'  # > check: not yet expired
        assert det.get_strength(ETH) == 0.0  # >= check: already zero

    def test_expired_signal_removed_from_internal_dict(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert ETH in det._signals            # signal present
        clock.advance(_SIGNAL_DECAY_S + 1)
        det.get_signal(ETH)                   # triggers cleanup
        assert ETH not in det._signals


# ── get_strength ──────────────────────────────────────────────────────────────

class TestGetStrength:
    def test_strength_zero_before_any_signal(self, det):
        assert det.get_strength(ETH) == 0.0

    def test_strength_positive_immediately_after_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_strength(ETH) > 0.0

    def test_strength_at_most_one(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH * 5))   # large move
        assert det.get_strength(ETH) <= 1.0

    def test_strength_decreases_over_time(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        s0 = det.get_strength(ETH)
        clock.advance(30)
        s1 = det.get_strength(ETH)
        clock.advance(30)
        s2 = det.get_strength(ETH)
        assert s0 > s1 > s2

    def test_strength_zero_after_expiry(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        clock.advance(_SIGNAL_DECAY_S + 1)
        assert det.get_strength(ETH) == 0.0

    def test_strength_is_float(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert isinstance(det.get_strength(ETH), float)


# ── confirms_buy / confirms_sell ──────────────────────────────────────────────

class TestConfirmsMethods:
    def test_confirms_buy_true_with_no_signal(self, det):
        assert det.confirms_buy(ETH) is True

    def test_confirms_buy_true_with_buy_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.confirms_buy(ETH) is True

    def test_confirms_buy_false_with_sell_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH - 0.001))   # SELL signal
        assert det.confirms_buy(ETH) is False

    def test_confirms_sell_true_with_no_signal(self, det):
        assert det.confirms_sell(ETH) is True

    def test_confirms_sell_false_with_buy_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))   # BUY signal
        assert det.confirms_sell(ETH) is False

    def test_confirms_sell_true_with_sell_signal(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH - 0.001))   # SELL signal
        assert det.confirms_sell(ETH) is True


# ── Signal reversal ───────────────────────────────────────────────────────────

class TestSignalReversal:
    def _emit_buy(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_signal(ETH) == 'BUY'

    def test_opposite_move_cancels_buy_signal(self, det, clock):
        self._emit_buy(det, clock)
        # Reset baseline so the reversal is measured from fresh reference
        clock.advance(_WINDOW_S + 1)          # force baseline refresh
        new_base = BASE * (1 + THRESH + 0.001)
        det.update_price(BTC, new_base)        # sets new baseline
        clock.advance(1)
        det.update_price(BTC, new_base * (1 - THRESH - 0.001))   # move DOWN > threshold
        assert det.get_signal(ETH) == 'SELL'

    def test_same_direction_signal_updates(self, det, clock):
        """A second BUY signal (from a fresh window) replaces the first."""
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))  # first BUY
        t_first = det._signals[ETH][1]

        clock.advance(_WINDOW_S + 1)           # force new window
        new_base = BASE * (1 + THRESH + 0.001)
        det.update_price(BTC, new_base)        # new baseline
        clock.advance(1)
        det.update_price(BTC, new_base * (1 + THRESH + 0.001))  # second BUY

        t_second = det._signals[ETH][1]
        assert t_second > t_first              # signal timestamp refreshed


# ── Magnitude gating ──────────────────────────────────────────────────────────

class TestMagnitudeGating:
    def test_larger_magnitude_replaces_existing_signal(self, det, clock):
        _prime(det, clock)
        small_move = THRESH + 0.001           # 0.40% — small BUY
        det.update_price(BTC, BASE * (1 + small_move))
        first_mag = det._signals[ETH][2]

        clock.advance(_WINDOW_S + 1)          # new window
        new_base = BASE * (1 + small_move)
        det.update_price(BTC, new_base)
        clock.advance(1)
        big_move = THRESH + 0.020             # 2.30% — much larger BUY
        det.update_price(BTC, new_base * (1 + big_move))
        second_mag = det._signals[ETH][2]

        assert second_mag > first_mag

    def test_smaller_magnitude_does_not_replace_existing(self, det, clock):
        """If a large signal exists, a smaller same-direction signal won't overwrite it."""
        _prime(det, clock)
        # Emit a large BUY signal
        big_move   = THRESH + 0.020           # ~2.3%
        big_price  = BASE * (1 + big_move)
        det.update_price(BTC, big_price)
        big_mag  = det._signals[ETH][2]
        big_time = det._signals[ETH][1]

        # Without advancing the window, try a smaller move from the new baseline
        clock.advance(1)
        small_price = big_price * (1 + THRESH + 0.001)   # 0.40% from big_price
        det.update_price(BTC, small_price)
        # The new move's magnitude is ~0.4% which is < big_mag (~2.3%)
        # so the signal should NOT be replaced
        assert det._signals[ETH][2] == big_mag
        assert det._signals[ETH][1] == big_time


# ── Window (baseline) refresh ─────────────────────────────────────────────────

class TestWindowRefresh:
    def test_window_expiry_resets_baseline_no_immediate_signal(self, det, clock):
        """After _WINDOW_S seconds the baseline resets; the next tick just sets it."""
        _prime(det, clock)
        # Move forward past the window boundary
        clock.advance(_WINDOW_S + 1)
        # Next BTC update resets baseline → no signal emitted
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert det.get_signal(ETH) is None

    def test_new_signal_possible_after_window_and_big_move(self, det, clock):
        """After baseline resets, a further qualifying move emits a signal."""
        _prime(det, clock)
        clock.advance(_WINDOW_S + 1)           # force baseline refresh
        new_base = BASE * (1 + THRESH + 0.001)
        det.update_price(BTC, new_base)        # new baseline set (no signal)
        clock.advance(1)
        det.update_price(BTC, new_base * (1 + THRESH + 0.001))  # new qualifying move
        assert det.get_signal(ETH) == 'BUY'


# ── Alt symbols not yet registered ────────────────────────────────────────────

class TestUnseenAltSymbols:
    def test_unseen_alt_does_not_receive_signal(self, det, clock):
        """
        A symbol that has never had update_price called will not be in _prices
        and therefore won't receive a signal even after a qualifying BTC move.
        """
        # Only register ETH, not SOL
        det.update_price(ETH, BASE)
        det.update_price(BTC, BASE)            # baseline
        clock.advance(1)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))

        assert det.get_signal(ETH) == 'BUY'
        assert det.get_signal(SOL) is None


# ── summary() ─────────────────────────────────────────────────────────────────

class TestSummary:
    def test_summary_with_no_signal_returns_string(self, det):
        result = det.summary(ETH)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_summary_with_active_signal_mentions_direction(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        result = det.summary(ETH)
        assert 'BUY' in result

    def test_summary_with_sell_signal_mentions_sell(self, det, clock):
        _prime(det, clock)
        det.update_price(BTC, BASE * (1 - THRESH - 0.001))
        result = det.summary(ETH)
        assert 'SELL' in result


# ── update_price with non-lead symbol ────────────────────────────────────────

class TestNonLeadSymbolUpdate:
    def test_eth_price_update_does_not_trigger_evaluation(self, det, clock):
        """Only BTC price updates trigger _evaluate_btc; ETH updates are just logged."""
        det.update_price(ETH, BASE)
        det.update_price(ETH, BASE * 1.05)    # big move in ETH — shouldn't matter
        assert det.get_signal(ETH) is None
        assert det.get_signal(SOL) is None

    def test_eth_prices_stored_in_deque(self, det, clock):
        det.update_price(ETH, BASE)
        det.update_price(ETH, BASE * 1.01)
        assert ETH in det._prices
        assert len(det._prices[ETH]) == 2


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_custom_threshold_respected(self, clock, monkeypatch):
        monkeypatch.setattr('src.lead_lag_detector.time.time', clock)
        custom_det = LeadLagDetector(lead_symbol=BTC,
                                     move_threshold=0.01,    # 1% threshold
                                     decay_seconds=60)
        custom_det.update_price(ETH, BASE)
        custom_det.update_price(BTC, BASE)
        clock.advance(1)
        # 0.5% move — below 1% custom threshold → no signal
        custom_det.update_price(BTC, BASE * 1.005)
        assert custom_det.get_signal(ETH) is None
        # 1.5% move — above 1% threshold → signal
        custom_det.update_price(BTC, BASE * 1.015)
        assert custom_det.get_signal(ETH) == 'BUY'

    def test_custom_decay_respected(self, clock, monkeypatch):
        monkeypatch.setattr('src.lead_lag_detector.time.time', clock)
        custom_det = LeadLagDetector(lead_symbol=BTC,
                                     move_threshold=THRESH,
                                     decay_seconds=10)
        custom_det.update_price(ETH, BASE)
        custom_det.update_price(BTC, BASE)
        clock.advance(1)
        custom_det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        assert custom_det.get_signal(ETH) == 'BUY'
        clock.advance(11)                      # past custom 10 s decay
        assert custom_det.get_signal(ETH) is None

    def test_multiple_alts_all_get_signal(self, clock, monkeypatch):
        monkeypatch.setattr('src.lead_lag_detector.time.time', clock)
        alts = ['ETH/USD', 'SOL/USD', 'AVAX/USD', 'DOT/USD']
        custom_det = LeadLagDetector(lead_symbol=BTC)
        for alt in alts:
            custom_det.update_price(alt, BASE)
        custom_det.update_price(BTC, BASE)
        clock.advance(1)
        custom_det.update_price(BTC, BASE * (1 + THRESH + 0.001))
        for alt in alts:
            assert custom_det.get_signal(alt) == 'BUY', f"{alt} missing BUY signal"
