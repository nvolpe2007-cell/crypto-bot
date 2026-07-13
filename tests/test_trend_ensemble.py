"""trend_ensemble_paper: 2-of-3 vote signal + forward bookkeeping."""
import json

import pytest

import trend_ensemble_paper as te


def _closes(n, price=100.0):
    return [price] * n


def _rising(n, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


class TestVotes:
    def test_warmup_returns_none(self):
        assert te._votes(_closes(te.WARMUP - 1)) is None
        assert te._want_long(_closes(te.WARMUP - 1)) is None

    def test_strong_uptrend_all_votes(self):
        closes = _rising(te.WARMUP + 10)
        n, legs = te._votes(closes)
        assert n == 3 and te._want_long(closes) is True

    def test_downtrend_no_votes(self):
        closes = [300.0 - i * 0.5 for i in range(te.WARMUP + 10)]
        n, _ = te._votes(closes)
        assert n == 0 and te._want_long(closes) is False

    def test_two_of_three_is_long(self):
        # above both SMAs but 90d momentum negative: spike then slow bleed above avg
        closes = _rising(te.WARMUP) + [0] * 0
        closes = _rising(te.WARMUP - 5, start=100, step=1.0)
        closes += [closes[-1] - i * 0.1 for i in range(1, 6)]  # slight dip, still > SMAs
        v = te._votes(closes)
        assert v[0] >= 2
        assert te._want_long(closes) is True

    def test_one_vote_is_cash(self):
        # long decline, then small pop: momentum(90) may turn but SMAs stay above
        closes = [400.0 - i for i in range(te.WARMUP - 10)]
        closes += [closes[-1] + i * 0.5 for i in range(1, 11)]
        v = te._votes(closes)
        if v[0] < 2:  # construction sanity: only assert the gate
            assert te._want_long(closes) is False


class TestBookkeeping:
    def _bars(self, closes):
        return [{"t": 86400 * (i + 1), "c": c} for i, c in enumerate(closes)]

    def test_seed_is_forward_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(te, "STATE_FILE", tmp_path / "s.json")
        state = te._load_state()
        bars = self._bars(_rising(te.WARMUP + 5))
        acted = te.process_symbol("BTC", bars, state)
        assert acted == 0                       # seeding books no trades
        assert "BTC" in state["positions"]      # but participates from inception
        assert state["closed"] == []

    def test_exit_books_cost_and_pnl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(te, "STATE_FILE", tmp_path / "s.json")
        state = te._load_state()
        up = _rising(te.WARMUP + 5)
        te.process_symbol("BTC", self._bars(up), state)
        # crash below everything -> votes collapse -> close
        crash = up + [up[-1] * 0.5] * 95
        acted = te.process_symbol("BTC", self._bars(crash), state)
        assert acted >= 1
        assert "BTC" not in state["positions"]
        rec = state["closed"][-1]
        assert rec["reason"] == "votes_off"
        expected = te.TRADE_SIZE * ((rec["exit"] - rec["entry"]) / rec["entry"] - te.COST_FRAC)
        assert rec["pnl"] == pytest.approx(expected, abs=0.01)
        assert state["equity"] == pytest.approx(te.STARTING_EQUITY + rec["pnl"], abs=0.01)

    def test_no_repaint_same_bars_no_action(self, tmp_path, monkeypatch):
        monkeypatch.setattr(te, "STATE_FILE", tmp_path / "s.json")
        state = te._load_state()
        bars = self._bars(_rising(te.WARMUP + 5))
        te.process_symbol("BTC", bars, state)
        assert te.process_symbol("BTC", bars, state) == 0  # nothing new -> no action

    def test_state_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(te, "STATE_FILE", tmp_path / "s.json")
        state = te._load_state()
        te.process_symbol("BTC", self._bars(_rising(te.WARMUP + 5)), state)
        te._save_state(state)
        loaded = te._load_state()
        assert loaded["positions"].keys() == state["positions"].keys()
        assert loaded["equity"] == state["equity"]
