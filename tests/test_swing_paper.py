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


def _proc(bars, state, strat, dlog, live_price=None):
    """Process one (BTC, 4h) slot, then commit any returned entry candidate
    (mirrors main()'s collect→commit). Returns # bars processed, so the existing
    assertions on the count and on the resulting position still hold."""
    n, cand = swing_paper.process_symbol(KEY, BASE, TF, bars, state, strat, dlog,
                                         live_price=live_price)
    if cand is not None:
        swing_paper._commit_entry(cand, state, dlog, notify=None)
    return n


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
    # measure-first annotation tags are stamped on the entry (never gated)
    assert isinstance(pos["td_signal"], str)
    assert "entry_window" in pos and "conviction" in pos


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


def test_fill_realism_uses_live_price(tmp_path):
    """On the latest bar, the entry fills at the live market price (cron acts
    after the close), not the bar close — stop/target shift to preserve R:R."""
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    sig_close = bars[-1]["c"]
    live = sig_close + 1.0
    n = _proc(bars, state, strat, dlog, live_price=live)
    assert n == 1
    pos = state["positions"][KEY]
    assert pos["entry"] == live                      # filled at market
    assert pos["target"] > pos["entry"] > pos["stop"]  # R:R intact


def test_event_blackout_vetoes_entry(tmp_path, monkeypatch):
    """A would-be entry inside an event blackout window is vetoed (no position)."""
    bars = _entry_bars()
    state = _fresh_state(bars)
    strat, dlog = SwingStrategy(), _dlog(tmp_path)
    monkeypatch.setattr(swing_paper, "blackout_reason", lambda *a, **k: "FOMC in 2.0h")
    n = _proc(bars, state, strat, dlog)
    assert n == 1                                    # bar was processed
    assert state["positions"] == {}                  # but no entry opened


def test_vp_context_annotates_with_volume():
    ctx = swing_paper._vp_context([{"l": 99, "h": 101, "c": 100, "v": 100.0}
                                   for _ in range(30)], 100.0)
    assert ctx["vp_zone"] != "n/a"
    assert "vp_poc" in ctx and "vp_dist_poc_pct" in ctx


def test_vp_context_na_without_volume():
    ctx = swing_paper._vp_context([{"l": 99, "h": 101, "c": 100} for _ in range(30)],
                                  100.0)
    assert ctx["vp_zone"] == "n/a"


# ── cadence governors: window budget, position cap, conviction ranking ────────

from datetime import datetime, timezone


def _cand(base, conviction, hour=12, day="2026-06-12"):
    """Minimal entry candidate (what process_symbol returns / _commit_entry eats)."""
    entry_dt = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
    window = swing_paper.window_of_hour(hour)
    pos = {"symbol": base, "tf": 240, "entry": 100.0, "stop": 98.0, "target": 103.0,
           "entry_ts": "1", "size_usd": 62.5, "rr": 1.5, "entry_day": day,
           "entry_window": window, "conviction": conviction, "vp_zone": "n/a"}
    return {"key": f"{base}@240", "base": base, "tf": 240, "entry_dt": entry_dt,
            "conviction": conviction, "position": pos, "fill": 100.0,
            "signal_close": 100.0, "delta": 0.0, "stop": 98.0, "target": 103.0,
            "rr": 1.5, "reason": "test", "vp_ctx": {"vp_zone": "n/a"}}


def test_window_budget_caps_new_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(swing_paper, "MAX_TRADES_DAY", 2)
    monkeypatch.setattr(swing_paper, "MAX_OPEN_POSITIONS", 10)
    state = {"positions": {}, "closed": []}
    dlog = _dlog(tmp_path)
    cands = [_cand("BTC", 0.5), _cand("ETH", 0.4), _cand("SOL", 0.3)]   # 3 day setups
    opened = sum(swing_paper._commit_entry(c, state, dlog) for c in cands)
    assert opened == 2                               # capped at the day budget
    assert len(state["positions"]) == 2


def test_window_budget_counts_already_closed_today(tmp_path, monkeypatch):
    monkeypatch.setattr(swing_paper, "MAX_TRADES_DAY", 1)
    monkeypatch.setattr(swing_paper, "MAX_OPEN_POSITIONS", 10)
    # one trade already CLOSED today in the day window → budget is spent
    state = {"positions": {}, "closed": [
        {"entry_day": "2026-06-12", "entry_window": "day"}]}
    opened = swing_paper._commit_entry(_cand("ETH", 0.9), state, _dlog(tmp_path))
    assert opened is False
    assert state["positions"] == {}


def test_day_and_night_budgets_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(swing_paper, "MAX_TRADES_DAY", 1)
    monkeypatch.setattr(swing_paper, "MAX_TRADES_NIGHT", 1)
    monkeypatch.setattr(swing_paper, "MAX_OPEN_POSITIONS", 10)
    state = {"positions": {}, "closed": []}
    dlog = _dlog(tmp_path)
    opened = sum(swing_paper._commit_entry(c, state, dlog)
                 for c in [_cand("BTC", 0.5, hour=12),    # day
                           _cand("ETH", 0.4, hour=3)])    # night
    assert opened == 2                               # separate windows, both fit


def test_concurrent_position_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(swing_paper, "MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(swing_paper, "MAX_TRADES_DAY", 10)
    state = {"positions": {}, "closed": []}
    dlog = _dlog(tmp_path)
    opened = sum(swing_paper._commit_entry(c, state, dlog)
                 for c in [_cand("BTC", 0.5), _cand("ETH", 0.4)])
    assert opened == 1


def test_conviction_ranking_keeps_the_best(tmp_path, monkeypatch):
    """When the window budget binds, the higher-conviction setup wins (main()
    sorts desc before committing)."""
    monkeypatch.setattr(swing_paper, "MAX_TRADES_DAY", 1)
    monkeypatch.setattr(swing_paper, "MAX_OPEN_POSITIONS", 10)
    state = {"positions": {}, "closed": []}
    dlog = _dlog(tmp_path)
    cands = [_cand("BTC", 0.2), _cand("ETH", 0.9)]
    cands.sort(key=lambda c: c["conviction"], reverse=True)
    for c in cands:
        swing_paper._commit_entry(c, state, dlog)
    assert list(state["positions"]) == ["ETH@240"]   # the stronger trend opened


def test_conviction_rewards_stronger_trend():
    weak = type("D", (), {"indicators": {"roc": 0.02, "close": 100.0, "ema_slow": 99.0}})()
    strong = type("D", (), {"indicators": {"roc": 0.08, "close": 100.0, "ema_slow": 95.0}})()
    assert swing_paper._conviction(strong) > swing_paper._conviction(weak)


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
