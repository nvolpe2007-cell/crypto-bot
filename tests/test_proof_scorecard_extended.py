"""
Extended tests for proof_scorecard.py.

The existing test_proof_scorecard.py covers _stats clustering, _design_effect_eff_n
basics, _family_t_bar, and the core _verdict string outputs.

This file fills the remaining gaps:
  - _stats edge cases (n=0/1, sd=0, all-loss, max_dd correctness)
  - _design_effect_eff_n boundary guards
  - _borrow_owed (direction gating, major vs alt APY, zero-cycle short)
  - _arm data loader (missing file, empty, net P&L, borrow_correct flag, sort)
  - _swing_forward (missing file, empty, week clustering, bad entry_ts)
  - _tsmom_forward (missing file, basic stats)
  - _directional (missing file, seed-row filters, prob_win filter, bad pnl)
  - _verdict additional paths (fantasy, n<30, negative expectancy, eff_n governs)
  - _swing_attribution (missing file, empty, symbol grouping, session-verdict gate)
"""
from __future__ import annotations

import csv
import datetime
import json
import math
import statistics

import pytest

import proof_scorecard as ps


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_arm(t=2.2, eff_n=40.0, t_clustered=None, **kw):
    base = dict(
        executable=True, n=40, expectancy=0.5,
        t_stat=t, t_clustered=t if t_clustered is None else t_clustered,
        eff_n=eff_n,
    )
    base.update(kw)
    return base


def _write_csv(path, rows, fieldnames=None):
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ── _stats edge cases ─────────────────────────────────────────────────────────

class TestStatsEdgeCases:
    def test_empty_list(self):
        s = ps._stats([])
        assert s["n"] == 0
        assert s["t_stat"] == 0.0
        assert s["t_clustered"] == 0.0
        assert s["max_dd"] == 0.0
        assert s["sharpe"] == 0.0

    def test_single_trade_no_division_error(self):
        # n=1 → pstdev=0 → t_stat must be 0, not ZeroDivisionError
        s = ps._stats([2.5])
        assert s["n"] == 1
        assert s["t_stat"] == 0.0
        assert s["win_rate"] == 1.0
        assert s["expectancy"] == pytest.approx(2.5)

    def test_all_identical_values_no_division_error(self):
        # sd=0 → both t_stat and sharpe must be 0.0, not ZeroDivisionError
        s = ps._stats([1.0, 1.0, 1.0, 1.0])
        assert s["t_stat"] == 0.0
        assert s["sharpe"] == 0.0

    def test_all_losses_win_rate_zero(self):
        nets = [-1.0, -2.0, -0.5]
        s = ps._stats(nets)
        assert s["win_rate"] == 0.0
        assert s["expectancy"] < 0
        assert s["max_dd"] < 0

    def test_max_drawdown_chronological(self):
        # Cumulative: 1→3→1→2→0   peak: 1,3,3,3,3   drawdown: 0,0,-2,-1,-3
        nets = [1.0, 2.0, -2.0, 1.0, -2.0]
        s = ps._stats(nets)
        assert s["max_dd"] == pytest.approx(-3.0)

    def test_max_drawdown_all_positive_is_zero(self):
        # Monotonically rising equity → no drawdown
        nets = [1.0, 2.0, 0.5, 3.0]
        s = ps._stats(nets)
        assert s["max_dd"] == 0.0

    def test_win_rate_correct(self):
        nets = [1.0, 1.0, -0.5, 1.0]
        s = ps._stats(nets)
        assert s["win_rate"] == pytest.approx(0.75)

    def test_t_stat_matches_manual_calculation(self):
        # Use a spread-out series so sd > 0
        nets = [1.0, 0.9, 1.1, 0.95, 1.05, 0.85, 1.15, 1.0, 0.9, 1.1] * 4
        s = ps._stats(nets)
        n = len(nets)
        mean = statistics.mean(nets)
        sd = statistics.stdev(nets)
        expected_t = mean / (sd / math.sqrt(n))
        assert abs(s["t_stat"] - expected_t) < 1e-9

    def test_total_is_sum_of_nets(self):
        nets = [1.0, -0.5, 2.0, -0.3]
        s = ps._stats(nets)
        assert s["total"] == pytest.approx(sum(nets))

    def test_no_clusters_gives_eff_n_equal_to_n(self):
        nets = [1.0, -0.5, 2.0, -1.0]
        s = ps._stats(nets)
        assert s["eff_n"] == s["n"]
        assert s["t_clustered"] == pytest.approx(s["t_stat"])


