"""
Tests for scripts/weekly_report.py.

The script is I/O-heavy (reads CSV, JSON, log files) so every test uses
tmp_path monkeypatching to avoid touching the real data/ and logs/
directories.  All pure-logic helpers are covered directly.
"""

from __future__ import annotations

import collections
import csv
import json
import sys
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

def _write_env(tmp_path: Path) -> Path:
    env = tmp_path / ".env"
    env.write_text("")
    return env


def _import_report(tmp_path: Path, monkeypatch):
    """Import weekly_report with file-path constants redirected to tmp_path.

    weekly_report.py sets ROOT = Path(__file__).resolve().parent.parent at
    module load, so we must import it normally (using sys.path) and then
    override the path constants after the fact. The script never modifies
    those paths at runtime, so post-import patching is safe.
    """
    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = repo_root / "scripts"
    # Add both repo root and scripts dir so `import weekly_report` resolves.
    monkeypatch.syspath_prepend(str(repo_root))
    monkeypatch.syspath_prepend(str(scripts_dir))
    sys.modules.pop("weekly_report", None)

    import importlib
    mod = importlib.import_module("weekly_report")

    # Override the module-level path constants that baked in the real ROOT.
    mod.JOURNAL_CSV      = tmp_path / "data" / "trade_journal.csv"
    mod.BOT_LOG          = tmp_path / "logs" / "bot.log"
    mod.CALIBRATION_JSON = tmp_path / "logs" / "calibration.json"
    mod.FUNDING_KRAKEN   = tmp_path / "data" / "funding_arb_kraken_state.json"
    mod.WINDOW_START     = mod.NOW - timedelta(days=mod.WINDOW_DAYS)
    return mod


# ── _bucket_skip ──────────────────────────────────────────────────────────────

class TestBucketSkip:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.wr = _import_report(tmp_path, monkeypatch)

    def test_session_bucket(self):
        assert self.wr._bucket_skip("session UNFAVORABLE") == "session"

    def test_vpin_bucket(self):
        assert self.wr._bucket_skip("vpin toxicity >0.6") == "vpin_safe"

    def test_spread_bucket(self):
        assert self.wr._bucket_skip("spread 0.25% above limit") == "spread_normal"

    def test_atr_alive_bucket(self):
        assert self.wr._bucket_skip("atr_alive below floor") == "atr_alive"

    def test_kill_bucket(self):
        assert self.wr._bucket_skip("kill filter active") == "kill_filter"

    def test_cooldown_bucket(self):
        assert self.wr._bucket_skip("cooldown period") == "cooldown"

    def test_ws_stale_bucket(self):
        assert self.wr._bucket_skip("ws stale 30s") == "ws_stale"

    def test_dual_noisy_bucket(self):
        assert self.wr._bucket_skip("both directions pass") == "dual_noisy"

    def test_spot_short_bucket(self):
        assert self.wr._bucket_skip("no retail shorting on spot") == "spot_short_blocked"

    def test_size_floor_bucket(self):
        assert self.wr._bucket_skip("size below floor 0.5") == "size_floor"

    def test_ofi_bucket(self):
        assert self.wr._bucket_skip("ofi=0.15 below gate") == "ofi_aligned"

    def test_unknown_falls_through_to_first_word(self):
        assert self.wr._bucket_skip("flibbertigibbet something") == "flibbertigibbet"

    def test_empty_string_returns_unknown(self):
        assert self.wr._bucket_skip("") == "unknown"

    def test_calibrated_p_bucket(self):
        assert self.wr._bucket_skip("p=0.61 < 0.65 min") == "calibrated_p"


# ── load_recent_trades ────────────────────────────────────────────────────────

