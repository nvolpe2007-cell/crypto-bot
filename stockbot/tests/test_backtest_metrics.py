"""Backtest runner + proof-metrics tests."""
import math

from stockbot.backtest import run_backtest, net_returns
from stockbot.data import synthetic_intraday
from stockbot.metrics import summary, verdict, N_MIN
from stockbot.strategy import ORBConfig


def test_backtest_runs_on_synthetic_and_is_intraday():
    df = synthetic_intraday(days=40, seed=3)
    cfg = ORBConfig(direction="both")
    trades = run_backtest(df, cfg, symbol="SYNTH")
    assert len(trades) > 0
    # every trade opens and closes the SAME session (intraday — no overnight)
    for t in trades:
        assert t.entry_time[:10] == t.exit_time[:10] == t.date
        assert t.reason in ("stop", "target", "eod")
    # at most one trade per day
    assert len(trades) == len({t.date for t in trades})


def test_cost_is_charged_each_trade():
    df = synthetic_intraday(days=30, seed=5)
    cfg = ORBConfig(direction="both", cost_bps_per_side=2.0)
    for t in run_backtest(df, cfg):
        assert abs(t.net_ret - (t.gross_ret - 0.0004)) < 1e-9   # 2bps/side round trip


def test_summary_and_verdict_thresholds():
    # < N_MIN trades → NOT PROVEN regardless of how good
    s = summary([0.01] * (N_MIN - 1))
    assert "NOT PROVEN" in verdict(s)
    # negative expectancy → FAILED
    s = summary([-0.01] * 50)
    assert verdict(s).startswith("FAILED")
    # strong positive with nonzero variance → PROVEN (in-sample). (Identical
    # values would give zero variance → t=0, which is correctly NOT proof.)
    s = summary([0.012, 0.008] * 25)
    assert "PROVEN (in-sample)" in verdict(s)
    assert s["t_stat"] > 2 and s["win_rate"] == 1.0


def test_summary_empty():
    s = summary([])
    assert s["n"] == 0 and s["expectancy"] == 0.0


def test_profit_factor_and_drawdown():
    s = summary([0.02, -0.01, 0.02, -0.01])   # gross win .04 / gross loss .02 = 2.0
    assert abs(s["profit_factor"] - 2.0) < 1e-9
    assert s["max_dd"] <= 0.0
