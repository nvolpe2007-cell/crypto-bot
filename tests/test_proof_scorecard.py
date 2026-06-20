"""
Tests for proof_scorecard.py — specifically the correlation-adjusted (clustered)
t-stat that stops a correlated universe from manufacturing significance.
"""
import proof_scorecard as ps


def test_no_clusters_matches_raw():
    nets = [1.0, -0.5, 2.0, -1.0, 1.5, 0.5]
    s = ps._stats(nets)
    assert s['eff_n'] == s['n']
    assert s['t_clustered'] == s['t_stat']


def test_all_singleton_clusters_no_penalty():
    # Every trade in its own cluster → fully independent → eff_n == n.
    nets = [1.0, -0.5, 2.0, -1.0, 1.5, 0.5, 0.7, -0.3]
    clusters = [f"w{i}" for i in range(len(nets))]
    s = ps._stats(nets, clusters)
    assert s['eff_n'] == len(nets)
    assert abs(s['t_clustered'] - s['t_stat']) < 1e-9


def test_correlated_clusters_shrink_eff_n_and_t():
    # 30 trades in 3 tight clusters: near-zero within-cluster spread, so almost
    # all variance is BETWEEN clusters → high ICC → ~3 independent bets, not 30.
    nets = ([1.00, 1.01, 0.99] * 5      # cluster A ~1.0
            + [1.40, 1.41, 1.39] * 5    # cluster B ~1.4
            + [0.60, 0.61, 0.59] * 5)   # cluster C ~0.6
    clusters = (['A'] * 15 + ['B'] * 15 + ['C'] * 15)
    s = ps._stats(nets, clusters)
    assert s['n'] == 45
    assert s['eff_n'] < 6                      # collapses toward the 3 clusters
    assert s['t_clustered'] < s['t_stat']      # significance is discounted
    # raw t would scream significance; clustered must be far smaller
    assert s['t_stat'] / max(s['t_clustered'], 1e-9) > 2


def test_eff_n_helper_floors_negative_icc():
    # Anti-correlated within clusters (within var >> between) → ICC floored at 0
    # → no penalty, eff_n ≈ n.
    nets = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    clusters = ['A', 'A', 'B', 'B', 'C', 'C']
    eff = ps._design_effect_eff_n(nets, clusters)
    assert eff <= len(nets) + 1e-9
    assert eff > len(nets) * 0.8               # negligible penalty


# ── family-wise (multiple-testing) correction ────────────────────────────────

def test_family_bar_k1_reproduces_original():
    # k<=1 must return the pre-registered T_MIN exactly (no regression).
    assert ps._family_t_bar(1) == ps.T_MIN
    assert ps._family_t_bar(0) == ps.T_MIN


def test_family_bar_rises_with_k():
    # More strategies tested → stricter per-arm bar (Šidák tightening).
    assert ps._family_t_bar(6) > ps._family_t_bar(2) > ps._family_t_bar(1)
    assert ps._family_t_bar(2) > ps.T_MIN


def _arm(t):
    return dict(executable=True, n=40, expectancy=0.5, t_stat=t, t_clustered=t,
                eff_n=40.0)


def test_verdict_default_reproduces_proven():
    # Default call (t_family=T_MIN, k=1): clears the single-arm bar → PROVEN ✓.
    v = ps._verdict(_arm(2.2))
    assert v.startswith('PROVEN ✓')


def test_verdict_clears_single_but_not_family():
    # t=2.2 clears T_MIN=2.0 but not a family bar of 2.5 → 'PROVEN (single)'.
    v = ps._verdict(_arm(2.2), t_family=2.5, k=6)
    assert v.startswith('PROVEN (single)')
    assert 'NOT family-wise robust' in v
    assert not v.startswith('PROVEN ✓')


def test_verdict_clears_family_bar():
    v = ps._verdict(_arm(3.0), t_family=2.5, k=6)
    assert v.startswith('PROVEN ✓')
    assert 'family-wise' in v


def test_verdict_below_t_min_not_proven():
    v = ps._verdict(_arm(1.5), t_family=2.5, k=6)
    assert v.startswith('NOT PROVEN')


# ── microstructure maker-only forward arm ─────────────────────────────────────

# ── deflated Sharpe ratio (Bailey & López de Prado) ──────────────────────────

def test_stats_reports_skew_and_kurt():
    s = ps._stats([1.0, -1.0, 1.0, -1.0, 1.0, -1.0])   # symmetric → skew≈0
    assert abs(s['skew']) < 1e-9
    assert 'kurt' in s
    z = ps._stats([])
    assert z['skew'] == 0.0 and z['kurt'] == 3.0       # normal defaults on empty


def test_expected_max_sharpe_rises_with_trials_and_spread():
    few = ps._expected_max_sharpe([0.1, 0.3])
    many = ps._expected_max_sharpe([0.1, 0.3] * 8)      # more trials → higher bar
    assert many > few > 0
    assert ps._expected_max_sharpe([0.2]) == 0.0        # N<2 → no deflation
    assert ps._expected_max_sharpe([0.2, 0.2, 0.2]) == 0.0  # zero variance → 0


def test_deflated_sharpe_monotonic_and_bounded():
    # higher per-trade Sharpe → higher DSR; below the benchmark → < 0.5
    lo = ps._deflated_sharpe(0.05, 100, 0.0, 3.0, sr0=0.10)
    hi = ps._deflated_sharpe(0.40, 100, 0.0, 3.0, sr0=0.10)
    assert 0.0 <= lo < 0.5 < hi <= 1.0


def test_deflated_sharpe_guards():
    assert ps._deflated_sharpe(0.3, 1, 0.0, 3.0, 0.1) == 0.0     # n_eff<2 → 0
    # a degenerate denom (huge skew/kurt) must not raise
    assert 0.0 <= ps._deflated_sharpe(0.3, 50, -50.0, 500.0, 0.1) <= 1.0


def test_microstructure_forward_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    assert ps._microstructure_forward() is None


def test_microstructure_forward_reads_state(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    (tmp_path / 'micro_paper_state.json').write_text(json.dumps({"closed": [
        {"entry_ts": 1718000000, "exit_ts": 1718000060, "pnl": 0.5},
        {"entry_ts": 1718000400, "exit_ts": 1718000460, "pnl": -0.3},
    ]}))
    a = ps._microstructure_forward()
    assert a is not None and a['n'] == 2 and a['executable'] is True
    assert 'maker-only' in a['label']
