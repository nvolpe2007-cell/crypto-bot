"""
Walk-forward + parameter-robustness for the ORB strategy — the antidote to the
overfit trap (sweeping params until something backtests well).

walk_forward(): repeatedly SELECT the best ORB params on a TRAIN window, then trade
them UNTOUCHED on the next, unseen TEST window. The pooled TEST trades are the
honest out-of-sample record — judge THAT with the proof bar, not the in-sample fit.

grid_robustness(): run every param set over the whole period so you can see whether
the edge is a broad PLATEAU (robust) or a single knife-edge cell (overfit), and get
the expected-max-Sharpe across trials to DEFLATE the best cell's Sharpe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd

from .backtest import net_returns, run_backtest
from .metrics import deflated_sharpe, expected_max_sharpe, summary
from .strategy import ORBConfig, Trade


def default_grid(cost_bps_per_side: float = 2.0) -> List[ORBConfig]:
    """A small, sane grid. Keep it SMALL on purpose — every extra cell raises the
    multiple-testing bar (see grid_robustness' deflated Sharpe)."""
    grid = []
    for orm in (5, 15, 30):
        for tr in (1.5, 2.0, 3.0):
            for direction in ("long", "both"):
                grid.append(ORBConfig(or_minutes=orm, target_r=tr, direction=direction,
                                      cost_bps_per_side=cost_bps_per_side))
    return grid


def _slice(df: pd.DataFrame, dates: set) -> pd.DataFrame:
    return df[pd.Index(df.index.date).isin(dates)]


def evaluate(df: pd.DataFrame, cfg: ORBConfig, symbol: str = "?") -> dict:
    return summary(net_returns(run_backtest(df, cfg, symbol=symbol)))


def select_best(df: pd.DataFrame, grid: List[ORBConfig], min_trades: int = 10,
                symbol: str = "?") -> Tuple[Optional[ORBConfig], Optional[dict]]:
    """Pick the in-sample cfg with the highest expectancy among those with enough
    trades AND positive expectancy. None if nothing qualifies (then we sit out —
    refusing to trade an edge we couldn't even see in-sample is the honest move)."""
    best_cfg, best_sum = None, None
    for cfg in grid:
        s = evaluate(df, cfg, symbol=symbol)
        if s["n"] < min_trades or s["expectancy"] <= 0:
            continue
        if best_sum is None or s["expectancy"] > best_sum["expectancy"]:
            best_cfg, best_sum = cfg, s
    return best_cfg, best_sum


@dataclass
class Fold:
    test_start: str
    test_end: str
    chosen: Optional[ORBConfig]
    oos_trades: List[Trade] = field(default_factory=list)

    def cfg_label(self) -> str:
        c = self.chosen
        return "—(sat out)" if c is None else f"OR{c.or_minutes}/R{c.target_r}/{c.direction}"


def walk_forward(df: pd.DataFrame, grid: Optional[List[ORBConfig]] = None,
                 train_days: int = 40, test_days: int = 10, anchored: bool = False,
                 min_trades: int = 10, symbol: str = "?") -> Tuple[List[Fold], List[Trade]]:
    """Rolling (or anchored) walk-forward. Returns (folds, pooled_oos_trades).
    The pooled OOS trades are what you judge — they were chosen WITHOUT seeing them."""
    grid = grid or default_grid()
    dates = sorted(set(df.index.date))
    folds: List[Fold] = []
    oos_all: List[Trade] = []
    i = train_days
    while i < len(dates):
        train_dates = set(dates[:i]) if anchored else set(dates[i - train_days:i])
        test_dates = set(dates[i:i + test_days])
        if not test_dates:
            break
        chosen, _ = select_best(_slice(df, train_dates), grid, min_trades, symbol)
        oos = run_backtest(_slice(df, test_dates), chosen, symbol=symbol) if chosen else []
        folds.append(Fold(test_start=str(min(test_dates)), test_end=str(max(test_dates)),
                          chosen=chosen, oos_trades=oos))
        oos_all.extend(oos)
        i += test_days
    return folds, oos_all


def grid_robustness(df: pd.DataFrame, grid: Optional[List[ORBConfig]] = None,
                    symbol: str = "?") -> dict:
    """Full-period result for every cfg → is the edge a plateau or a knife-edge?
    Also returns sr0 = expected-max-Sharpe across the grid and the deflated Sharpe
    of the BEST cell (so 'best of N params' is judged against the selection bias)."""
    grid = grid or default_grid()
    rows = []
    for cfg in grid:
        s = evaluate(df, cfg, symbol=symbol)
        rows.append((cfg, s))
    sharpes = [s["sharpe"] for _, s in rows if s["n"] >= 2]
    sr0 = expected_max_sharpe(sharpes)
    best_cfg, best_sum = max(rows, key=lambda cs: cs[1]["expectancy"])
    dsr = deflated_sharpe(best_sum["sharpe"], best_sum["n"], best_sum["skew"],
                          best_sum["kurt"], sr0) if best_sum["n"] >= 2 else 0.0
    exps = sorted(s["expectancy"] for _, s in rows)
    return {
        "n_params": len(grid),
        "best_cfg": best_cfg, "best_summary": best_sum,
        "best_deflated_sharpe": dsr, "sr0_expected_max_sharpe": sr0,
        "expectancy_best": exps[-1], "expectancy_median": exps[len(exps) // 2],
        "expectancy_worst": exps[0],
        "share_positive": sum(1 for e in exps if e > 0) / len(exps),
        "rows": rows,
    }
