"""Tests for the proof-gated allocator (src/allocator.py)."""

import json
import os

import pytest

from src import allocator as AL
from src.allocator import AllocConfig, MetaAllocator, score_arms, target_weights


def _write_arm(data_dir, fname, pnls, start_equity=1000.0, week0=1_700_000_000, spacing=604800):
    closed = []
    for i, p in enumerate(pnls):
        ts = week0 + i * spacing
        closed.append({"pnl": p, "entry_ts": ts, "exit_ts": ts + 3600})
    payload = {"starting_equity": start_equity, "equity": start_equity + sum(pnls), "closed": closed}
    with open(os.path.join(data_dir, fname), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


# ── reading + scoring ──────────────────────────────────────────────────────────
def test_read_arm_record_absent(data_dir):
    assert AL.read_arm_record(data_dir, "nope_state.json", "week") is None


def test_read_arm_record_parses(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [1.0, -0.5, 2.0])
    rec = AL.read_arm_record(data_dir, "swing_paper_state.json", "week")
    assert rec["nets"] == [1.0, -0.5, 2.0]
    assert len(rec["clusters"]) == 3
    assert rec["equity"] == pytest.approx(1002.5)


def test_score_arms_nothing_proven_when_too_few(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [1.0, 2.0, 1.5])  # n=3 < 30
    rows = score_arms(data_dir)
    assert len(rows) == 1
    assert rows[0]["proven"] is False
    assert rows[0]["n"] == 3


def test_score_arms_marks_strong_arm_proven(data_dir):
    # 35 positive, low-variance trades across distinct weeks → clustered t huge
    _write_arm(data_dir, "swing_paper_state.json", [1.0 + (i % 3) * 0.05 for i in range(35)])
    rows = score_arms(data_dir)
    swing = next(r for r in rows if r["name"] == "swing")
    assert swing["n"] == 35
    assert swing["expectancy"] > 0
    assert swing["proven"] is True


def test_score_arms_losing_arm_not_proven(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [-1.0 - (i % 3) * 0.05 for i in range(35)])
    swing = next(r for r in score_arms(data_dir) if r["name"] == "swing")
    assert swing["proven"] is False
    assert swing["expectancy"] < 0


# ── target weights ──────────────────────────────────────────────────────────────
def _scored(name="swing", family="trend", executable=True, proven=True, max_dd=-0.1):
    return {"name": name, "family": family, "executable": executable, "equity": 1000.0,
            "start": 1000.0, "n": 35, "expectancy": 0.5, "t_clustered": 4.0,
            "sharpe": 1.0, "max_dd": max_dd, "proven": proven, "t_family": 2.0}


def test_target_weights_all_cash_when_none_proven():
    assert target_weights([_scored(proven=False)], "TRENDING_UP", AllocConfig()) == {}


def test_target_weights_funds_proven_arm():
    w = target_weights([_scored()], "TRENDING_UP", AllocConfig())
    assert w.get("swing", 0) > 0


def test_target_weights_excludes_non_executable_when_executable_only():
    w = target_weights([_scored(executable=False)], "TRENDING_UP", AllocConfig(executable_only=True))
    assert w == {}
    w2 = target_weights([_scored(executable=False)], "TRENDING_UP", AllocConfig(executable_only=False))
    assert w2.get("swing", 0) > 0


def test_target_weights_respects_per_arm_cap():
    arms = [_scored(name=f"a{i}") for i in range(5)]
    w = target_weights(arms, "TRENDING_UP", AllocConfig(per_arm_cap=0.30))
    assert all(v <= 0.30 + 1e-9 for v in w.values())


def test_target_weights_crash_regime_reduces_gross():
    arms = [_scored(name=f"a{i}") for i in range(3)]
    up = sum(target_weights(arms, "TRENDING_UP", AllocConfig()).values())
    crash = sum(target_weights(arms, "CRASH", AllocConfig()).values())
    assert crash < up


# ── MetaAllocator dynamics ──────────────────────────────────────────────────────
def test_allocator_sits_in_cash_when_nothing_proven():
    a = MetaAllocator(cfg=AllocConfig())
    d = a.update([_scored(proven=False)], "TRENDING_UP")
    assert a.weights == {}
    assert d["cash_pct"] == 100.0
    assert a.equity == 1000.0


def test_allocator_persistence_gate_delays_adoption():
    a = MetaAllocator(cfg=AllocConfig(confirm_ticks=3))
    for _ in range(2):
        a.update([_scored()], "TRENDING_UP")
        assert a.weights == {}           # not yet adopted
    a.update([_scored()], "TRENDING_UP")  # third consecutive → adopt
    assert a.weights.get("swing", 0) > 0


def test_allocator_adopts_immediately_with_confirm_one():
    a = MetaAllocator(cfg=AllocConfig(confirm_ticks=1))
    a.update([_scored()], "TRENDING_UP")
    assert a.weights.get("swing", 0) > 0


def test_allocator_tracks_weighted_return():
    a = MetaAllocator(cfg=AllocConfig(confirm_ticks=1, switch_cost_frac=0.0))
    a.update([_scored()], "TRENDING_UP")           # adopt weight, set last_equity=1000
    w = a.weights["swing"]
    a.update([_scored() | {"equity": 1100.0}], "TRENDING_UP")  # arm +10%
    assert a.equity == pytest.approx(1000.0 * (1 + w * 0.10), rel=1e-6)


def test_allocator_drawdown_demote_benches_arm():
    a = MetaAllocator(cfg=AllocConfig(confirm_ticks=1, arm_dd_cap=0.25, switch_cost_frac=0.0))
    a.update([_scored()], "TRENDING_UP")                      # peak=1000, weight on
    assert a.weights.get("swing", 0) > 0
    d = a.update([_scored() | {"equity": 700.0}], "TRENDING_UP")  # -30% > 25% cap
    assert a.weights.get("swing", 0.0) == 0.0
    assert "swing" in d["demoted"]


def test_allocator_state_roundtrip():
    a = MetaAllocator(cfg=AllocConfig(confirm_ticks=1))
    a.update([_scored()], "TRENDING_UP")
    state = a.to_state()
    assert state["starting_equity"] == 1000.0
    assert "swing" in state["positions"]
    b = MetaAllocator.from_state(state, AllocConfig(confirm_ticks=1))
    assert b.weights == a.weights
    assert b.peak == a.peak
    assert b.equity == pytest.approx(a.equity)


def test_read_arm_record_exposes_entry_ts(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [1.0, 2.0], week0=1_700_000_000)
    rec = AL.read_arm_record(data_dir, "swing_paper_state.json", "week")
    assert rec["entry_ts"] == [1_700_000_000, 1_700_000_000 + 604800]


def test_switch_readiness_negative_arm_flagged_needs_edge(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [-1.0 - (i % 3) * 0.1 for i in range(10)])
    rd = {r["name"]: r for r in AL.switch_readiness(data_dir)}
    assert rd["swing"]["positive"] is False
    assert "edge" in rd["swing"]["status"]
    assert rd["swing"]["eta_days"] is None  # time won't help a losing arm


def test_switch_readiness_positive_low_n_has_eta(data_dir):
    # 5 positive trades, one per week → cadence ~1/wk, needs 25 more → ETA set
    _write_arm(data_dir, "pairs_paper_state.json", [2.0, 2.1, 1.9, 2.0, 2.2])
    rd = {r["name"]: r for r in AL.switch_readiness(data_dir)}
    p = rd["pairs"]
    assert p["positive"] is True
    assert p["need_more"] == 25
    assert p["eta_days"] is not None and p["eta_days"] > 0


def test_switch_readiness_proven_arm_eligible(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [1.0 + (i % 3) * 0.05 for i in range(35)])
    rd = {r["name"]: r for r in AL.switch_readiness(data_dir)}
    assert rd["swing"]["proven"] is True
    assert "PROVEN" in rd["swing"]["status"]


def test_switch_readiness_sorted_proven_then_positive_first(data_dir):
    _write_arm(data_dir, "swing_paper_state.json", [-2.0] * 10)            # losing
    _write_arm(data_dir, "pairs_paper_state.json", [2.0, 2.1, 1.9, 2.0])   # positive small-n
    rd = AL.switch_readiness(data_dir)
    names = [r["name"] for r in rd]
    assert names.index("pairs") < names.index("swing")  # positive ranks above losing


def test_to_state_has_dashboard_shape(data_dir):
    a = MetaAllocator(cfg=AllocConfig())
    a.update([_scored(proven=False)], "RANGING")
    s = a.to_state()
    # dashboard_data.collect_arms requires starting_equity; reads equity_mtm/equity
    assert {"starting_equity", "equity", "equity_mtm", "positions", "closed"} <= set(s)
