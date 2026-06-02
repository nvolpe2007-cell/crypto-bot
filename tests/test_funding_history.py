"""
Tests for the funding-history tracker + persistence gate.

The persistence gate is the evidence-based fix for the Kraken arm's cycle-0
flip bleed: don't enter on a snapshot-positive rate, enter only when funding
has demonstrably HELD positive and the symbol isn't a serial flipper.
"""

from datetime import datetime, timezone, timedelta

import arbitrage.funding_arb_paper as fap
from arbitrage.funding_arb_paper import FundingArbPaperSim
from arbitrage.funding_history import FundingHistory


def _h(tmp_path, **kw):
    return FundingHistory(path=tmp_path / "hist.json", **kw)


def _seed(hist, key, apys, now, step_hours=1.0):
    """Seed `apys` oldest→newest, spaced step_hours apart ending at `now`."""
    n = len(apys)
    for i, a in enumerate(apys):
        ts = now - timedelta(hours=step_hours * (n - 1 - i))
        hist.samples.setdefault(key, []).append((ts.isoformat(), float(a)))


def test_consecutive_positive_hours_counts_trailing_run(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    # 20 hourly positive samples → ~19h continuous positive.
    _seed(hist, "K:PF_X", [50.0] * 20, now, step_hours=1.0)
    assert hist.consecutive_positive_hours("K:PF_X", now) >= 18.0
    assert hist.consecutive_positive_cycles("K:PF_X", now) >= 2.0


def test_negative_latest_sample_gives_zero(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path)
    _seed(hist, "K:PF_X", [50.0, 50.0, -10.0], now)
    assert hist.consecutive_positive_hours("K:PF_X", now) == 0.0


def test_gap_breaks_continuity(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    # Old positive block, a >3h gap, then 2 recent positive hourly samples.
    hist.samples["K:PF_X"] = [
        ((now - timedelta(hours=30)).isoformat(), 50.0),
        ((now - timedelta(hours=29)).isoformat(), 50.0),
        ((now - timedelta(hours=1)).isoformat(), 50.0),
        (now.isoformat(), 50.0),
    ]
    # Continuity only spans the last two samples (1h), not back across the gap.
    assert hist.consecutive_positive_hours("K:PF_X", now) <= 2.0


def test_flip_count(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path)
    _seed(hist, "K:PF_X", [10, -10, 10, -10, 10], now)  # 4 sign changes
    assert hist.flip_count("K:PF_X") == 4


def test_is_stable_requires_persistence_and_low_flips(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    _seed(hist, "K:STABLE", [40.0] * 20, now)           # 19h positive, 0 flips
    _seed(hist, "K:TWITCHY", [40, -5, 40, -5, 40, -5, 40], now)  # many flips
    assert hist.is_stable("K:STABLE", min_cycles=2, max_flips=6, now=now)
    assert not hist.is_stable("K:TWITCHY", min_cycles=2, max_flips=6, now=now)
    # Unknown symbol (cold start) → not stable.
    assert not hist.is_stable("K:UNSEEN", min_cycles=2, max_flips=6, now=now)


def test_record_downsamples(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, sample_interval_min=60)
    hist.record("K:PF_X", 30.0, now)
    hist.record("K:PF_X", 31.0, now + timedelta(minutes=5))   # too soon → skipped
    hist.record("K:PF_X", 32.0, now + timedelta(minutes=61))  # kept
    assert len(hist.samples["K:PF_X"]) == 2


def test_persistence_round_trips(tmp_path):
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path)
    _seed(hist, "K:PF_X", [10.0, 20.0], now)
    hist.save()
    reloaded = FundingHistory(path=tmp_path / "hist.json")
    assert reloaded.flip_count("K:PF_X") == 0
    assert len(reloaded.samples["K:PF_X"]) == 2


# ── gate integration ─────────────────────────────────────────────────────────

class _FakeScanner:
    def __init__(self, opps):
        self._opps = opps

    def get_state(self):
        return self._opps


def _opp(symbol, apy, exchange="Kraken Futures"):
    return {"exchange": exchange, "symbol": symbol,
            "rate_8h": round(apy / 1095.0, 6), "apy": apy,
            "action": "SHORT PERP + LONG SPOT", "timestamp": ""}


def _kraken_sim_with_history(tmp_path, opps, monkeypatch, hist):
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    return FundingArbPaperSim(
        scanner=_FakeScanner(opps), notifier=None,
        positive_funding_only=True, source_allowlist={"Kraken Futures"},
        cost_frac=0.0054, max_breakeven_cycles=6.0, max_entry_apy=300.0,
        max_positions=1, min_position_usd=100, max_position_usd=100,
        max_total_notional=100,
        history=hist, min_persistence_cycles=2.0, max_flips=6,
        state_file=tmp_path / "kraken_state.json", label="Funding Arb (Kraken)",
    )


def test_gate_blocks_cold_start(tmp_path, monkeypatch):
    # Rich APY that clears every other gate, but no history → persistence blocks.
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    sim = _kraken_sim_with_history(tmp_path, [_opp("PF_OMIUSD", 150.0)], monkeypatch, hist)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_gate_admits_after_persistence_confirmed(tmp_path, monkeypatch):
    # Pre-seed 20h of held-positive funding → persistence satisfied → opens.
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    _seed(hist, "Kraken Futures:PF_OMIUSD", [150.0] * 20, now)
    sim = _kraken_sim_with_history(tmp_path, [_opp("PF_OMIUSD", 150.0)], monkeypatch, hist)
    sim._tick()
    assert len(sim.open_positions) == 1
    pos = next(iter(sim.open_positions.values()))
    assert abs(pos.size_usd - 100.0) < 1e-9


def test_gate_blocks_serial_flipper(tmp_path, monkeypatch):
    # Currently positive and even recently persistent, but flipped too often.
    now = datetime.now(timezone.utc)
    hist = _h(tmp_path, max_gap_hours=3)
    flippy = [40, -5, 40, -5, 40, -5, 40, -5] + [40.0] * 20  # >6 flips, then steady
    _seed(hist, "Kraken Futures:PF_OMIUSD", flippy, now)
    sim = _kraken_sim_with_history(tmp_path, [_opp("PF_OMIUSD", 150.0)], monkeypatch, hist)
    sim._tick()
    assert len(sim.open_positions) == 0
