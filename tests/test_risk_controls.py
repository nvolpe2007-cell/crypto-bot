"""Risk controls: master kill switch (src/kill_switch.py) + per-arm funding loss cap."""

from datetime import datetime, timezone

import pytest

import src.kill_switch as ks
import arbitrage.funding_arb_paper as fap
from arbitrage.funding_arb_paper import FundingArbPaperSim


# ── kill switch module ───────────────────────────────────────────────────────

@pytest.fixture
def kill_isolated(tmp_path, monkeypatch):
    """Isolate the flag file and clear the env trigger so tests don't collide
    with any real data/KILL_SWITCH or ambient env."""
    monkeypatch.setattr(ks, "KILL_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.delenv("BOT_KILL_SWITCH", raising=False)
    return ks


def test_kill_default_live(kill_isolated):
    assert kill_isolated.is_killed() is False
    assert kill_isolated.reason() == "live"


def test_kill_env_trigger_variants(kill_isolated, monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("BOT_KILL_SWITCH", v)
        assert kill_isolated.is_killed() is True, v
    monkeypatch.setenv("BOT_KILL_SWITCH", "0")
    assert kill_isolated.is_killed() is False


def test_kill_file_engage_release(kill_isolated):
    assert kill_isolated.is_killed() is False
    assert kill_isolated.engage("test reason") is True
    assert kill_isolated.is_killed() is True
    assert "test reason" in kill_isolated.reason()
    assert kill_isolated.release() is True
    assert kill_isolated.is_killed() is False


def test_kill_never_raises(monkeypatch):
    # Even with a bogus path + no env, is_killed must return a bool, never raise.
    monkeypatch.setattr(ks, "KILL_FILE", "\0/bad/path")
    monkeypatch.delenv("BOT_KILL_SWITCH", raising=False)
    assert ks.is_killed() in (True, False)


# ── funding-arm per-arm loss cap + master kill ───────────────────────────────

class _FakeScanner:
    def __init__(self, opps):
        self._opps = opps

    def get_state(self):
        return self._opps


def _opp(symbol, apy, exchange="Binance"):
    return {"exchange": exchange, "symbol": symbol,
            "rate_8h": round(apy / 1095.0, 6), "apy": apy,
            "action": "SHORT PERP + LONG SPOT",
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _sim(tmp_path, monkeypatch, opps, *, kill=False, **kw):
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(fap, "_is_killed", lambda: kill)   # isolate from real flag
    return FundingArbPaperSim(scanner=_FakeScanner(opps), notifier=None,
                              state_file=tmp_path / "s.json", **kw)


def test_loss_cap_halts_new_entries(tmp_path, monkeypatch):
    # A rich 40% APY opp would normally open (cf. test_cost_gate_accepts_rich_apy).
    sim = _sim(tmp_path, monkeypatch, [_opp("SOLUSDT", 40.0)], max_drawdown_usd=5.0)
    monkeypatch.setattr(sim, "_total_pnl", lambda: -6.0)   # underwater past the cap
    sim._tick()
    assert len(sim.open_positions) == 0           # halted
    assert sim._halt_reason is not None


def test_within_cap_still_trades(tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch, [_opp("SOLUSDT", 40.0)], max_drawdown_usd=5.0)
    monkeypatch.setattr(sim, "_total_pnl", lambda: -3.0)   # above the cap
    sim._tick()
    assert len(sim.open_positions) == 1           # trades normally


def test_cap_zero_disabled(tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch, [_opp("SOLUSDT", 40.0)], max_drawdown_usd=0.0)
    monkeypatch.setattr(sim, "_total_pnl", lambda: -999.0)  # cap off → ignored
    sim._tick()
    assert len(sim.open_positions) == 1


def test_master_kill_halts_funding_entry(tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch, [_opp("SOLUSDT", 40.0)], kill=True)
    sim._tick()
    assert len(sim.open_positions) == 0
    assert sim._halt_reason is not None


def test_resume_after_halt_clears(tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch, [_opp("SOLUSDT", 40.0)], max_drawdown_usd=5.0)
    monkeypatch.setattr(sim, "_total_pnl", lambda: -6.0)
    sim._tick()
    assert sim._halt_reason is not None
    # Recover above the cap → halt clears, entries resume on the next tick.
    monkeypatch.setattr(sim, "_total_pnl", lambda: -1.0)
    sim._tick()
    assert sim._halt_reason is None
    assert len(sim.open_positions) == 1
