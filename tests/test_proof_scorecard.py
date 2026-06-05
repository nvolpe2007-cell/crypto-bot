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
