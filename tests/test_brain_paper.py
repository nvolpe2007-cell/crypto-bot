"""Tests for brain_paper.py — the parts not already covered by test_trade_brain.py
(apply_decision/build_snapshot): funding-cost accounting and the full P&L record
that feeds proof_scorecard._brain_forward, the BRAIN_DRY_RUN self-test heuristic,
and the daily-reasoning Telegram notify."""

import importlib

import pytest

import brain_paper as bp
from src.trade_brain import BrainResult, CoinDecision


@pytest.fixture(autouse=True)
def _reset():
    """Reload module so per-test monkeypatching of globals is isolated."""
    importlib.reload(bp)
    yield
    importlib.reload(bp)


def _state(**over):
    base = {"positions": {}, "closed": [], "equity": 1000.0, "starting_equity": 1000.0}
    base.update(over)
    return base


# ── _funding_cost ────────────────────────────────────────────────────────────

def test_funding_cost_one_year_at_full_apy(monkeypatch):
    monkeypatch.setattr(bp, "FUNDING_APY", 0.10)
    entry_t = 1_000_000
    exit_t = entry_t + 365 * 86_400
    cost = bp._funding_cost(1000.0, str(entry_t), str(exit_t))
    assert cost == pytest.approx(100.0, rel=1e-3)


def test_funding_cost_zero_duration_is_zero(monkeypatch):
    monkeypatch.setattr(bp, "FUNDING_APY", 0.10)
    assert bp._funding_cost(1000.0, "1000", "1000") == 0.0


def test_funding_cost_invalid_timestamps_is_zero():
    assert bp._funding_cost(1000.0, "not-a-number", "1000") == 0.0
    assert bp._funding_cost(1000.0, None, "1000") == 0.0


def test_funding_cost_negative_duration_clamped_to_zero(monkeypatch):
    monkeypatch.setattr(bp, "FUNDING_APY", 0.10)
    cost = bp._funding_cost(1000.0, "2000", "1000")  # exit before entry
    assert cost == 0.0


# ── _close: full record consumed by proof_scorecard._brain_forward ───────────

def test_close_long_records_full_pnl_breakdown(monkeypatch):
    monkeypatch.setattr(bp, "COST_FRAC", 0.0015)
    monkeypatch.setattr(bp, "FUNDING_APY", 0.10)
    state = _state(positions={"BTC": {"symbol": "BTC", "side": 1, "entry": 100.0,
                                        "entry_ts": "1000000", "size_usd": 1000.0}})
    exit_ts = str(1_000_000 + 365 * 86_400)  # held exactly one year

    rec = bp._close(state, "BTC", 110.0, exit_ts, "brain:flat")

    # gross +10% on $1000 = $100; round-trip cost 0.15% of $1000 = $1.50;
    # funding @10% APY for one year = $100
    assert rec["pnl"] == pytest.approx(100.0 - 1.5 - 100.0)
    assert rec["pnl_pct"] == pytest.approx(10.0)
    assert rec["funding_cost"] == pytest.approx(100.0, rel=1e-3)
    assert rec["entry_ts"] == "1000000" and rec["exit_ts"] == exit_ts
    assert rec["reason"] == "brain:flat"
    assert "BTC" not in state["positions"]
    assert state["closed"] == [rec]
    assert state["equity"] == pytest.approx(1000.0 + rec["pnl"])
    assert rec["equity_after"] == pytest.approx(state["equity"])


def test_close_short_profits_on_price_drop(monkeypatch):
    monkeypatch.setattr(bp, "COST_FRAC", 0.0)
    monkeypatch.setattr(bp, "FUNDING_APY", 0.0)
    state = _state(positions={"ETH": {"symbol": "ETH", "side": -1, "entry": 100.0,
                                        "entry_ts": "1000", "size_usd": 500.0}})
    rec = bp._close(state, "ETH", 90.0, "2000", "brain:long")
    assert rec["side"] == -1
    assert rec["pnl"] == pytest.approx(50.0)   # short +10% on $500, no cost/funding


# ── _dry_run_result: BRAIN_DRY_RUN=1 self-test heuristic ──────────────────────

def test_dry_run_long_when_above_sma_and_positive_momentum():
    snap = {"BTC": {"vs_sma50_pct": 5.0, "ret_20d_pct": 3.0}}
    result = bp._dry_run_result(snap)
    assert result.model == "dry-run-heuristic"
    dec = result.decisions["BTC"]
    assert dec.action == "long"
    assert dec.conviction == 6 and dec.size_mult == 1.0
    assert "above SMA50" in dec.key_signal
    assert dec.reasoning.startswith("[SELF-TEST heuristic, not the AI]")


