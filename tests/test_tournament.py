"""Tests for the tournament core (src/tournament.py) — no network."""

import numpy as np
import pandas as pd
import pytest

from src import tournament as T


def _trend_df(n=400, drift=0.002, seed=1):
    """Synthetic uptrending series with mild noise."""
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, 0.01, n)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    idx = pd.RangeIndex(n)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                         "vol": 1.0}, index=idx)


def test_generate_candidates_count_over_100():
    cands = T.generate_candidates()
    assert len(cands) > 100, f"only {len(cands)} candidates"


def test_candidates_have_families_and_lo_flags():
    cands = T.generate_candidates()
    fams = {c.family for c in cands.values()}
    assert {"trend", "cross", "momentum", "breakout", "meanrev"} <= fams
    # At least some long-only-executable and some long/short
    assert any(c.long_only_ok for c in cands.values())
    assert any(not c.long_only_ok for c in cands.values())


def test_candidate_fns_return_bounded_positions():
    cands = T.generate_candidates()
    df = _trend_df()
    for name, cand in cands.items():
        pos = pd.Series(cand.fn(df), index=df.index)
        assert pos.max() <= 1.0 + 1e-9, name
        assert pos.min() >= -1.0 - 1e-9, name
        if cand.long_only_ok:
            assert pos.min() >= -1e-9, f"{name} long-only went short"


def test_backtest_costs_reduce_return():
    df = _trend_df()
    pos = pd.Series(1.0, index=df.index)  # always long, 1 position change
    net_free, eq_free = T.backtest(df, pos, cost=0.0)
    net_cost, eq_cost = T.backtest(df, pos, cost=0.01)
    assert eq_cost.iloc[-1] < eq_free.iloc[-1]


def test_backtest_is_lookahead_free():
    # A position set only on the last bar must not affect any earlier equity.
    df = _trend_df(n=50)
    pos = pd.Series(0.0, index=df.index)
    pos.iloc[-1] = 1.0
    net, eq = T.backtest(df, pos)
    assert (net.iloc[:-1] == 0).all()  # last decision realized on the (absent) t+1


def test_metrics_keys_and_buyhold_positive_on_uptrend():
    df = _trend_df(drift=0.003)
    net, eq = T.backtest(df, pd.Series(1.0, index=df.index), cost=0.0)
    m = T.metrics(net, eq)
    assert set(m) >= {"final", "ret", "cagr", "sharpe", "mdd", "trades", "t_stat"}
    assert m["final"] > T.START


def test_metrics_short_series_safe():
    s = pd.Series([0.0])
    m = T.metrics(s, pd.Series([T.START]))
    assert m["trades"] == 0 and m["sharpe"] == 0.0


def test_sidak_t_bar_increases_with_k():
    assert T.sidak_t_bar(1) < T.sidak_t_bar(10) < T.sidak_t_bar(100)
    # k=1 two-sided 0.05 ~ 1.96
    assert 1.9 < T.sidak_t_bar(1) < 2.0


def test_evaluate_produces_rows_and_flags():
    cands = T.generate_candidates()
    data = {"A": _trend_df(seed=1), "B": _trend_df(seed=2)}
    rows = T.evaluate(cands, data)
    assert len(rows) == len(cands)
    r0 = rows[0]
    assert set(r0) >= {"name", "family", "long_only_ok", "sharpe", "t_stat",
                       "ret_pct", "mdd_pct", "trades", "robust", "passes_family"}
    # sorted by sharpe desc
    assert rows[0]["sharpe"] >= rows[-1]["sharpe"]


def test_evaluate_trend_strategy_robust_on_uptrend():
    cands = {k: v for k, v in T.generate_candidates().items() if k == "tsmom_50"}
    data = {"A": _trend_df(seed=1, drift=0.003), "B": _trend_df(seed=2, drift=0.003)}
    rows = T.evaluate(cands, data)
    assert rows[0]["robust"] is True


def test_passes_family_requires_robust():
    # A row that isn't robust can never pass the family bar.
    cands = T.generate_candidates()
    data = {"A": _trend_df(seed=1), "B": _trend_df(seed=2)}
    rows = T.evaluate(cands, data)
    for r in rows:
        if r["passes_family"]:
            assert r["robust"]


def test_summarize_counts():
    cands = T.generate_candidates()
    data = {"A": _trend_df(seed=1), "B": _trend_df(seed=2)}
    rows = T.evaluate(cands, data)
    s = T.summarize(rows, len(cands))
    assert s["n_candidates"] == len(cands)
    assert s["n_passes_family"] <= s["n_robust"]
    assert s["n_long_only_executable"] <= s["n_passes_family"]