class TestLoadRecentTrades:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.wr = _import_report(tmp_path, monkeypatch)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    def _write_journal(self, rows):
        p = self.tmp / "data" / "trade_journal.csv"
        with p.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "pnl", "won", "closed_at"])
            writer.writeheader()
            writer.writerows(rows)
        self.wr.JOURNAL_CSV = p

    def test_missing_journal_returns_empty(self):
        self.wr.JOURNAL_CSV = self.tmp / "data" / "nonexistent.csv"
        assert self.wr.load_recent_trades() == []

    def test_returns_recent_rows_only(self):
        recent_ts = (self.wr.NOW - timedelta(hours=1)).isoformat()
        old_ts    = (self.wr.NOW - timedelta(days=30)).isoformat()
        self._write_journal([
            {"symbol": "BTC/USD", "pnl": "1.0", "won": "True",  "closed_at": recent_ts},
            {"symbol": "ETH/USD", "pnl": "-2.0", "won": "False", "closed_at": old_ts},
        ])
        trades = self.wr.load_recent_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BTC/USD"

    def test_all_recent_rows_returned(self):
        ts = (self.wr.NOW - timedelta(hours=2)).isoformat()
        self._write_journal([
            {"symbol": "BTC/USD", "pnl": "1.0", "won": "True",  "closed_at": ts},
            {"symbol": "SOL/USD", "pnl": "0.5", "won": "True",  "closed_at": ts},
        ])
        assert len(self.wr.load_recent_trades()) == 2

    def test_row_with_bad_timestamp_skipped(self):
        ts = (self.wr.NOW - timedelta(hours=1)).isoformat()
        self._write_journal([
            {"symbol": "BTC/USD", "pnl": "1.0", "won": "True",  "closed_at": "not-a-date"},
            {"symbol": "ETH/USD", "pnl": "2.0", "won": "True",  "closed_at": ts},
        ])
        assert len(self.wr.load_recent_trades()) == 1


# ── calibration_status ────────────────────────────────────────────────────────

class TestCalibrationStatus:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.wr = _import_report(tmp_path, monkeypatch)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    def _write_cal(self, data: dict):
        p = self.tmp / "logs" / "calibration.json"
        p.write_text(json.dumps(data))
        self.wr.CALIBRATION_JSON = p

    def test_missing_file_returns_inactive(self):
        self.wr.CALIBRATION_JSON = self.tmp / "logs" / "no_file.json"
        r = self.wr.calibration_status([])
        assert r["active"] is False

    def test_corrupt_json_returns_inactive(self):
        p = self.tmp / "logs" / "bad.json"
        p.write_text("{not valid json")
        self.wr.CALIBRATION_JSON = p
        r = self.wr.calibration_status([])
        assert r["active"] is False

    def test_empty_knots_returns_inactive(self):
        self._write_cal({"x": [], "y": [], "n_fit": 0, "n_seen": 5})
        r = self.wr.calibration_status([])
        assert r["active"] is False

    def test_active_with_valid_knots(self):
        self._write_cal({"x": [0.5, 0.7], "y": [0.45, 0.65], "n_fit": 40, "shrink": 0.8, "n_seen": 50})
        r = self.wr.calibration_status([])
        assert r["active"] is True
        assert r["n_fit"] == 40

    def test_active_computes_brier_scores_from_trades(self):
        self._write_cal({"x": [0.5, 0.7], "y": [0.45, 0.65], "n_fit": 40, "shrink": 0.8, "n_seen": 50})
        trades = [
            {"prob_win": "0.6", "won": "True"},
            {"prob_win": "0.6", "won": "False"},
            {"prob_win": "0.6", "won": "True"},
        ]
        r = self.wr.calibration_status(trades)
        assert "brier_raw" in r
        assert "brier_cal" in r
        assert r["brier_raw"] > 0

    def test_skips_trades_with_zero_prob(self):
        self._write_cal({"x": [0.5, 0.7], "y": [0.45, 0.65], "n_fit": 40, "shrink": 0.8, "n_seen": 50})
        trades = [{"prob_win": "0.0", "won": "True"}, {"prob_win": "", "won": "False"}]
        r = self.wr.calibration_status(trades)
        assert "brier_raw" not in r  # no resolved trades with non-zero prob

    def test_degenerate_detection_all_losers(self):
        self._write_cal({"x": [0.5, 0.5], "y": [0.5, 0.5], "n_fit": 40, "shrink": 0.8, "n_seen": 50})
        trades = [{"prob_win": "0.6", "won": "False"}] * 20
        r = self.wr.calibration_status(trades)
        assert r.get("calibrator_degenerate") is True

    def test_not_degenerate_with_mixed_outcomes(self):
        self._write_cal({"x": [0.5, 0.7], "y": [0.45, 0.65], "n_fit": 40, "shrink": 0.8, "n_seen": 50})
        trades = (
            [{"prob_win": "0.6", "won": "True"}] * 10
            + [{"prob_win": "0.6", "won": "False"}] * 10
        )
        r = self.wr.calibration_status(trades)
        assert r.get("calibrator_degenerate") is False


