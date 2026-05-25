"""
Tests for the cost-aware funding-rate arbitrage paper simulator.

Focus: the round-trip cost model and the cost-aware entry gate. These are the
changes that turn the sim from a fantasy (gross-funding-only) into an honest
net-of-costs simulation — the same cost-expectancy discipline applied to the
microstructure scalper.
"""

from datetime import datetime, timezone, timedelta

import arbitrage.funding_arb_paper as fap
from arbitrage.funding_arb_paper import FundingArbPaperSim, PaperPosition


class _FakeScanner:
    """Stand-in for FundingScanner: returns a fixed opportunity list."""

    def __init__(self, opps):
        self._opps = opps

    def get_state(self):
        return self._opps


def _opp(symbol, apy, exchange="Binance"):
    """Build an opportunity dict matching FundingScanner.get_state() shape.

    The scanner stores rate_8h as a PERCENT; apy = rate_8h_frac * 3 * 365 * 100,
    so rate_8h_percent = apy / 1095.
    """
    return {
        "exchange": exchange,
        "symbol": symbol,
        "rate_8h": round(apy / 1095.0, 6),  # percent
        "apy": apy,
        "action": "SHORT PERP + LONG SPOT" if apy > 0 else "LONG PERP + SHORT SPOT",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _make_sim(tmp_path, opps, monkeypatch):
    # Isolate persistence so the test never touches real state.
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "funding_arb_state.json")
    return FundingArbPaperSim(scanner=_FakeScanner(opps), notifier=None)


def test_net_pnl_is_funding_minus_cost():
    pos = PaperPosition(
        symbol="BTCUSDT", exchange="Binance", direction="LONG_SPOT_SHORT_PERP",
        entry_apy=30.0, entry_rate_8h=0.000274, size_usd=500.0,
        entry_time_iso=datetime.now(timezone.utc).isoformat(),
        funding_collected=2.50, entry_cost=1.10,
    )
    assert abs(pos.net_pnl - (2.50 - 1.10)) < 1e-9


def test_entry_charges_round_trip_cost(tmp_path, monkeypatch):
    # apy=30% → ~8 breakeven cycles at default 0.22% cost → passes the gate.
    sim = _make_sim(tmp_path, [_opp("BTCUSDT", 30.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 1
    pos = next(iter(sim.open_positions.values()))
    # Invariant: entry cost is always the round-trip fraction of the (now
    # conviction-weighted) position size.
    expected_cost = fap.ROUND_TRIP_COST_FRAC * pos.size_usd
    assert abs(pos.entry_cost - expected_cost) < 1e-9
    # No funding accrued yet → net PnL starts negative (we paid to open).
    assert pos.net_pnl < 0


def test_conviction_sizing_scales_with_apy(tmp_path, monkeypatch):
    sim = _make_sim(tmp_path, [], monkeypatch)
    low  = sim._size_for_apy(30)    # just above the cost floor
    high = sim._size_for_apy(140)   # near the cap
    assert fap.MIN_POSITION_USD <= low < high <= fap.MAX_POSITION_USD
    # Clamps: below floor → MIN, above cap → MAX.
    assert sim._size_for_apy(5) == fap.MIN_POSITION_USD
    assert sim._size_for_apy(5000) == fap.MAX_POSITION_USD


def test_cost_gate_rejects_marginal_apy(tmp_path, monkeypatch):
    # apy=20% clears the 15% APY floor but NOT the cost gate:
    # breakeven = 0.0022 / (20/109500) ≈ 12 cycles > 10 → skip.
    sim = _make_sim(tmp_path, [_opp("ETHUSDT", 20.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_cost_gate_accepts_rich_apy(tmp_path, monkeypatch):
    # apy=40% → breakeven ≈ 6 cycles < 10 → open.
    sim = _make_sim(tmp_path, [_opp("SOLUSDT", 40.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 1


def test_apy_cap_rejects_absurd_funding(tmp_path, monkeypatch):
    # apy=2000% is an illiquid meme perp — above the 150% cap → skip.
    sim = _make_sim(tmp_path, [_opp("LITEUSDT", 2000.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_apy_within_band_opens(tmp_path, monkeypatch):
    # apy=80% is elevated-but-plausible: clears min (15%), cap (150%), cost gate.
    sim = _make_sim(tmp_path, [_opp("DOGEUSDT", 80.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 1


def test_total_pnl_is_net_of_costs(tmp_path, monkeypatch):
    sim = _make_sim(tmp_path, [_opp("BTCUSDT", 40.0)], monkeypatch)
    sim._tick()
    pos = next(iter(sim.open_positions.values()))

    # Simulate 9 funding cycles elapsing, then accrue.
    pos.last_funding_ts_iso = (
        datetime.now(timezone.utc) - timedelta(hours=9 * fap.FUNDING_CYCLE_HOURS)
    ).isoformat()
    sim._accrue_funding(pos, _opp("BTCUSDT", 40.0), datetime.now(timezone.utc))

    assert pos.funding_collected > 0
    # Cumulative total must equal gross funding minus costs.
    assert abs(sim._total_pnl()
               - (sim._total_gross_funding() - sim._total_costs())) < 1e-9
    assert abs(pos.net_pnl - (pos.funding_collected - pos.entry_cost)) < 1e-9
