"""
Tests for src/pairs_strategy.py (statistical-arbitrage pairs signal).

Covers a bug where `_zscore()` returned `None` whenever the rolling spread
window's std collapsed to ~0 (a fully mean-reverted spread). `_evaluate_pair`
treats `None` the same as a sub-threshold z-score, so that's harmless on the
entry side — but `should_exit()` also uses `_zscore()` and treats `None` as
"not reverted yet", so a converged spread (the textbook exit condition) could
never trigger an exit.
"""

import time
from collections import deque

from src.pairs_strategy import PairsStrategy, EXIT_ZSCORE, ZSCORE_THRESHOLD

PAIR = ("BTC/USD", "ETH/USD")


def _seeded_strategy(spreads, start_ts=0.0, step=5.0):
    strat = PairsStrategy(["BTC/USD", "ETH/USD"])
    hist = deque()
    for i, s in enumerate(spreads):
        hist.append((start_ts + i * step, s))
    strat._spread_history[PAIR] = hist
    return strat


class TestZscoreDegenerateStd:
    def test_zscore_is_zero_not_none_when_window_is_flat(self):
        # 12 identical samples -> std == 0 exactly, the fully-converged case.
        strat = _seeded_strategy([0.0] * 12)
        assert strat._zscore(PAIR) == 0.0

    def test_zscore_is_none_on_insufficient_history(self):
        # Unrelated guard (len(hist) < 10) must still behave as before.
        strat = _seeded_strategy([0.0] * 5)
        assert strat._zscore(PAIR) is None


class TestShouldExitOnConvergedSpread:
    def test_fires_once_spread_fully_converges(self):
        strat = _seeded_strategy([0.0] * 12)
        assert strat.should_exit("ETH/USD", "BTC/USD") is True

    def test_fires_regardless_of_which_symbol_is_passed_as_lagger(self):
        strat = _seeded_strategy([0.0] * 12)
        assert strat.should_exit("BTC/USD", "ETH/USD") is True

    def test_does_not_fire_on_a_genuinely_open_divergence(self):
        # Spread still has real variance and the latest point is far from
        # the mean -> should_exit must stay False.
        spreads = [0.0] * 11 + [1.0]
        strat = _seeded_strategy(spreads)
        assert strat.should_exit("ETH/USD", "BTC/USD") is False


class TestEntryGatingUnaffectedByDegenerateStd:
    def test_flat_spread_never_produces_an_entry_signal(self):
        # A flat (non-diverged) spread must still refuse to signal an entry
        # -- the fix must not loosen _evaluate_pair's ZSCORE_THRESHOLD gate.
        strat = _seeded_strategy([0.0] * 12)
        assert 0.0 < ZSCORE_THRESHOLD  # sanity: 0 fails the entry bar
        sig = strat._evaluate_pair(PAIR, time.time())
        assert sig is None
