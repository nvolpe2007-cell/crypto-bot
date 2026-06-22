"""Tests for the read-only dashboard data layer (src/dashboard_data.py)."""

import json
import os
import sqlite3

import pytest

from src import dashboard_data as dd


def _write_state(data_dir, stem, *, start, equity, closed=None, positions=None, mtm=None):
    payload = {"starting_equity": start, "equity": equity}
    if mtm is not None:
        payload["equity_mtm"] = mtm
    if closed is not None:
        payload["closed"] = closed
    if positions is not None:
        payload["positions"] = positions
    with open(os.path.join(data_dir, f"{stem}_state.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def test_collect_arms_basic_pnl(data_dir):
    _write_state(data_dir, "tsmom_paper", start=1000, equity=1080)
    rows = dd.collect_arms(data_dir)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "tsmom_50"
    assert r["pnl"] == 80.0
    assert r["pnl_pct"] == 8.0
    assert r["status"] == "idle"  # no closed trades


def test_equity_mtm_preferred_over_equity(data_dir):
    _write_state(data_dir, "brain_paper", start=1000, equity=1000, mtm=901.29)
    r = dd.collect_arms(data_dir)[0]
    assert r["equity"] == 901.29
    assert r["pnl"] == -98.71


def test_unknown_stem_falls_back_to_stem_name(data_dir):
    _write_state(data_dir, "brandnew_arm", start=500, equity=510)
    r = dd.collect_arms(data_dir)[0]
    assert r["name"] == "brandnew_arm"


def test_missing_starting_equity_is_skipped(data_dir):
    with open(os.path.join(data_dir, "junk_state.json"), "w") as fh:
        json.dump({"equity": 100}, fh)
    assert dd.collect_arms(data_dir) == []


def test_corrupt_json_is_skipped(data_dir):
    with open(os.path.join(data_dir, "bad_state.json"), "w") as fh:
        fh.write("{not json")
    assert dd.collect_arms(data_dir) == []


def test_closed_trades_count_and_winrate(data_dir):
    closed = [{"net": 5.0}, {"pnl": -2.0}, {"net_pnl": 3.0}, {"pnl_usd": -1.0}]
    _write_state(data_dir, "conf_paper", start=1000, equity=1005, closed=closed)
    r = dd.collect_arms(data_dir)[0]
    assert r["trades"] == 4
    assert r["wins"] == 2
    assert r["win_rate"] == 50.0


def test_open_positions_dict_and_list(data_dir):
    _write_state(data_dir, "a", start=1, equity=1, positions={"BTC": {}, "ETH": {}})
    _write_state(data_dir, "b", start=1, equity=1, positions=[{"x": 1}])
    rows = {r["stem"]: r for r in dd.collect_arms(data_dir)}
    assert rows["a"]["open"] == 2
    assert rows["b"]["open"] == 1


def test_proof_status_building_below_30():
    label, t = dd._proof_status([1.0] * 10)
    assert label == "building n=10<30"
    assert t is None


def test_proof_status_promising_above_30():
    label, t = dd._proof_status([5.0, 6.0, 4.0, 5.5] * 8)  # 32 positive low-variance
    assert label == "PROMISING t>2"
    assert t > 2.0


def test_proof_status_losing():
    label, t = dd._proof_status([-5.0, -6.0, -4.0, -5.5] * 8)
    assert label == "LOSING t<-2"
    assert t < -2.0


def test_proof_status_idle_empty():
    assert dd._proof_status([]) == ("idle", None)


def test_arms_sorted_by_pnl_desc(data_dir):
    _write_state(data_dir, "loser", start=1000, equity=900)
    _write_state(data_dir, "winner", start=1000, equity=1100)
    rows = dd.collect_arms(data_dir)
    assert rows[0]["stem"] == "winner"
    assert rows[-1]["stem"] == "loser"


def test_collect_attribution_reads_ledger(data_dir):
    db = os.path.join(data_dir, "attribution.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE fills (id INTEGER PRIMARY KEY, arm TEXT, gross_pnl REAL, "
        "fees_paid REAL, slippage_cost REAL, net_pnl REAL)"
    )
    con.executemany(
        "INSERT INTO fills (arm, gross_pnl, fees_paid, slippage_cost, net_pnl) VALUES (?,?,?,?,?)",
        [("brain", 10.0, 1.0, 0.5, 8.5), ("brain", 5.0, 1.0, 0.5, 3.5), ("kraken", -2.0, 1.0, 0.0, -3.0)],
    )
    con.commit()
    con.close()
    rows = {r["arm"]: r for r in dd.collect_attribution(data_dir)}
    assert rows["brain"]["fills"] == 2
    assert rows["brain"]["net"] == 12.0
    assert rows["kraken"]["net"] == -3.0


def test_collect_attribution_no_db_returns_empty(data_dir):
    assert dd.collect_attribution(data_dir) == []


def test_collect_tournament_absent_is_graceful(data_dir):
    t = dd.collect_tournament(data_dir)
    assert t["candidates"] == [] and t["summary"] == {}


def test_collect_tournament_reads_and_caps_top(data_dir):
    cands = [{"name": f"s{i}", "sharpe": 1.0 - i * 0.01} for i in range(50)]
    payload = {"generated_at": 123, "coins": ["BTC/USD"], "n_bars": 730,
               "summary": {"n_candidates": 50, "family_t_bar": 3.4, "n_robust": 2,
                           "n_passes_family": 0, "n_long_only_executable": 0},
               "candidates": cands}
    with open(os.path.join(data_dir, "tournament.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    t = dd.collect_tournament(data_dir, top=10)
    assert len(t["candidates"]) == 10
    assert t["n_total"] == 50
    assert t["summary"]["family_t_bar"] == 3.4


def test_snapshot_includes_tournament_key(data_dir):
    _write_state(data_dir, "a", start=1000, equity=1000)
    snap = dd.snapshot(data_dir)
    assert "tournament" in snap


def test_collect_readiness_absent_is_empty(data_dir):
    assert dd.collect_readiness(data_dir) == []


def test_collect_readiness_reads_from_allocator_state(data_dir):
    payload = {"starting_equity": 1000, "equity": 1000,
               "readiness": [{"name": "pairs", "n": 3, "need_more": 27, "positive": True,
                              "proven": False, "status": "on track"}]}
    with open(os.path.join(data_dir, "allocator_state.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    rd = dd.collect_readiness(data_dir)
    assert len(rd) == 1 and rd[0]["name"] == "pairs" and rd[0]["need_more"] == 27


def test_snapshot_includes_readiness_key(data_dir):
    _write_state(data_dir, "a", start=1000, equity=1000)
    assert "readiness" in dd.snapshot(data_dir)


def test_snapshot_totals(data_dir):
    _write_state(data_dir, "a", start=1000, equity=1100, positions={"BTC": {}})
    _write_state(data_dir, "b", start=1000, equity=950)
    snap = dd.snapshot(data_dir)
    assert snap["totals"]["equity"] == 2050.0
    assert snap["totals"]["pnl"] == 50.0
    assert snap["totals"]["n_arms"] == 2
    assert snap["totals"]["active"] == 1
