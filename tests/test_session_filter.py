"""Unit tests for src/session_filter.py — the time-of-day edge gate.

Verifies the verdict logic (fail-open on warm-up, UNFAVORABLE only on a real
losing sample, FAVORABLE on positive realised expectancy), the session
bucketing, and the from_state / record-loading constructors.
"""
from src.session_filter import SessionEdge, session_of_hour, _wilson_lower_bound, SESSIONS


def _recs(hour, n, pnl, won):
    return [{"hour": hour, "pnl": pnl, "won": won} for _ in range(n)]


class TestSessionBucketing:
    def test_asia_eu_us_boundaries(self):
        assert session_of_hour(0) == "Asia"
        assert session_of_hour(7) == "Asia"
        assert session_of_hour(8) == "EU"
        assert session_of_hour(15) == "EU"
        assert session_of_hour(16) == "US"
        assert session_of_hour(23) == "US"

    def test_wraps_and_handles_bad_input(self):
        assert session_of_hour(24) == "Asia"     # 24 % 24 == 0
        assert session_of_hour(None) is None
        assert session_of_hour("nope") is None


class TestVerdicts:
    def test_empty_is_neutral_failopen(self):
        se = SessionEdge(records=[])
        for s in SESSIONS:
            assert se.session_stats()[s]["verdict"] == "NEUTRAL"
        assert se.verdict_for_hour(3) == "NEUTRAL"

    def test_small_sample_is_neutral(self):
        # 5 losing Asia trades — below the 20-sample floor → NEUTRAL.
        se = SessionEdge(records=_recs(3, 5, -1.0, False), min_samples=20)
        assert se.verdict_for_hour(3) == "NEUTRAL"

    def test_real_losing_sample_is_unfavorable(self):
        # 25 losing Asia trades: expectancy<0 AND wilson_lb(0/25) < floor.
        se = SessionEdge(records=_recs(3, 25, -1.0, False), min_samples=20)
        assert se.verdict_for_hour(3) == "UNFAVORABLE"

    def test_positive_expectancy_is_favorable(self):
        se = SessionEdge(records=_recs(18, 25, 1.0, True), min_samples=20)
        assert se.verdict_for_hour(18) == "FAVORABLE"

    def test_negative_expectancy_but_high_winrate_not_condemned(self):
        # 24 small wins + 1 huge loss → expectancy<0 but win-rate Wilson-LB is
        # well above the floor, so the window is NOT condemned (NEUTRAL).
        recs = _recs(18, 24, 0.5, True) + _recs(18, 1, -50.0, False)
        se = SessionEdge(records=recs, min_samples=20)
        assert se.verdict_for_hour(18) != "UNFAVORABLE"

    def test_sessions_are_independent(self):
        recs = _recs(3, 25, -1.0, False) + _recs(18, 25, 1.0, True)
        se = SessionEdge(records=recs, min_samples=20)
        assert se.verdict_for_hour(3) == "UNFAVORABLE"
        assert se.verdict_for_hour(18) == "FAVORABLE"
        assert se.verdict_for_hour(10) == "NEUTRAL"   # EU empty


class TestFromState:
    def test_extracts_entry_hour_and_won(self):
        state = {"closed": [
            {"entry_hour": 3, "pnl": -1.0, "won": False},
            {"entry_hour": 3, "pnl": -2.0, "won": False},
            {"entry_hour": 99, "pnl": 1.0},     # bad hour wraps to 99%24=3
            {"pnl": 1.0},                        # no entry_hour → dropped
        ]}
        se = SessionEdge.from_state(state, min_samples=1)
        stats = se.session_stats()
        assert stats["Asia"]["n"] == 3
        assert stats["Asia"]["wins"] == 1          # the 99-hour winner

    def test_won_inferred_from_pnl_when_missing(self):
        state = {"closed": [{"entry_hour": 18, "pnl": 5.0}]}
        se = SessionEdge.from_state(state, min_samples=1)
        assert se.session_stats()["US"]["wins"] == 1


class TestWilson:
    def test_lower_bound_below_point_estimate(self):
        # 18/20 wins: point 0.9, lower bound strictly below and < 1.
        lb = _wilson_lower_bound(18, 20)
        assert 0.0 < lb < 0.9

    def test_zero_n_is_zero(self):
        assert _wilson_lower_bound(0, 0) == 0.0