# ── funding_kraken_status ─────────────────────────────────────────────────────

class TestFundingKrakenStatus:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.wr = _import_report(tmp_path, monkeypatch)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    def _write_state(self, data: dict):
        p = self.tmp / "data" / "funding_arb_kraken_state.json"
        p.write_text(json.dumps(data))
        self.wr.FUNDING_KRAKEN = p

    def test_missing_file_returns_unavailable(self):
        self.wr.FUNDING_KRAKEN = self.tmp / "data" / "nope.json"
        r = self.wr.funding_kraken_status()
        assert r == {"available": False}

    def test_empty_state(self):
        self._write_state({"open": {}, "closed": []})
        r = self.wr.funding_kraken_status()
        assert r["available"] is True
        assert r["open_count"] == 0
        assert r["closed_total"] == 0
        assert r["cum_net_pnl"] == 0.0

    def test_cum_pnl_sums_closed_positions(self):
        self._write_state({
            "open": {},
            "closed": [
                {"funding_collected": 10.0, "entry_cost": 3.0, "close_time_iso": ""},
                {"funding_collected":  5.0, "entry_cost": 2.0, "close_time_iso": ""},
            ],
        })
        r = self.wr.funding_kraken_status()
        assert r["cum_net_pnl"] == pytest.approx(10.0)

    def test_closed_in_window_counts_recent(self):
        recent = (self.wr.NOW - timedelta(hours=1)).isoformat()
        old    = (self.wr.NOW - timedelta(days=30)).isoformat()
        self._write_state({
            "open": {},
            "closed": [
                {"funding_collected": 1.0, "entry_cost": 0.1, "close_time_iso": recent},
                {"funding_collected": 1.0, "entry_cost": 0.1, "close_time_iso": old},
            ],
        })
        r = self.wr.funding_kraken_status()
        assert r["closed_in_window"] == 1
        assert r["closed_total"] == 2

    def test_open_unrealized_included(self):
        self._write_state({
            "open": {
                "BTC/USD": {"funding_collected": 5.0, "entry_cost": 1.0},
            },
            "closed": [],
        })
        r = self.wr.funding_kraken_status()
        assert r["open_count"] == 1
        assert r["open_unrealized"] == pytest.approx(4.0)

    def test_corrupt_file_returns_unavailable(self):
        p = self.tmp / "data" / "bad.json"
        p.write_text("{not json")
        self.wr.FUNDING_KRAKEN = p
        r = self.wr.funding_kraken_status()
        assert r["available"] is False


# ── recommendations ────────────────────────────────────────────────────────────