def test_dry_run_short_when_below_sma_and_negative_momentum():
    snap = {"BTC": {"vs_sma50_pct": -5.0, "ret_20d_pct": -3.0}}
    dec = bp._dry_run_result(snap).decisions["BTC"]
    assert dec.action == "short"
    assert "below SMA50" in dec.key_signal


def test_dry_run_flat_when_signals_disagree():
    snap = {"BTC": {"vs_sma50_pct": 5.0, "ret_20d_pct": -3.0}}
    dec = bp._dry_run_result(snap).decisions["BTC"]
    assert dec.action == "flat"
    assert "mixed" in dec.key_signal


def test_dry_run_flat_when_warming_up():
    snap = {"BTC": {"vs_sma50_pct": None, "ret_20d_pct": None}}
    dec = bp._dry_run_result(snap).decisions["BTC"]
    assert dec.action == "flat"
    assert dec.key_signal == "warming up"


def test_dry_run_covers_every_coin_in_snapshot():
    snap = {"BTC": {"vs_sma50_pct": 1, "ret_20d_pct": 1},
            "ETH": {"vs_sma50_pct": -1, "ret_20d_pct": -1},
            "SOL": {"vs_sma50_pct": None, "ret_20d_pct": None}}
    decs = bp._dry_run_result(snap).decisions
    assert set(decs) == {"BTC", "ETH", "SOL"}


# ── _notify: daily reasoning post (added so hold days post too) ──────────────

class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_message(self, msg, parse_mode="HTML"):
        self.sent.append(msg)
        return True


def _patch_notifier(monkeypatch):
    fake = _FakeNotifier()
    import src.notifications as notif
    monkeypatch.setattr(notif, "create_notifier_from_env", lambda: fake)
    return fake


def test_notify_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("BRAIN_NOTIFY", "0")
    fake = _patch_notifier(monkeypatch)
    result = BrainResult(decisions={"BTC": CoinDecision("BTC", "long")})
    bp._notify(_state(), result, {}, acted=1)
    assert fake.sent == []


def test_notify_skipped_on_hold_day_with_no_decisions(monkeypatch):
    fake = _patch_notifier(monkeypatch)
    bp._notify(_state(), BrainResult(decisions={}), {}, acted=0)
    assert fake.sent == []


def test_notify_fires_on_hold_day_with_reasoning(monkeypatch):
    """Latest behavior: post the daily call + reasoning even when acted=0."""
    fake = _patch_notifier(monkeypatch)
    result = BrainResult(decisions={
        "BTC": CoinDecision("BTC", action="flat", conviction=4, reasoning="chop, staying flat")
    })
    bp._notify(_state(), result, {"BTC": 100.0}, acted=0)
    assert len(fake.sent) == 1
    assert "BTC" in fake.sent[0] and "FLAT" in fake.sent[0]
    assert "chop, staying flat" in fake.sent[0]


def test_notify_force_overrides_gating(monkeypatch):
    monkeypatch.setenv("BRAIN_NOTIFY_FORCE", "1")
    fake = _patch_notifier(monkeypatch)
    bp._notify(_state(), BrainResult(decisions={}), {}, acted=0)
    assert len(fake.sent) == 1


def test_notify_shows_open_position_with_unrealized_pnl(monkeypatch):
    fake = _patch_notifier(monkeypatch)
    state = _state(positions={"BTC": {"symbol": "BTC", "side": 1, "entry": 100.0,
                                        "entry_ts": "1", "size_usd": 500.0}})
    result = BrainResult(decisions={"BTC": CoinDecision("BTC", action="long", reasoning="hold")})
    bp._notify(state, result, {"BTC": 110.0}, acted=0)
    msg = fake.sent[0]
    assert "BTC" in msg and "LONG" in msg and "+10.0%" in msg


def test_notify_all_flat_label_when_no_positions(monkeypatch):
    fake = _patch_notifier(monkeypatch)
    result = BrainResult(decisions={"BTC": CoinDecision("BTC", action="flat", reasoning="flat")})
    bp._notify(_state(), result, {}, acted=0)
    assert "(all flat)" in fake.sent[0]


def test_notify_title_reflects_dry_run_mode(monkeypatch):
    fake = _patch_notifier(monkeypatch)
    monkeypatch.setattr(bp, "DRY_RUN", True)
    result = BrainResult(decisions={"BTC": CoinDecision("BTC", action="flat", reasoning="x")})
    bp._notify(_state(), result, {}, acted=0)
    assert "SELF-TEST" in fake.sent[0]