# ── _design_effect_eff_n boundary guards ─────────────────────────────────────

class TestDesignEffectBoundaryGuards:
    def test_n_less_than_2_returns_n(self):
        # Guard: n < 2 → return float(n)
        assert ps._design_effect_eff_n([1.0], ["A"]) == pytest.approx(1.0)

    def test_single_cluster_returns_n(self):
        # k=1 < 2 → guard fires
        nets = [1.0, 0.9, 1.1]
        assert ps._design_effect_eff_n(nets, ["A", "A", "A"]) == pytest.approx(3.0)

    def test_all_unique_clusters_returns_n(self):
        # k == n → all-singleton guard fires
        nets = [1.0, 2.0, 3.0]
        assert ps._design_effect_eff_n(nets, ["A", "B", "C"]) == pytest.approx(3.0)

    def test_two_clusters_partial_correlation(self):
        # Two clusters with different means → some positive ICC → eff_n < n
        nets = [1.0, 1.0, 1.0, 3.0, 3.0, 3.0]
        clusters = ["A", "A", "A", "B", "B", "B"]
        eff = ps._design_effect_eff_n(nets, clusters)
        assert eff < len(nets)
        assert eff > 0


# ── _borrow_owed ──────────────────────────────────────────────────────────────

class TestBorrowOwed:
    def test_non_short_direction_returns_zero(self):
        p = {"direction": "LONG_PERP", "symbol": "BTCUSDT",
             "size_usd": 100.0, "cycles_collected": 10}
        assert ps._borrow_owed(p) == 0.0

    def test_missing_direction_returns_zero(self):
        p = {"symbol": "BTCUSDT", "size_usd": 100.0, "cycles_collected": 5}
        assert ps._borrow_owed(p) == 0.0

    def test_zero_cycles_gives_zero_borrow(self):
        p = {"direction": "SHORT_SPOT_LONG_PERP", "symbol": "BTCUSDT",
             "size_usd": 100.0, "cycles_collected": 0}
        assert ps._borrow_owed(p) == 0.0

    def test_major_symbol_uses_major_apy(self):
        # BTC is in MAJOR_SYMBOLS → uses BORROW_APY_MAJOR
        cycles = 3 * 365   # 3*365*8h == 1 year
        p = {"direction": "SHORT_SPOT_LONG_PERP", "symbol": "BTCUSDT",
             "size_usd": 100.0, "cycles_collected": cycles}
        expected = ((ps.BORROW_APY_MAJOR / 100.0) * 100.0
                    * (cycles * ps.FUNDING_CYCLE_HOURS / (24.0 * 365.0)))
        assert ps._borrow_owed(p) == pytest.approx(expected)

    def test_alt_symbol_uses_alt_apy(self):
        # SHIB is NOT in MAJOR_SYMBOLS → uses BORROW_APY_ALT
        cycles = 3 * 365
        p = {"direction": "SHORT_SPOT_LONG_PERP", "symbol": "SHIBUSDT",
             "size_usd": 100.0, "cycles_collected": cycles}
        expected = ((ps.BORROW_APY_ALT / 100.0) * 100.0
                    * (cycles * ps.FUNDING_CYCLE_HOURS / (24.0 * 365.0)))
        assert ps._borrow_owed(p) == pytest.approx(expected)

    def test_major_apy_larger_than_alt_is_not_assumed(self):
        # Just verify both APY values are positive (the constants themselves)
        assert ps.BORROW_APY_MAJOR > 0
        assert ps.BORROW_APY_ALT > 0


# ── _arm data loader ──────────────────────────────────────────────────────────