class TestRecommendations:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.wr = _import_report(tmp_path, monkeypatch)

    def _empty_log(self):
        return {"skips": Counter(), "triarb_count": 0, "triarb_best_bps": None,
                "triarb_total_pnl": 0.0, "funnel": {}, "dual_flip": 0, "dual_reject": 0}

    def test_no_trades_no_skips_warns_about_bot_down(self):
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 0}, {})
        text = " ".join(recs)
        assert "not be running" in text or "No trades" in text

    def test_dominant_skip_reason_flagged_at_60pct(self):
        from collections import Counter
        log = self._empty_log()
        log["skips"] = Counter({"atr_alive": 70, "spread_normal": 10, "session": 20})
        recs = self.wr.recommendations([], log, {"active": False, "n_fit": 0}, {})
        text = " ".join(recs)
        assert "atr_alive" in text

    def test_calibration_inactive_flagged(self):
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 5}, {})
        assert any("not active" in r.lower() or "inactive" in r.lower() for r in recs)

    def test_calibration_helping(self):
        cal = {"active": True, "brier_lift_pct": 10.0, "calibrator_degenerate": False}
        recs = self.wr.recommendations([], self._empty_log(), cal, {})
        assert any("✅" in r or "helping" in r.lower() for r in recs)

    def test_calibration_hurting(self):
        cal = {"active": True, "brier_lift_pct": -5.0, "calibrator_degenerate": False}
        recs = self.wr.recommendations([], self._empty_log(), cal, {})
        assert any("hurting" in r.lower() or "worse" in r.lower() for r in recs)

    def test_degenerate_calibration_warns(self):
        cal = {"active": True, "brier_lift_pct": 99.0, "calibrator_degenerate": True, "win_rate_in_window": 0.0}
        recs = self.wr.recommendations([], self._empty_log(), cal, {})
        assert any("DEGENERATE" in r for r in recs)

    def test_no_triarb_opps_noted(self):
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 0}, {})
        assert any("triarb" in r.lower() or "TRIARB" in r for r in recs)

    def test_positive_kraken_funding_noted(self):
        kraken = {"available": True, "cum_net_pnl": 5.0, "open_unrealized": 1.0, "open_count": 1}
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 0}, kraken)
        assert any("💰" in r or "Kraken" in r for r in recs)

    def test_negative_kraken_funding_warns(self):
        kraken = {"available": True, "cum_net_pnl": -3.0, "open_unrealized": 0.0, "open_count": 0}
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 0}, kraken)
        assert any("negative" in r.lower() or "⚠️" in r for r in recs)

    def test_returns_at_least_one_rec(self):
        recs = self.wr.recommendations([], self._empty_log(), {"active": False, "n_fit": 0}, {})
        assert len(recs) >= 1


# ── proof_status ──────────────────────────────────────────────────────────────

