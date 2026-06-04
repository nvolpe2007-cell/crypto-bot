"""
Tests for swing_paper.py — the forward paper runner's bar-stepping logic.

Covers: forward-only baseline (no replay on first run), opening on an entry
bar, closing on target, and idempotency (already-seen bars don't re-trade).
"""
from pathlib import Path

import swing_paper
from src.swing_strategy import SwingStrategy
from src.decision_log import DecisionLog


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


def _fresh_state(bars, base="BTC"):
    # baseline set to the SECOND-TO-LAST bar, so the final (entry) bar is "new"
    return {"positions": {}, "closed": [], "started_at": "x",
            "last_bar_t": {base: bars[-2]["t"]}}


def _dlog(tmp_path):
    return DecisionLog(path=tmp_path / "dec.jsonl")


def test_first_run_sets_baseline_no_trade(tmp_path):
    bars = _entry_bars()
    state = {"positions": {}, "closed": [], "last_bar_t": {}, "started_at": "x"}
    n = swing_paper.process_symbol("BTC", bars, state, SwingStrategy(), _dlog(tmp_path))
    assert n == 0                                  # forward-only: no replay
    assert state["last_bar_t"]["BTC"] == bars[-1]["t"]
    assert state["positions"] == {}


def test_opens_on_entry_bar(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    n = swing_paper.process_symbol("BTC", bars, state, SwingStrategy(), _dlog(tmp_path))
    assert n == 1
    assert "BTC" in state["positions"]
    pos = state["positions"]["BTC"]
    assert pos["target"] > pos["entry"] > pos["stop"]


def test_closes_on_target(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    swing_paper.process_symbol("BTC", bars, state, strat, dlog)
    pos = state["positions"]["BTC"]
    # next closed bar spikes through the target → win
    nxt = {"t": bars[-1]["t"] + 14400, "o": pos["entry"],
           "h": pos["target"] + 5, "l": pos["entry"] - 0.5, "c": pos["target"] + 3}
    swing_paper.process_symbol("BTC", bars + [nxt], state, strat, dlog)
    assert state["positions"] == {}
    assert len(state["closed"]) == 1
    assert state["closed"][0]["won"] is True
    assert state["closed"][0]["reason"] == "target"


def test_idempotent_no_new_bars(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    swing_paper.process_symbol("BTC", bars, state, strat, dlog)
    open_after_first = dict(state["positions"])
    # same bars again → nothing new to process
    n = swing_paper.process_symbol("BTC", bars, state, strat, dlog)
    assert n == 0
    assert state["positions"] == open_after_first


def test_stop_loss_exit(tmp_path):
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    swing_paper.process_symbol("BTC", bars, state, strat, dlog)
    pos = state["positions"]["BTC"]
    nxt = {"t": bars[-1]["t"] + 14400, "o": pos["entry"],
           "h": pos["entry"] + 0.5, "l": pos["stop"] - 5, "c": pos["stop"] - 4}
    swing_paper.process_symbol("BTC", bars + [nxt], state, strat, dlog)
    assert len(state["closed"]) == 1
    assert state["closed"][0]["reason"] == "stop"
    assert state["closed"][0]["won"] is False