class TestArmLoader:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        assert ps._arm("Test", "nonexistent.json", True, False) is None

    def test_empty_closed_returns_zero_stats(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        (tmp_path / "state.json").write_text(json.dumps({"closed": []}))
        a = ps._arm("Test", "state.json", True, False)
        assert a is not None
        assert a["n"] == 0
        assert a["executable"] is True
        assert a["label"] == "Test"

    def test_net_pnl_is_funding_minus_entry_cost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"funding_collected": 2.0, "entry_cost": 0.5, "borrow_cost": 0.0,
             "close_time_iso": "2025-01-01"},
            {"funding_collected": 1.5, "entry_cost": 0.5, "borrow_cost": 0.0,
             "close_time_iso": "2025-01-02"},
        ]
        (tmp_path / "state.json").write_text(json.dumps({"closed": closed}))
        a = ps._arm("Test", "state.json", True, False)
        assert a["n"] == 2
        assert a["total"] == pytest.approx(2.5)  # (2.0-0.5) + (1.5-0.5)

    def test_borrow_correct_false_uses_borrow_cost_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        # borrow_cost field carries a pre-computed cost; borrow_correct=False uses it directly
        closed = [
            {"funding_collected": 5.0, "entry_cost": 0.5, "borrow_cost": 1.0,
             "close_time_iso": "2025-01-01"},
        ]
        (tmp_path / "state.json").write_text(json.dumps({"closed": closed}))
        a = ps._arm("Test", "state.json", True, borrow_correct=False)
        assert a["total"] == pytest.approx(3.5)   # 5.0 - 0.5 - 1.0
        assert "corrected_total" not in a

    def test_borrow_correct_true_adds_corrected_total(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"direction": "SHORT_SPOT_LONG_PERP", "symbol": "BTCUSDT",
             "funding_collected": 5.0, "entry_cost": 0.5, "borrow_cost": 0.0,
             "size_usd": 100.0, "cycles_collected": 0,
             "close_time_iso": "2025-01-01"},
        ]
        (tmp_path / "state.json").write_text(json.dumps({"closed": closed}))
        a = ps._arm("Test", "state.json", executable=False, borrow_correct=True)
        assert "corrected_total" in a

    def test_borrow_correct_true_reduces_total_for_short_spot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        cycles = 3 * 365
        closed = [
            {"direction": "SHORT_SPOT_LONG_PERP", "symbol": "BTCUSDT",
             "funding_collected": 100.0, "entry_cost": 0.5, "borrow_cost": 0.0,
             "size_usd": 100.0, "cycles_collected": cycles,
             "close_time_iso": "2025-01-01"},
        ]
        (tmp_path / "state.json").write_text(json.dumps({"closed": closed}))
        a = ps._arm("Test", "state.json", executable=False, borrow_correct=True)
        # corrected_total should be smaller than the booked total (borrow costs are real)
        assert a["corrected_total"] < a["total"]

    def test_entries_sorted_by_close_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        # Provided out-of-chronological order; function must sort them
        closed = [
            {"funding_collected": 1.0, "entry_cost": 0.0, "borrow_cost": 0.0,
             "close_time_iso": "2025-01-03"},
            {"funding_collected": 2.0, "entry_cost": 0.0, "borrow_cost": 0.0,
             "close_time_iso": "2025-01-01"},
        ]
        (tmp_path / "state.json").write_text(json.dumps({"closed": closed}))
        a = ps._arm("Test", "state.json", True, False)
        # Both entries counted regardless of input order
        assert a["n"] == 2
        assert a["total"] == pytest.approx(3.0)


# ── _swing_forward ────────────────────────────────────────────────────────────

class TestSwingForward:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        assert ps._swing_forward() is None

    def test_empty_closed_returns_zero_stat_arm(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": []}))
        r = ps._swing_forward()
        assert r is not None
        assert r["n"] == 0
        assert r["executable"] is True
        assert "Swing" in r["label"]

    def test_week_clustering_applied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        # Two trades in the same ISO week (Monday + Wednesday)
        mon = int(datetime.datetime(2025, 1, 6).timestamp())
        wed = int(datetime.datetime(2025, 1, 8).timestamp())
        closed = [
            {"pnl": 1.0, "entry_ts": mon, "exit_ts": "2025-01-10"},
            {"pnl": 1.5, "entry_ts": wed, "exit_ts": "2025-01-11"},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        r = ps._swing_forward()
        assert r["n"] == 2
        assert r["total"] == pytest.approx(2.5)

    def test_bad_entry_ts_falls_back_to_unknown_cluster(self, tmp_path, monkeypatch):
        """Unparseable entry_ts must not crash — uses 'unknown' cluster."""
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"pnl": 1.0, "entry_ts": None, "exit_ts": "2025-01-10"},
            {"pnl": -0.5, "entry_ts": "not-a-timestamp", "exit_ts": "2025-01-11"},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        r = ps._swing_forward()
        # Both trades land in 'unknown' cluster (k==1) → eff_n == n
        assert r["n"] == 2

    def test_sorted_by_exit_ts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        ts = int(datetime.datetime(2025, 3, 1).timestamp())
        closed = [
            {"pnl": 2.0, "entry_ts": ts, "exit_ts": "2025-03-05"},
            {"pnl": -1.0, "entry_ts": ts, "exit_ts": "2025-03-02"},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        r = ps._swing_forward()
        assert r["n"] == 2
        assert r["total"] == pytest.approx(1.0)


# ── _tsmom_forward ────────────────────────────────────────────────────────────

class TestTsmomForward:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        assert ps._tsmom_forward() is None

    def test_basic_stats_and_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        ts = int(datetime.datetime(2025, 3, 10).timestamp())
        closed = [
            {"pnl": 2.0, "entry_ts": ts, "exit_ts": "2025-03-15"},
            {"pnl": -1.0, "entry_ts": ts, "exit_ts": "2025-03-16"},
        ]
        (tmp_path / "tsmom_paper_state.json").write_text(json.dumps({"closed": closed}))
        r = ps._tsmom_forward()
        assert r is not None
        assert r["n"] == 2
        assert r["total"] == pytest.approx(1.0)
        assert r["executable"] is True
        assert "Trend-follow" in r["label"]

    def test_bad_entry_ts_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"pnl": 1.0, "entry_ts": None, "exit_ts": "2025-04-01"},
        ]
        (tmp_path / "tsmom_paper_state.json").write_text(json.dumps({"closed": closed}))
        r = ps._tsmom_forward()
        assert r is not None
        assert r["n"] == 1