class TestProofStatus:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.wr = _import_report(tmp_path, monkeypatch)

    def test_no_data_files_returns_no_arms(self, tmp_path, monkeypatch):
        # With no data/*.json files proof_scorecard returns no arms
        import proof_scorecard as ps
        monkeypatch.setattr(ps, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        lines = self.wr.proof_status()
        assert lines == ["no arms with data yet"]

    def test_uses_build_arms_not_hardcoded_list(self, tmp_path, monkeypatch):
        """proof_status() must call ps.build_arms(), not a private arm list."""
        import proof_scorecard as ps
        called = []
        original = ps.build_arms

        def _mock():
            called.append(True)
            return [], 0, ps._family_t_bar(0)

        monkeypatch.setattr(ps, "build_arms", _mock)
        self.wr.proof_status()
        assert called, "proof_status() did not call proof_scorecard.build_arms()"

    def test_includes_family_bar_in_first_line(self, tmp_path, monkeypatch):
        import proof_scorecard as ps

        fake_arm = {
            "label": "Test arm", "n": 40, "total": 10.0, "win_rate": 0.6,
            "expectancy": 0.25, "t_stat": 3.5, "t_clustered": 3.5, "eff_n": 40,
            "sharpe": 1.2, "max_dd": -5.0, "skew": 0.1, "kurt": 3.0,
            "executable": True,
        }
        monkeypatch.setattr(ps, "build_arms", lambda: ([fake_arm], 1, ps._family_t_bar(1)))
        lines = self.wr.proof_status()
        assert any("family-wise bar" in l for l in lines)
        assert any("k=1" in l for l in lines)

    def test_arm_verdict_line_included(self, tmp_path, monkeypatch):
        import proof_scorecard as ps

        fake_arm = {
            "label": "My arm", "n": 40, "total": 10.0, "win_rate": 0.6,
            "expectancy": 0.25, "t_stat": 3.5, "t_clustered": 3.5, "eff_n": 40,
            "sharpe": 1.2, "max_dd": -5.0, "skew": 0.1, "kurt": 3.0,
            "executable": True,
        }
        monkeypatch.setattr(ps, "build_arms", lambda: ([fake_arm], 1, ps._family_t_bar(1)))
        lines = self.wr.proof_status()
        assert any("My arm" in l for l in lines)


# ── render_report ─────────────────────────────────────────────────────────────

class TestRenderReport:
    @pytest.fixture(autouse=True)
    def _mod(self, tmp_path, monkeypatch):
        self.wr = _import_report(tmp_path, monkeypatch)

    def _empty_log(self):
        return {"skips": Counter(), "triarb_count": 0, "triarb_best_bps": None,
                "triarb_total_pnl": 0.0, "funnel": {}, "dual_flip": 0, "dual_reject": 0}

    def test_render_contains_all_section_headers(self):
        report = self.wr.render_report(
            [], self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        for sec in ["1. Trades", "2. Skip reasons", "3. Triangular arb",
                    "4. Calibration", "5. Funding arb", "6. Recommendations",
                    "7. Proof scorecard", "8. Session edge"]:
            assert sec in report, f"Section '{sec}' missing from report"

    def test_render_includes_trade_counts(self):
        from collections import Counter
        trades = [
            {"symbol": "BTC/USD", "pnl": "5.0",  "won": "True"},
            {"symbol": "BTC/USD", "pnl": "-2.0", "won": "False"},
        ]
        report = self.wr.render_report(
            trades, self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "2 executed" in report
        assert "1W/1L" in report
        assert "win 50.0%" in report

    def test_render_no_trades_shows_zero(self):
        report = self.wr.render_report(
            [], self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "0 executed" in report

    def test_render_includes_pnl_total(self):
        trades = [
            {"symbol": "BTC/USD", "pnl": "3.50", "won": "True"},
            {"symbol": "ETH/USD", "pnl": "-1.25", "won": "False"},
        ]
        report = self.wr.render_report(
            trades, self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "+2.25" in report or "2.25" in report

    def test_render_dual_direction_block_when_nonzero(self):
        log = self._empty_log()
        log["dual_flip"] = 3
        log["dual_reject"] = 7
        report = self.wr.render_report(
            [], log, {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "Dual-direction probe" in report
        assert "3 signal flips" in report

    def test_render_no_dual_direction_block_when_zero(self):
        report = self.wr.render_report(
            [], self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "Dual-direction probe" not in report

    def test_render_funding_missing_shows_state_missing(self):
        report = self.wr.render_report(
            [], self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, {}
        )
        assert "state file missing" in report

    def test_render_funding_available_shows_counts(self):
        kraken = {
            "available": True, "open_count": 2, "closed_in_window": 1,
            "cum_net_pnl": 4.56, "open_unrealized": 0.12,
        }
        report = self.wr.render_report(
            [], self._empty_log(), {"active": False, "n_fit": 0, "n_seen": 0}, kraken
        )
        assert "2 open" in report
        assert "+4.56" in report or "4.56" in report


# ── build_arms round-trip ─────────────────────────────────────────────────────

class TestBuildArms:
    """Smoke-test that proof_scorecard.build_arms() returns the canonical list
    structure and that weekly_report uses it (not a private hardcoded subset)."""

    def test_build_arms_returns_tuple_of_three(self, tmp_path, monkeypatch):
        import proof_scorecard as ps
        monkeypatch.setattr(ps, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        result = ps.build_arms()
        assert isinstance(result, tuple) and len(result) == 3
        arms, k, t_family = result
        assert isinstance(arms, list)
        assert isinstance(k, int) and k >= 0
        assert isinstance(t_family, float) and t_family >= 0

    def test_build_arms_with_no_data_returns_empty(self, tmp_path, monkeypatch):
        import proof_scorecard as ps
        monkeypatch.setattr(ps, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        arms, k, _ = ps.build_arms()
        assert arms == []
        assert k == 0

    def test_build_arms_k_matches_arms_length(self, tmp_path, monkeypatch):
        import proof_scorecard as ps
        monkeypatch.setattr(ps, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        arms, k, _ = ps.build_arms()
        assert k == len(arms)

    def test_main_still_runs_after_refactor(self, tmp_path, monkeypatch, capsys):
        """proof_scorecard.main() should run without error after the refactor."""
        import proof_scorecard as ps
        monkeypatch.setattr(ps, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        ps.main()   # should not raise
