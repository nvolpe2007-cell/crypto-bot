"""Walk-forward + robustness + deflated-Sharpe tests."""
from stockbot.data import synthetic_intraday
from stockbot.metrics import deflated_sharpe, expected_max_sharpe, summary
from stockbot.strategy import ORBConfig
from stockbot.walkforward import (default_grid, grid_robustness, select_best,
                                  walk_forward)

DF = synthetic_intraday(days=90, seed=11)


def test_default_grid_is_small_and_typed():
    g = default_grid()
    assert 6 <= len(g) <= 40 and all(isinstance(c, ORBConfig) for c in g)


def test_select_best_sits_out_when_min_trades_unreachable():
    cfg, s = select_best(DF, default_grid(), min_trades=10_000)
    assert cfg is None and s is None


def test_select_best_returns_positive_expectancy_cfg_or_none():
    cfg, s = select_best(DF, default_grid(), min_trades=3)
    if cfg is not None:
        assert s["expectancy"] > 0 and s["n"] >= 3


def test_walk_forward_oos_windows_are_sequential_and_nonoverlapping():
    folds, oos = walk_forward(DF, train_days=40, test_days=10, min_trades=3)
    assert len(folds) >= 1
    # test windows advance in time and don't overlap
    ends = [f.test_start for f in folds]
    assert ends == sorted(ends)
    for a, b in zip(folds, folds[1:]):
        assert a.test_end < b.test_start
    # pooled OOS trades only come from the fold test windows
    assert len(oos) == sum(len(f.oos_trades) for f in folds)


def test_walk_forward_chosen_cfg_is_from_grid_or_none():
    grid = default_grid()
    keys = {(c.or_minutes, c.target_r, c.direction) for c in grid}
    folds, _ = walk_forward(DF, grid, train_days=40, test_days=10, min_trades=3)
    for f in folds:
        if f.chosen is not None:
            assert (f.chosen.or_minutes, f.chosen.target_r, f.chosen.direction) in keys


def test_grid_robustness_shape():
    r = grid_robustness(DF)
    assert r["n_params"] == len(default_grid())
    assert 0.0 <= r["share_positive"] <= 1.0
    assert 0.0 <= r["best_deflated_sharpe"] <= 1.0
    assert r["expectancy_best"] >= r["expectancy_median"] >= r["expectancy_worst"]


# ── deflated Sharpe machinery (mirrors the crypto proof bar) ──────────────────

def test_expected_max_sharpe_rises_with_trials():
    assert expected_max_sharpe([0.1, 0.3] * 8) > expected_max_sharpe([0.1, 0.3]) > 0
    assert expected_max_sharpe([0.2]) == 0.0
    assert expected_max_sharpe([0.2, 0.2, 0.2]) == 0.0


def test_deflated_sharpe_monotonic_bounded():
    lo = deflated_sharpe(0.05, 100, 0.0, 3.0, sr0=0.10)
    hi = deflated_sharpe(0.40, 100, 0.0, 3.0, sr0=0.10)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    assert deflated_sharpe(0.3, 1, 0.0, 3.0, 0.1) == 0.0   # n<2 guard


def test_summary_has_skew_kurt():
    s = summary([0.01, -0.01, 0.02, -0.02])
    assert "skew" in s and "kurt" in s