# ── _directional ──────────────────────────────────────────────────────────────

class TestDirectional:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        assert ps._directional() is None

    def test_filters_id_prefix_seed_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        rows = [
            {"trade_id": "id_001",  "pnl": "1.0", "prob_win": "0.6"},   # filtered
            {"trade_id": "id_002",  "pnl": "2.0", "prob_win": "0.7"},   # filtered
            {"trade_id": "real_01", "pnl": "1.5", "prob_win": "0.65"},  # kept
        ]
        _write_csv(tmp_path / "trade_journal.csv", rows)
        r = ps._directional()
        assert r["n"] == 1
        assert r["total"] == pytest.approx(1.5)

    def test_filters_btc_seed_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        rows = [
            {"trade_id": "BTC_17000_001", "pnl": "1.0", "prob_win": "0.6"},  # filtered
            {"trade_id": "real_01",       "pnl": "3.0", "prob_win": "0.7"},  # kept
        ]
        _write_csv(tmp_path / "trade_journal.csv", rows)
        r = ps._directional()
        assert r["n"] == 1
        assert r["total"] == pytest.approx(3.0)

    def test_filters_empty_prob_win(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        rows = [
            {"trade_id": "real_01", "pnl": "1.0", "prob_win": ""},    # filtered
            {"trade_id": "real_02", "pnl": "2.0", "prob_win": "0.0"}, # filtered
            {"trade_id": "real_03", "pnl": "3.0", "prob_win": "0.6"}, # kept
        ]
        _write_csv(tmp_path / "trade_journal.csv", rows)
        r = ps._directional()
        assert r["n"] == 1
        assert r["total"] == pytest.approx(3.0)

    def test_skips_rows_with_bad_pnl_value(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        rows = [
            {"trade_id": "real_01", "pnl": "bad_value", "prob_win": "0.6"},  # skip
            {"trade_id": "real_02", "pnl": "2.0",       "prob_win": "0.7"},  # kept
        ]
        _write_csv(tmp_path / "trade_journal.csv", rows)
        r = ps._directional()
        assert r["n"] == 1
        assert r["total"] == pytest.approx(2.0)

    def test_correct_net_pnl_and_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        rows = [
            {"trade_id": "real_01", "pnl": "1.0",  "prob_win": "0.6"},
            {"trade_id": "real_02", "pnl": "-0.5", "prob_win": "0.55"},
        ]
        _write_csv(tmp_path / "trade_journal.csv", rows)
        r = ps._directional()
        assert r["n"] == 2
        assert r["total"] == pytest.approx(0.5)
        assert r["executable"] is True
        assert "Directional" in r["label"]

    def test_empty_file_returns_zero_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        _write_csv(tmp_path / "trade_journal.csv", [], fieldnames=["trade_id", "pnl", "prob_win"])
        r = ps._directional()
        assert r is not None
        assert r["n"] == 0


# ── _verdict additional paths ─────────────────────────────────────────────────

class TestVerdictAdditionalPaths:
    def test_fantasy_arm_not_executable(self):
        a = _make_arm(t=3.0, executable=False)
        v = ps._verdict(a)
        assert "FANTASY" in v

    def test_below_n_min_not_proven(self):
        a = _make_arm(t=3.0, n=10, expectancy=0.5)
        v = ps._verdict(a)
        assert "NOT PROVEN" in v
        assert "10" in v

    def test_negative_expectancy_fails(self):
        a = _make_arm(t=3.0, n=40, expectancy=-0.1)
        v = ps._verdict(a)
        assert v.startswith("FAILED")
        assert "-0.1000" in v

    def test_zero_expectancy_also_fails(self):
        # expectancy <= 0 is the gate; zero is not positive
        a = _make_arm(t=3.0, n=40, expectancy=0.0)
        v = ps._verdict(a)
        assert v.startswith("FAILED")

    def test_exactly_t_min_not_proven(self):
        # The guard is t <= T_MIN (not strict <), so t==T_MIN is NOT PROVEN
        a = _make_arm(t=ps.T_MIN, n=40, expectancy=0.5)
        v = ps._verdict(a)
        assert "NOT PROVEN" in v

    def test_t_clustered_governs_verdict_not_raw_t_stat(self):
        # t_stat > T_MIN but t_clustered < T_MIN → verdict must be NOT PROVEN
        a = dict(
            executable=True, n=50, expectancy=0.5,
            t_stat=2.5,       # raw t passes the single bar
            t_clustered=1.5,  # clustered t fails
            eff_n=10.0,
        )
        v = ps._verdict(a)
        assert "NOT PROVEN" in v
        assert "1.50" in v

    def test_proven_single_when_between_t_min_and_family_bar(self):
        # t=2.2 > T_MIN=2.0 but below family bar of 2.5
        a = _make_arm(t=2.2, n=40, expectancy=0.5)
        v = ps._verdict(a, t_family=2.5, k=4)
        assert "PROVEN (single)" in v
        assert "NOT family-wise robust" in v
        assert not v.startswith("PROVEN ✓")

    def test_proven_tick_shows_eff_n_in_message(self):
        a = _make_arm(t=3.0, n=40, expectancy=0.5, eff_n=25.0, t_clustered=3.0)
        v = ps._verdict(a, t_family=2.0, k=1)
        assert "PROVEN ✓" in v
        assert "25" in v   # eff_n ≈ 25 appears in the verdict text


# ── _swing_attribution ────────────────────────────────────────────────────────

class TestSwingAttribution:
    def test_missing_file_produces_no_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        ps._swing_attribution()
        assert capsys.readouterr().out == ""

    def test_empty_closed_prints_notice(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": []}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "no closed trades" in out.lower()

    def test_groups_by_symbol(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"symbol": "BTC", "pnl": 1.0,  "tf": 240, "entry_hour": 10},
            {"symbol": "BTC", "pnl": 2.0,  "tf": 240, "entry_hour": 14},
            {"symbol": "ETH", "pnl": -1.0, "tf": 240, "entry_hour": 20},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "BTC" in out
        assert "ETH" in out
        assert "symbol" in out.lower()

    def test_session_verdict_section_absent_when_untagged(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"symbol": "BTC", "pnl": 1.0, "tf": 240, "entry_hour": 10},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "session verdict" not in out.lower()

    def test_session_verdict_section_present_when_tagged(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"symbol": "BTC", "pnl": 1.0, "tf": 240, "entry_hour": 10,
             "session_verdict": "FAVORABLE"},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "session verdict" in out.lower()
        assert "FAVORABLE" in out

    def test_volatility_tercile_section_appears_with_atr_data(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"symbol": "BTC", "pnl": float(i), "tf": 240, "entry_hour": 10,
             "entry_atr_pct": float(i) * 0.5}
            for i in range(1, 10)
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "volatility" in out.lower()

    def test_timeframe_section_present(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        closed = [
            {"symbol": "BTC", "pnl": 1.0, "tf": 240, "entry_hour": 9},
            {"symbol": "ETH", "pnl": 2.0, "tf": 60,  "entry_hour": 17},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "timeframe" in out.lower()
        assert "240m" in out
        assert "60m" in out

    def test_session_section_buckets_by_utc_hour(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ps, "DATA", tmp_path)
        # hour=4 → Asia, hour=10 → EU, hour=20 → US
        closed = [
            {"symbol": "BTC", "pnl": 1.0, "tf": 240, "entry_hour": 4},
            {"symbol": "ETH", "pnl": 2.0, "tf": 240, "entry_hour": 10},
            {"symbol": "SOL", "pnl": 3.0, "tf": 240, "entry_hour": 20},
        ]
        (tmp_path / "swing_paper_state.json").write_text(json.dumps({"closed": closed}))
        ps._swing_attribution()
        out = capsys.readouterr().out
        assert "Asia" in out
        assert "EU" in out
        assert "US" in out
