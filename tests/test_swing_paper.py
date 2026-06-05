"""
Tests for swing_paper.py — the forward paper runner's bar-stepping logic.

Covers: forward-only baseline (no replay on first run), opening on an entry
bar, closing on target, and idempotency (already-seen bars don't re-trade).
"""
from pathlib import Path

import pytest

import swing_paper
from src.swing_strategy import SwingStrategy
from src.decision_log import DecisionLog


# The runner namespaces state per (symbol, timeframe) as "BASE@INTERVAL".
KEY, BASE, TF = "BTC@240", "BTC", 240


def _proc(bars, state, strat, dlog):
    """Process one (BTC, 4h) slot with the namespaced-key signature."""
    return swing_paper.process_symbol(KEY, BASE, TF, bars, state, strat, dlog)


def _bars_from_closes(closes, hl_spread=1.0):
    return [{"t": i * 14400, "o": closes[i - 1] if i else c,
             "h": c + hl_spread, "l": c - hl_spread, "c": c}
            for i, c in enumerate(closes)]


def _entry_bars():
    """Closed bars whose final bar triggers an ENTER (uptrend pullback resume)."""
    closes = [100.0 + i * 1.5 for i in range(60)]
    base = closes[-1]
    closes += [base - 2 * j for j in range(1, 9)]
    closes += [closes[-1] + 6]
    return _bars_from_closes(closes)


def _fresh_state(bars, key=KEY):
    # baseline set to the SECOND-TO-LAST bar, so the final (entry) bar is "new"
    return {"positions": {}, "closed": [], "started_at": "x",
            "last_bar_t": {key: bars[-2]["t"]}}


def _dlog(tmp_path):
    return DecisionLog(path=tmp_path / "dec.jsonl")


def test_first_run_sets_baseline_no_trade(tmp_path):
    bars = _entry_bars()
    state = {"positions": {}, "closed": [], "last_bar_t": {}, "started_at": "x"}
    n = _proc(bars, state, SwingStrategy(), _dlog(tmp_path))
    assert n == 0                                  # forward-only: no replay
    assert state["last_bar_t"][KEY] == bars[-1]["t"]
    assert state["positions"] == {}


def test_opens_on_entry_bar(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    n = _proc(bars, state, SwingStrategy(), _dlog(tmp_path))
    assert n == 1
    assert KEY in state["positions"]
    pos = state["positions"][KEY]
    assert pos["target"] > pos["entry"] > pos["stop"]


def test_closes_on_target(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    _proc(bars, state, strat, dlog)
    pos = state["positions"][KEY]
    # next closed bar spikes through the target → win
    nxt = {"t": bars[-1]["t"] + 14400, "o": pos["entry"],
           "h": pos["target"] + 5, "l": pos["entry"] - 0.5, "c": pos["target"] + 3}
    _proc(bars + [nxt], state, strat, dlog)
    assert state["positions"] == {}
    assert len(state["closed"]) == 1
    assert state["closed"][0]["won"] is True
    assert state["closed"][0]["reason"] == "target"


def test_idempotent_no_new_bars(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    _proc(bars, state, strat, dlog)
    open_after_first = dict(state["positions"])
    # same bars again → nothing new to process
    n = _proc(bars, state, strat, dlog)
    assert n == 0
    assert state["positions"] == open_after_first


def test_equity_tracks_closed_pnl(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    state["equity"] = state["starting_equity"] = 500.0
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    _proc(bars, state, strat, dlog)
    pos = state["positions"][KEY]
    nxt = {"t": bars[-1]["t"] + 14400, "o": pos["entry"],
           "h": pos["target"] + 5, "l": pos["entry"] - 0.5, "c": pos["target"] + 3}
    _proc(bars + [nxt], state, strat, dlog)
    net = state["closed"][0]["pnl"]
    assert state["equity"] == pytest.approx(500.0 + net)
    assert state["closed"][0]["equity_after"] == pytest.approx(500.0 + net, abs=0.01)


def test_stop_loss_exit(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    _proc(bars, state, strat, dlog)
    pos = state["positions"][KEY]
    nxt = {"t": bars[-1]["t"] + 14400, "o": pos["entry"],
           "h": pos["entry"] + 0.5, "l": pos["stop"] - 5, "c": pos["stop"] - 4}
    _proc(bars + [nxt], state, strat, dlog)
    assert len(state["closed"]) == 1
    assert state["closed"][0]["reason"] == "stop"
    assert state["closed"][0]["won"] is False


def test_gap_down_through_stop_fills_at_open(tmp_path):
    """When a bar GAPS open below the stop, the (market) stop fills at the worse
    open price, not at the stop — otherwise gap-downs overstate P&L."""
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    _proc(bars, state, strat, dlog)
    pos = state["positions"][KEY]
    gap_open = pos["stop"] - 3.0              # opens BELOW the stop (gap down)
    nxt = {"t": bars[-1]["t"] + 14400, "o": gap_open,
           "h": gap_open + 0.2, "l": gap_open - 1.0, "c": gap_open - 0.5}
    _proc(bars + [nxt], state, strat, dlog)
    rec = state["closed"][0]
    assert rec["reason"] == "stop"
    # filled at the gapped-open, strictly worse than the stop price
    assert rec["exit"] == gap_open < pos["stop"]
