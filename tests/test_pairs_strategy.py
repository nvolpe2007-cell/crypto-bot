"""Tests for src/pairs_strategy.py — the (currently unwired) PairsStrategy class.

`_evaluate_pair` is exercised directly with `_zscore`/`_five_min_return` stubbed out,
so these tests pin down the divergence-age bookkeeping without depending on the
price-history deque's warm-up/timing details (covered implicitly by evaluate()
callers elsewhere).
"""
from src.pairs_strategy import PairsStrategy, MAX_DIVERGENCE_AGE


def _make_strategy():
    return PairsStrategy(symbols=["A", "B"], lookback_minutes=30)


def test_divergence_age_resets_on_leadership_flip():
    """A leadership flip (z crosses from +threshold straight to -threshold
    without dipping below threshold in between) must start a fresh age clock,
    not inherit the old direction's divergence_start."""
    strat = _make_strategy()
    pair = ("A", "B")

    returns = {"A": 0.0, "B": 0.0}
    z_value = {"z": 0.0}
    strat._zscore = lambda p: z_value["z"]
    strat._five_min_return = lambda sym, now: returns[sym]

    t0 = 1_000_000.0

    # A leads: z > threshold, A clears MIN_LEADER_MOVE, B stays within MAX_LAGGER_MOVE.
    z_value["z"] = 3.0
    returns["A"], returns["B"] = 0.02, 0.0
    sig = strat._evaluate_pair(pair, t0)
    assert sig is not None
    assert sig.leader == "A"
    assert strat._divergence_start[pair] == t0

    # Long after MAX_DIVERGENCE_AGE would have expired the OLD divergence.
    t_late = t0 + MAX_DIVERGENCE_AGE + 60

    # B leads instead: z flips sign, never crossing back below threshold.
    z_value["z"] = -3.0
    returns["A"], returns["B"] = 0.0, 0.02
    sig2 = strat._evaluate_pair(pair, t_late)

    assert sig2 is not None, (
        "a brand-new divergence in the opposite direction was rejected as "
        "stale — divergence_start was not reset on the leadership flip"
    )
    assert sig2.leader == "B"
    assert strat._divergence_start[pair] == t_late


def test_divergence_age_persists_without_flip():
    """Sanity check: age must NOT reset when the direction stays the same
    across consecutive evaluations."""
    strat = _make_strategy()
    pair = ("A", "B")

    returns = {"A": 0.02, "B": 0.0}
    z_value = {"z": 3.0}
    strat._zscore = lambda p: z_value["z"]
    strat._five_min_return = lambda sym, now: returns[sym]

    t0 = 2_000_000.0
    sig = strat._evaluate_pair(pair, t0)
    assert sig is not None
    start = strat._divergence_start[pair]

    sig2 = strat._evaluate_pair(pair, t0 + 5)
    assert sig2 is not None
    assert strat._divergence_start[pair] == start
