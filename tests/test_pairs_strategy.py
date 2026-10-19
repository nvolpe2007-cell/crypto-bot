"""
Tests for src/pairs_strategy.py.

This class has no live call site today (pairs_paper.py implements its own,
separate dollar-neutral pairs logic — see its module docstring), but it lives
in the directional lane and is a real, self-contained unit worth covering.
"""

from src.pairs_strategy import (
    PairsStrategy,
    MAX_DIVERGENCE_AGE,
    MIN_LEADER_MOVE,
)

SYMBOLS = ["A/USD", "B/USD"]
PAIR = ("A/USD", "B/USD")


def _strategy_with_forced_divergence(z=3.0, leader_ret=MIN_LEADER_MOVE + 0.01,
                                      lagger_ret=0.0):
    """A PairsStrategy whose z-score/return internals are stubbed so tests can
    drive _evaluate_pair() directly without simulating a full price feed."""
    strat = PairsStrategy(SYMBOLS)
    state = {"z": z, "leader_ret": leader_ret, "lagger_ret": lagger_ret}
    strat._zscore = lambda pair: state["z"]
    strat._five_min_return = lambda sym, now: (
        state["leader_ret"] if sym == "A/USD" else state["lagger_ret"]
    )
    return strat, state


class TestDivergenceFreshness:
    """Regression coverage for the staleness bug: a divergence that exceeds
    MAX_DIVERGENCE_AGE must stay blocked for as long as it persists, not
    reset and immediately re-fire as "fresh" on the next tick."""

    def test_fresh_divergence_fires(self):
        strat, _ = _strategy_with_forced_divergence()
        sig = strat._evaluate_pair(PAIR, now=1_000_000.0)
        assert sig is not None
        assert sig.leader == "A/USD"
        assert sig.lagger == "B/USD"

    def test_stale_divergence_is_blocked_and_stays_blocked(self):
        strat, _ = _strategy_with_forced_divergence()
        t0 = 1_000_000.0
        assert strat._evaluate_pair(PAIR, now=t0) is not None

        # Advance past MAX_DIVERGENCE_AGE while the divergence keeps holding.
        t1 = t0 + MAX_DIVERGENCE_AGE + 1
        assert strat._evaluate_pair(PAIR, now=t1) is None

        # Bug this guards: resetting _divergence_start to None here would let
        # the very next tick treat the still-active divergence as brand new
        # (age=0) and re-fire immediately. It must stay blocked instead.
        t2 = t1 + 1
        assert strat._evaluate_pair(PAIR, now=t2) is None
        t3 = t2 + 500
        assert strat._evaluate_pair(PAIR, now=t3) is None

    def test_divergence_rearms_after_conditions_clear_and_reform(self):
        strat, state = _strategy_with_forced_divergence()
        t0 = 1_000_000.0
        assert strat._evaluate_pair(PAIR, now=t0) is not None

        # Divergence clears (z drops back under threshold) — should reset.
        state["z"] = 0.5
        t1 = t0 + 5
        assert strat._evaluate_pair(PAIR, now=t1) is None
        assert strat._divergence_start[PAIR] is None

        # A new divergence forms — treated as fresh (age ~0), fires again.
        state["z"] = 3.0
        t2 = t1 + 5
        sig = strat._evaluate_pair(PAIR, now=t2)
        assert sig is not None
