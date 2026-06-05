"""
Tests for the cost-aware funding-rate arbitrage paper simulator.

Focus: the round-trip cost model, the cost-aware entry gate, and the exit
logic (_should_exit).  The exit conditions were previously completely untested;
they are also where the off_scanner_24h bug lived (last_funding_ts_iso was used
as a proxy for "last scanner sighting" but was updated even on off-scanner
decayed-rate ticks, so the 24h gate never fired).
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


def _make_majors_sim(tmp_path, opps, monkeypatch):
    # Mirrors the deployed majors arm: Kraken Futures only (the executable venue)
    # at a realistic ~0.54% round-trip cost. See the realism fix in paper_trading.
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    return FundingArbPaperSim(
        scanner=_FakeScanner(opps), notifier=None,
        positive_funding_only=True,
        symbol_allowlist=fap.MAJOR_SYMBOLS,
        source_allowlist={"Kraken Futures"},
        cost_frac=0.0054,
        state_file=tmp_path / "majors_state.json",
        label="Funding Arb (majors)",
    )


def _kf_opp(symbol, apy):
    """A Kraken-Futures opportunity (the only venue the majors arm executes)."""
    return _opp(symbol, apy, exchange="Kraken Futures")


def test_majors_arm_rejects_non_major(tmp_path, monkeypatch):
    # ENJ isn't in the majors allowlist → conservative arm skips it.
    sim = _make_majors_sim(tmp_path, [_kf_opp("ENJUSDT", 80.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_majors_arm_rejects_negative_funding(tmp_path, monkeypatch):
    # Negative funding would need a spot short (borrow) → conservative arm skips.
    sim = _make_majors_sim(tmp_path, [_kf_opp("BTCUSDT", -80.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_majors_arm_rejects_non_executable_venue(tmp_path, monkeypatch):
    # A positive major well above the floor, but on Binance — not executable for
    # a US-restricted account. The source allowlist must reject it.
    sim = _make_majors_sim(tmp_path, [_opp("BTCUSDT", 80.0, exchange="Binance")],
                           monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_majors_arm_opens_rich_executable_major(tmp_path, monkeypatch):
    # Positive major on Kraken Futures, above the realistic-cost floor (~59%) → opens.
    sim = _make_majors_sim(tmp_path, [_kf_opp("BTCUSDT", 80.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 1


def test_majors_arm_skips_below_realistic_floor(tmp_path, monkeypatch):
    # 15% APY cleared the old 0.08%-cost floor but not the realistic 0.54% one
    # (~59%): at honest cost it doesn't break even in time, so the arm passes.
    sim = _make_majors_sim(tmp_path, [_kf_opp("BTCUSDT", 15.0)], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_majors_arm_realistic_cost_floor(tmp_path, monkeypatch):
    # Realistic Kraken round-trip cost (~0.54%) lifts the APY floor far above the
    # old optimistic ~9%: 0.0054 / 10 cycles × 3 × 365 × 100 ≈ 59%.
    sim = _make_majors_sim(tmp_path, [], monkeypatch)
    assert 50.0 < sim._apy_floor() < 65.0


def _make_kraken_sim(tmp_path, opps, monkeypatch, max_be=2.0):
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    return FundingArbPaperSim(
        scanner=_FakeScanner(opps), notifier=None,
        positive_funding_only=True,
        source_allowlist={"Kraken Futures"},
        cost_frac=0.0064,
        max_breakeven_cycles=max_be,
        state_file=tmp_path / "kraken_state.json",
        label="Funding Arb (Kraken)",
    )


def test_kraken_persistence_gate_rejects_microcap(tmp_path, monkeypatch):
    # 140% APY microcap on Kraken: at 0.64% cost, breakeven ≈ 5 cycles. The
    # strict 2-cycle persistence gate rejects it (funding won't survive long
    # enough to clear honest cost) — this is the funding_arb_kraken_bleed fix.
    opp = _opp("PF_OMIUSD", 140.0, exchange="Kraken Futures")
    sim = _make_kraken_sim(tmp_path, [opp], monkeypatch, max_be=2.0)
    sim._tick()
    assert len(sim.open_positions) == 0


def test_kraken_arm_with_lax_gate_would_open(tmp_path, monkeypatch):
    # Same opportunity under the lax 10-cycle gate WOULD open — proves the
    # per-arm threshold (not the cost/cap/sign filters) is what gates it out.
    opp = _opp("PF_OMIUSD", 140.0, exchange="Kraken Futures")
    sim = _make_kraken_sim(tmp_path, [opp], monkeypatch, max_be=10.0)
    sim._tick()
    assert len(sim.open_positions) == 1


def test_default_arm_breakeven_gate_unchanged(tmp_path, monkeypatch):
    # Arms that don't pass max_breakeven_cycles keep the module default (10).
    sim = _make_sim(tmp_path, [], monkeypatch)
    assert sim.max_breakeven_cycles == fap.MAX_BREAKEVEN_CYCLES


def _make_aggressive_kraken_sim(tmp_path, opps, monkeypatch, alloc=500.0):
    # Mirrors the live aggressive Kraken config: all-in single position,
    # maker-only cost, gate relaxed to 6 cycles, cap raised to 300%.
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    return FundingArbPaperSim(
        scanner=_FakeScanner(opps), notifier=None,
        positive_funding_only=True,
        source_allowlist={"Kraken Futures"},
        cost_frac=0.0054,
        max_breakeven_cycles=6.0,
        max_entry_apy=300.0,
        max_positions=1,
        min_position_usd=alloc, max_position_usd=alloc, max_total_notional=alloc,
        state_file=tmp_path / "kraken_state.json",
        label="Funding Arb (Kraken)",
    )


def test_aggressive_kraken_goes_all_in_one_position(tmp_path, monkeypatch):
    # Two rich opportunities, but max_positions=1 → only one opens, sized at the
    # full allocation (no conviction scaling since min==max==total).
    opps = [
        _opp("PF_OMIUSD", 160.0, exchange="Kraken Futures"),
        _opp("PF_DEXEUSD", 145.0, exchange="Kraken Futures"),
    ]
    sim = _make_aggressive_kraken_sim(tmp_path, opps, monkeypatch, alloc=500.0)
    sim._tick()
    assert len(sim.open_positions) == 1
    pos = next(iter(sim.open_positions.values()))
    assert abs(pos.size_usd - 500.0) < 1e-9          # all-in, full alloc
    assert abs(pos.entry_cost - 0.0054 * 500.0) < 1e-9  # maker-only cost


def test_aggressive_kraken_relaxed_gate_admits_rich_apy(tmp_path, monkeypatch):
    # 150% APY: at 0.54% maker cost, breakeven ≈ 3.9 cycles < 6 → the relaxed
    # gate admits it (the strict 2-cycle gate would have rejected it).
    opp = _opp("PF_OMIUSD", 150.0, exchange="Kraken Futures")
    sim = _make_aggressive_kraken_sim(tmp_path, [opp], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 1


def test_aggressive_kraken_still_rejects_thin_funding(tmp_path, monkeypatch):
    # 60% APY: breakeven = 0.0054 / (60/109500) ≈ 9.9 cycles > 6 → still skipped.
    # Proves the +EV gate survives the relax — it's aggressive, not blind.
    opp = _opp("PF_THINUSD", 60.0, exchange="Kraken Futures")
    sim = _make_aggressive_kraken_sim(tmp_path, [opp], monkeypatch)
    sim._tick()
    assert len(sim.open_positions) == 0


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


# ── per-venue funding interval (#2) ──────────────────────────────────────────

def test_kraken_interval_is_hourly_others_8h():
    assert fap._funding_interval_hours("Kraken Futures") == fap.KRAKEN_FUNDING_INTERVAL_HOURS
    assert fap._funding_interval_hours("Binance") == float(fap.FUNDING_CYCLE_HOURS)
    assert fap._funding_interval_hours(None) == float(fap.FUNDING_CYCLE_HOURS)


def test_kraken_position_stamped_hourly_at_open(tmp_path, monkeypatch):
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    sim = FundingArbPaperSim(scanner=_FakeScanner([]), notifier=None)
    sim._open_position(_opp("PF_XBTUSD", 40.0, exchange="Kraken Futures"),
                       datetime.now(timezone.utc))
    pos = next(iter(sim.open_positions.values()))
    assert pos.funding_interval_hours == fap.KRAKEN_FUNDING_INTERVAL_HOURS


def test_total_funding_invariant_to_interval():
    """Funding APY is annualized: over the same hold, total funding collected is
    the same whether accrued in 1h or 8h increments. Shorter interval only changes
    granularity (banking partial funding before a flip), not expectancy."""
    now = datetime.now(timezone.utc)
    sim = FundingArbPaperSim.__new__(FundingArbPaperSim)  # no I/O needed
    opp = _opp("X", 40.0)

    def collect(interval_h):
        pos = PaperPosition(
            symbol="X", exchange="e", direction="LONG_SPOT_SHORT_PERP",
            entry_apy=40.0, entry_rate_8h=40.0 / 1095.0 / 100.0, size_usd=1000.0,
            entry_time_iso=now.isoformat(), last_funding_ts_iso=now.isoformat(),
            last_seen_iso=now.isoformat(), funding_interval_hours=interval_h,
        )
        # 24h later — an exact multiple of both 1h and 8h.
        sim._accrue_funding(pos, opp, now + timedelta(hours=24))
        return pos.funding_collected, pos.cycles_collected

    f8, c8 = collect(8.0)
    f1, c1 = collect(1.0)
    assert abs(f8 - f1) < 1e-9          # same total funding
    assert c8 == 3 and c1 == 24         # finer granularity at 1h


def test_off_scanner_accrues_no_phantom_funding(tmp_path, monkeypatch):
    """A position that's fallen off the scanner must NOT keep booking funding —
    off-scanner means its rate dropped below the scanner's threshold, so any
    credit is phantom income. Default OFFSCANNER_RATE_FRAC=0 → zero funding."""
    sim = _sim_for_exit(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    pos = _open_pos(entry_apy=40.0)           # long-spot → no borrow either
    pos.last_funding_ts_iso = now.isoformat()
    sim._accrue_funding(pos, None, now + timedelta(hours=24))  # 3 cycles, off-scanner
    assert pos.cycles_collected == 3          # time still advances
    assert pos.funding_collected == 0.0       # but no phantom funding booked


# ── _should_exit — exit condition tests ──────────────────────────────────────

def _open_pos(entry_apy: float = 40.0,
              hours_old: float = 0.0,
              last_seen_hours_ago: float = 0.0) -> PaperPosition:
    """Build an open PaperPosition with controllable age and scanner-visibility."""
    now = datetime.now(timezone.utc)
    entry_time = now - timedelta(hours=hours_old)
    last_seen  = now - timedelta(hours=last_seen_hours_ago)
    return PaperPosition(
        symbol="BTCUSDT", exchange="Binance",
        direction="LONG_SPOT_SHORT_PERP",
        entry_apy=entry_apy,
        entry_rate_8h=entry_apy / 1095.0 / 100.0,  # fraction
        size_usd=500.0,
        entry_time_iso=entry_time.isoformat(),
        last_seen_iso=last_seen.isoformat(),
    )


def _sim_for_exit(tmp_path, monkeypatch) -> FundingArbPaperSim:
    monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
    return FundingArbPaperSim(scanner=_FakeScanner([]), notifier=None)


class TestShouldExit:

    def test_no_exit_healthy_position(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0, hours_old=24.0)
        opp = _opp("BTCUSDT", 40.0)  # rate unchanged, on scanner
        assert sim._should_exit(pos, opp, datetime.now(timezone.utc)) is None

    # ── exit 1: max hold ──────────────────────────────────────────────────────

    def test_exits_after_max_hold_days(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(hours_old=fap.MAX_HOLD_DAYS * 24 + 1)
        opp = _opp("BTCUSDT", 40.0)
        reason = sim._should_exit(pos, opp, datetime.now(timezone.utc))
        assert reason is not None and "max_hold" in reason

    def test_no_exit_just_under_max_hold(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(hours_old=fap.MAX_HOLD_DAYS * 24 - 1)
        opp = _opp("BTCUSDT", 40.0)
        assert sim._should_exit(pos, opp, datetime.now(timezone.utc)) is None

    # ── exit 2: funding flipped sign ──────────────────────────────────────────

    def test_exits_when_positive_funding_flips_negative(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0)   # entered as LONG_SPOT_SHORT_PERP
        opp = _opp("BTCUSDT", -10.0)     # rate is now negative
        reason = sim._should_exit(pos, opp, datetime.now(timezone.utc))
        assert reason == "funding_flipped"

    def test_exits_when_negative_funding_flips_positive(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=-40.0)  # entered SHORT_SPOT_LONG_PERP
        opp = _opp("BTCUSDT", 10.0)      # rate is now positive
        reason = sim._should_exit(pos, opp, datetime.now(timezone.utc))
        assert reason == "funding_flipped"

    def test_no_exit_when_sign_unchanged(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0)
        opp = _opp("BTCUSDT", 20.0)     # positive but lower — sign same
        assert sim._should_exit(pos, opp, datetime.now(timezone.utc)) is None

    # ── exit 3: APY decayed below threshold ───────────────────────────────────

    def test_exits_when_apy_decays_below_fraction(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0)
        # EXIT_APY_FRACTION=0.40: exit when current < 40% of entry (40*0.40=16%)
        # Use 15% — just below the threshold.
        opp = _opp("BTCUSDT", 15.0)
        reason = sim._should_exit(pos, opp, datetime.now(timezone.utc))
        assert reason is not None and "apy_decayed" in reason

    def test_no_exit_when_apy_above_fraction(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0)
        # 17% > 40*0.40=16% → stays open
        opp = _opp("BTCUSDT", 17.0)
        assert sim._should_exit(pos, opp, datetime.now(timezone.utc)) is None

    def test_exits_on_exact_decay_boundary(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(entry_apy=40.0)
        # Exactly at boundary (40 * 0.40 = 16.0) — abs(16.0) < abs(16.0) is False,
        # so it should NOT exit at the exact threshold (strict <).
        opp = _opp("BTCUSDT", 16.0)
        assert sim._should_exit(pos, opp, datetime.now(timezone.utc)) is None

    # ── exit 4: off scanner 24h (the previously broken gate) ─────────────────

    def test_exits_when_off_scanner_over_24h(self, tmp_path, monkeypatch):
        """Core regression test for the off_scanner_24h bug.

        Before the fix, _should_exit used last_funding_ts_iso, which gets
        updated on every decayed-rate accrual tick. That reset the 24h clock
        continuously, making the gate never fire. Now it uses last_seen_iso
        (updated only when scanner data is present), so the gate correctly
        fires after 25h of scanner absence.
        """
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(last_seen_hours_ago=25.0)  # last seen 25h ago
        reason = sim._should_exit(pos, None, datetime.now(timezone.utc))
        assert reason == "off_scanner_24h"

    def test_no_exit_when_off_scanner_under_24h(self, tmp_path, monkeypatch):
        sim = _sim_for_exit(tmp_path, monkeypatch)
        pos = _open_pos(last_seen_hours_ago=23.0)
        assert sim._should_exit(pos, None, datetime.now(timezone.utc)) is None

    def test_off_scanner_gate_survives_decayed_accruals(self, tmp_path, monkeypatch):
        """Verify that calling _accrue_funding with current_opp=None (decayed rate)
        does NOT update last_seen_iso and therefore does NOT reset the 24h clock."""
        sim = _sim_for_exit(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        pos = _open_pos(last_seen_hours_ago=20.0)  # seen 20h ago
        # Simulate three 8h off-scanner accrual ticks: only last_funding_ts_iso
        # should change, not last_seen_iso.
        for i in range(1, 4):
            tick_now = now + timedelta(hours=i * 8)
            pos.last_funding_ts_iso = (now - timedelta(hours=20 - i * 8)).isoformat()
            sim._accrue_funding(pos, None, tick_now)

        # last_seen_iso must still be 20h before 'now', not updated.
        last_seen = datetime.fromisoformat(pos.last_seen_iso)
        assert (now - last_seen).total_seconds() >= 20 * 3600 - 1

        # After 25h total, the exit should fire (20h already elapsed + 5h gap).
        final_now = now + timedelta(hours=5)
        reason = sim._should_exit(pos, None, final_now)
        assert reason == "off_scanner_24h"

    def test_on_scanner_resets_last_seen(self, tmp_path, monkeypatch):
        """When the symbol comes back onto the scanner, last_seen_iso is refreshed
        via _accrue_funding — resetting the 24h clock correctly."""
        sim = _sim_for_exit(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        pos = _open_pos(last_seen_hours_ago=23.0)
        # Scanner returns data → _accrue_funding updates last_seen_iso.
        sim._accrue_funding(pos, _opp("BTCUSDT", 40.0), now)
        # Now last_seen_iso ≈ now; the 25h check should NOT fire.
        reason = sim._should_exit(pos, None, now + timedelta(hours=2))
        assert reason is None

    # ── fallback: no last_seen_iso (old persisted positions) ─────────────────

    def test_no_last_seen_iso_uses_entry_time(self, tmp_path, monkeypatch):
        """Positions persisted before last_seen_iso was added have last_seen_iso=None.
        The exit check must fall back to entry_time to avoid crashing and should
        close the position if entry was more than 24h ago with no current scanner data."""
        sim = _sim_for_exit(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        pos = _open_pos(hours_old=26.0)
        pos.last_seen_iso = None    # simulate old serialized state
        reason = sim._should_exit(pos, None, now)
        assert reason == "off_scanner_24h"

    # ── source_allowlist — Kraken-only arm ────────────────────────────────────

    def test_source_allowlist_rejects_non_allowlisted_exchange(self, tmp_path, monkeypatch):
        """Kraken-only arm must reject Binance/Bybit opportunities."""
        monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
        sim = FundingArbPaperSim(
            scanner=_FakeScanner([_opp("BTCUSDT", 40.0, exchange="Binance")]),
            notifier=None,
            source_allowlist={"Kraken Futures"},
            state_file=tmp_path / "kraken_state.json",
        )
        sim._tick()
        assert len(sim.open_positions) == 0

    def test_source_allowlist_accepts_allowlisted_exchange(self, tmp_path, monkeypatch):
        """Kraken-only arm must accept opportunities from Kraken Futures."""
        monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
        sim = FundingArbPaperSim(
            scanner=_FakeScanner([_opp("PF_XBTUSD", 40.0, exchange="Kraken Futures")]),
            notifier=None,
            source_allowlist={"Kraken Futures"},
            state_file=tmp_path / "kraken_state.json",
        )
        sim._tick()
        assert len(sim.open_positions) == 1

    # ── max positions cap ─────────────────────────────────────────────────────

    def test_max_positions_blocks_new_entries(self, tmp_path, monkeypatch):
        """Once max_positions is reached, no further entries are opened."""
        opps = [_opp(f"COIN{i}USDT", 40.0) for i in range(5)]
        monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "state.json")
        sim = FundingArbPaperSim(
            scanner=_FakeScanner(opps), notifier=None,
            max_positions=2,
            state_file=tmp_path / "cap_state.json",
        )
        sim._tick()
        assert len(sim.open_positions) == 2

    # ── state persistence round-trip ──────────────────────────────────────────

    def test_state_persists_and_reloads(self, tmp_path, monkeypatch):
        """Save state, reload into a fresh sim instance, and verify positions survive."""
        state_file = tmp_path / "persist_state.json"
        monkeypatch.setattr(fap, "STATE_FILE", state_file)

        sim1 = FundingArbPaperSim(
            scanner=_FakeScanner([_opp("BTCUSDT", 40.0)]),
            notifier=None,
            state_file=state_file,
        )
        sim1._tick()
        assert len(sim1.open_positions) == 1
        # State is saved inside _tick(); no extra call needed.

        sim2 = FundingArbPaperSim(
            scanner=_FakeScanner([]),
            notifier=None,
            state_file=state_file,
        )
        assert len(sim2.open_positions) == 1
        pos1 = next(iter(sim1.open_positions.values()))
        pos2 = next(iter(sim2.open_positions.values()))
        assert pos1.symbol == pos2.symbol
        assert abs(pos1.entry_cost - pos2.entry_cost) < 1e-9
        # last_seen_iso must survive the round-trip.
        assert pos2.last_seen_iso is not None


# ── _base_symbol — symbol normalisation ──────────────────────────────────────

class TestBaseSymbol:
    """_base_symbol strips exchange-specific prefixes/suffixes and normalises
    venue aliases (XBT→BTC, XDG→DOGE) so that symbol allowlists work across
    Binance, Bybit, and Kraken Futures without per-venue special-casing."""

    def test_binance_usdt_pair(self):
        assert fap._base_symbol("BTCUSDT") == "BTC"

    def test_binance_usd_pair(self):
        assert fap._base_symbol("ETHUSD") == "ETH"

    def test_binance_usdc_pair(self):
        assert fap._base_symbol("SOLUSDC") == "SOL"

    def test_kraken_futures_pf_prefix_stripped(self):
        assert fap._base_symbol("PF_SOLUSD") == "SOL"

    def test_kraken_xbt_alias_normalised_to_btc(self):
        """Kraken lists Bitcoin as XBT; without normalisation PF_XBTUSD never
        matches a 'BTC' entry in the symbol allowlist."""
        assert fap._base_symbol("PF_XBTUSD") == "BTC"

    def test_kraken_xdg_alias_normalised_to_doge(self):
        """Kraken lists Dogecoin as XDG."""
        assert fap._base_symbol("PF_XDGUSD") == "DOGE"

    def test_lowercase_input(self):
        assert fap._base_symbol("btcusdt") == "BTC"

    def test_no_quote_suffix_returned_as_upper(self):
        """Symbol with no recognised quote suffix is returned uppercased as-is."""
        assert fap._base_symbol("BTC") == "BTC"

    def test_quote_priority_usdt_before_usd(self):
        """USDT must be stripped before USD so 'BTCUSDT' → 'BTC' not 'BTCUS'."""
        result = fap._base_symbol("BTCUSDT")
        assert result == "BTC"

    def test_kraken_eth_pf_prefix(self):
        assert fap._base_symbol("PF_ETHUSD") == "ETH"


# ── _accrue_funding — borrow cost model ──────────────────────────────────────

class TestBorrowCostAccrual:
    """The borrow cost model for SHORT_SPOT_LONG_PERP (negative-funding) trades.

    Shorting spot requires borrowing the asset; the old model charged only a
    one-off entry cost and ignored carry, inflating the aggressive arm's P&L
    on microcap shorts.  These tests pin the corrected behaviour.
    """

    def _short_pos(self, symbol: str = "BTCUSDT", size_usd: float = 500.0) -> PaperPosition:
        """Return a SHORT_SPOT_LONG_PERP position with 1 funding cycle due."""
        now = datetime.now(timezone.utc)
        entry = now - timedelta(hours=9)   # 9h ago → int(9/8)=1 cycle due
        return PaperPosition(
            symbol=symbol,
            exchange="Binance",
            direction="SHORT_SPOT_LONG_PERP",
            entry_apy=-40.0,
            entry_rate_8h=-40.0 / 109500.0,  # fraction; 109500 = 1095*100
            size_usd=size_usd,
            entry_time_iso=entry.isoformat(),
            last_funding_ts_iso=entry.isoformat(),
        )

    def _long_pos(self) -> PaperPosition:
        """Return a LONG_SPOT_SHORT_PERP position with 1 funding cycle due."""
        now = datetime.now(timezone.utc)
        entry = now - timedelta(hours=9)
        return PaperPosition(
            symbol="BTCUSDT",
            exchange="Binance",
            direction="LONG_SPOT_SHORT_PERP",
            entry_apy=40.0,
            entry_rate_8h=40.0 / 109500.0,
            size_usd=500.0,
            entry_time_iso=entry.isoformat(),
            last_funding_ts_iso=entry.isoformat(),
        )

    def _sim(self, tmp_path, monkeypatch) -> FundingArbPaperSim:
        monkeypatch.setattr(fap, "STATE_FILE", tmp_path / "s.json")
        return FundingArbPaperSim(scanner=_FakeScanner([]), notifier=None)

    def test_long_position_never_accrues_borrow_cost(self, tmp_path, monkeypatch):
        """LONG_SPOT_SHORT_PERP owns the spot asset outright — zero borrow needed."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._long_pos()
        sim._accrue_funding(pos, _opp("BTCUSDT", 40.0), datetime.now(timezone.utc))
        assert pos.borrow_cost == 0.0

    def test_short_major_uses_major_borrow_rate(self, tmp_path, monkeypatch):
        """SHORT_SPOT on a liquid major (BTC) charges BORROW_APY_MAJOR per cycle."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="BTCUSDT", size_usd=500.0)
        sim._accrue_funding(pos, _opp("BTCUSDT", -40.0), datetime.now(timezone.utc))

        expected = (
            (fap.BORROW_APY_MAJOR / 100.0)
            * 500.0
            * (fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
        )
        assert abs(pos.borrow_cost - expected) < 1e-9

    def test_short_alt_uses_higher_alt_borrow_rate(self, tmp_path, monkeypatch):
        """SHORT_SPOT on an illiquid alt charges the steeper BORROW_APY_ALT."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="OBSCUREUSDT", size_usd=500.0)
        sim._accrue_funding(pos, _opp("OBSCUREUSDT", -40.0), datetime.now(timezone.utc))

        expected = (
            (fap.BORROW_APY_ALT / 100.0)
            * 500.0
            * (fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
        )
        assert abs(pos.borrow_cost - expected) < 1e-9

    def test_alt_borrow_rate_is_higher_than_major(self, tmp_path, monkeypatch):
        """Sanity: alt borrow APY must exceed major borrow APY."""
        assert fap.BORROW_APY_ALT > fap.BORROW_APY_MAJOR

    def test_borrow_scales_with_multiple_cycles(self, tmp_path, monkeypatch):
        """Three elapsed cycles → 3× the per-cycle borrow charge."""
        sim = self._sim(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        pos = self._short_pos(symbol="BTCUSDT", size_usd=500.0)
        # Override last timestamp so 3 cycles are due
        pos.last_funding_ts_iso = (now - timedelta(hours=25)).isoformat()
        pos.entry_time_iso = pos.last_funding_ts_iso
        sim._accrue_funding(pos, _opp("BTCUSDT", -40.0), now)

        cycles = int(25 // fap.FUNDING_CYCLE_HOURS)   # = 3
        expected = (
            (fap.BORROW_APY_MAJOR / 100.0)
            * 500.0
            * (fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
            * cycles
        )
        assert abs(pos.borrow_cost - expected) < 1e-9

    def test_borrow_cost_accrues_off_scanner(self, tmp_path, monkeypatch):
        """Borrow must keep accruing even when the symbol drops off the scanner
        (current_opp=None) — the short is still open and the borrow is still owed."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="BTCUSDT", size_usd=500.0)
        sim._accrue_funding(pos, None, datetime.now(timezone.utc))  # off-scanner

        expected = (
            (fap.BORROW_APY_MAJOR / 100.0)
            * 500.0
            * (fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
        )
        assert abs(pos.borrow_cost - expected) < 1e-9

    def test_net_pnl_deducts_borrow_from_short_position(self, tmp_path, monkeypatch):
        """net_pnl must equal funding_collected − entry_cost − borrow_cost."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="BTCUSDT", size_usd=500.0)
        pos.entry_cost = 1.50
        sim._accrue_funding(pos, _opp("BTCUSDT", -40.0), datetime.now(timezone.utc))

        assert pos.borrow_cost > 0.0    # sanity: borrow was charged
        assert abs(
            pos.net_pnl - (pos.funding_collected - pos.entry_cost - pos.borrow_cost)
        ) < 1e-9

    def test_total_costs_includes_borrow_for_short(self, tmp_path, monkeypatch):
        """_total_costs() must aggregate entry_cost + borrow_cost so that
        _total_pnl() == _total_gross_funding() - _total_costs() holds."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="BTCUSDT", size_usd=500.0)
        pos.entry_cost = 1.50
        sim.open_positions["Binance:BTCUSDT"] = pos
        sim._accrue_funding(pos, _opp("BTCUSDT", -40.0), datetime.now(timezone.utc))

        assert abs(
            sim._total_costs() - (pos.entry_cost + pos.borrow_cost)
        ) < 1e-9
        assert abs(
            sim._total_pnl() - (sim._total_gross_funding() - sim._total_costs())
        ) < 1e-9

    def test_kraken_xbt_symbol_uses_major_borrow_rate(self, tmp_path, monkeypatch):
        """PF_XBTUSD: Kraken's XBT alias must map to BTC → major borrow rate."""
        sim = self._sim(tmp_path, monkeypatch)
        pos = self._short_pos(symbol="PF_XBTUSD", size_usd=500.0)
        sim._accrue_funding(pos, _opp("PF_XBTUSD", -40.0, exchange="Kraken Futures"),
                            datetime.now(timezone.utc))

        expected = (
            (fap.BORROW_APY_MAJOR / 100.0)
            * 500.0
            * (fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
        )
        assert abs(pos.borrow_cost - expected) < 1e-9


# ── Post-loss re-entry cooldown (realized-outcome feedback loop) ───────────────

def _cooldown_sim(tmp_path, opps, hours):
    """A both-sides sim with the flip-cooldown enabled and isolated state."""
    return FundingArbPaperSim(
        scanner=_FakeScanner(opps), notifier=None,
        flip_cooldown_hours=hours,
        state_file=tmp_path / "cooldown_state.json",
    )


def test_net_loss_close_arms_cooldown(tmp_path):
    opps = [_opp("PF_DEXEUSD", 140.0, exchange="Kraken Futures")]
    sim = _cooldown_sim(tmp_path, opps, hours=48)
    sim._tick()                                   # opens (no funding yet)
    key = "Kraken Futures:PF_DEXEUSD"
    assert key in sim.open_positions
    # Funding flips negative → funding_flipped close at a net loss.
    sim.scanner = _FakeScanner([_opp("PF_DEXEUSD", -140.0, exchange="Kraken Futures")])
    sim._tick()
    assert key not in sim.open_positions
    assert key in sim._flip_cooldowns             # cooldown stamped


def test_cooldown_blocks_reentry(tmp_path):
    key = "Kraken Futures:PF_DEXEUSD"
    sim = _cooldown_sim(tmp_path, [_opp("PF_DEXEUSD", 140.0, exchange="Kraken Futures")], hours=48)
    sim._tick(); sim.scanner = _FakeScanner([_opp("PF_DEXEUSD", -140.0, exchange="Kraken Futures")]); sim._tick()
    assert key in sim._flip_cooldowns
    # A fresh, attractive positive opp on the SAME symbol must be refused.
    sim.scanner = _FakeScanner([_opp("PF_DEXEUSD", 140.0, exchange="Kraken Futures")])
    sim._tick()
    assert key not in sim.open_positions          # cooldown held the line


def test_cooldown_expires_and_is_pruned(tmp_path):
    key = "Kraken Futures:PF_DEXEUSD"
    sim = _cooldown_sim(tmp_path, [], hours=24)
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    sim._flip_cooldowns[key] = old.isoformat()
    now = datetime.now(timezone.utc)
    assert sim._cooldown_remaining_h(key, now) is None   # 30h > 24h → expired
    assert key not in sim._flip_cooldowns                 # pruned on read


def test_cooldown_remaining_is_positive_within_window(tmp_path):
    key = "Kraken Futures:PF_DEXEUSD"
    sim = _cooldown_sim(tmp_path, [], hours=48)
    sim._flip_cooldowns[key] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    rem = sim._cooldown_remaining_h(key, datetime.now(timezone.utc))
    assert 37.0 < rem < 39.0                              # ~38h left


def test_cooldown_disabled_by_default(tmp_path):
    # hours=0 → never records, never blocks (the module/arm default for arms
    # that don't opt in).
    sim = _cooldown_sim(tmp_path, [_opp("PF_DEXEUSD", 140.0, exchange="Kraken Futures")], hours=0)
    sim._tick(); sim.scanner = _FakeScanner([_opp("PF_DEXEUSD", -140.0, exchange="Kraken Futures")]); sim._tick()
    assert sim._flip_cooldowns == {}
    assert sim._cooldown_remaining_h("Kraken Futures:PF_DEXEUSD", datetime.now(timezone.utc)) is None


def test_cooldown_persists_across_reload(tmp_path):
    key = "Kraken Futures:PF_DEXEUSD"
    sim = _cooldown_sim(tmp_path, [], hours=48)
    sim._flip_cooldowns[key] = datetime.now(timezone.utc).isoformat()
    sim._save_state()
    sim2 = _cooldown_sim(tmp_path, [], hours=48)          # reloads same state file
    assert key in sim2._flip_cooldowns


# ── Rolling-window net P&L (feedback / observability) ─────────────────────────

def test_net_pnl_since_windows_closed_positions(tmp_path):
    sim = _cooldown_sim(tmp_path, [], hours=0)
    now = datetime.now(timezone.utc)
    def _closed(net, age_days):
        # net_pnl = funding_collected - entry_cost - borrow_cost; pin cost at 1.0
        # and solve funding for the exact net we want.
        p = PaperPosition(
            symbol="X", exchange="Kraken Futures", direction="LONG_SPOT_SHORT_PERP",
            entry_apy=140.0, entry_rate_8h=0.001, size_usd=100.0,
            entry_time_iso=(now - timedelta(days=age_days + 1)).isoformat(),
            funding_collected=net + 1.0, entry_cost=1.0,
        )
        p.close_time_iso = (now - timedelta(days=age_days)).isoformat()
        return p
    sim.closed_positions = [_closed(-20.0, age_days=20), _closed(5.0, age_days=2)]
    cutoff = now - timedelta(days=7)
    # Only the 2-day-old +5 falls in the window; the 20-day-old -20 is excluded.
    assert abs(sim.net_pnl_since(cutoff) - 5.0) < 1e-9
    # Lifetime still includes the legacy loss.
    assert abs(sim._total_pnl() - (-15.0)) < 1e-9


# ── Borrow-corrected P&L (unpaid-carry exposure) ──────────────────────────────

def test_borrow_owed_zero_for_long_spot(tmp_path):
    sim = _cooldown_sim(tmp_path, [], hours=0)
    p = PaperPosition(
        symbol="BTCUSDT", exchange="Binance", direction="LONG_SPOT_SHORT_PERP",
        entry_apy=30.0, entry_rate_8h=0.000274, size_usd=500.0,
        entry_time_iso=datetime.now(timezone.utc).isoformat(),
        funding_collected=5.0, entry_cost=1.0, cycles_collected=10,
    )
    assert sim._borrow_owed(p) == 0.0          # own the asset → no borrow


def test_borrow_owed_uses_alt_tier_for_microcap(tmp_path):
    sim = _cooldown_sim(tmp_path, [], hours=0)
    p = PaperPosition(
        symbol="XCNUSDT", exchange="Binance", direction="SHORT_SPOT_LONG_PERP",
        entry_apy=-137.0, entry_rate_8h=-0.00125, size_usd=900.0,
        entry_time_iso=datetime.now(timezone.utc).isoformat(),
        funding_collected=11.0, entry_cost=2.0, cycles_collected=21,
    )
    # 50% alt APY × $900 × (21×8h / 8760h)
    expected = (fap.BORROW_APY_ALT / 100.0) * 900.0 * (21 * fap.FUNDING_CYCLE_HOURS / (24.0 * 365.0))
    assert abs(sim._borrow_owed(p) - expected) < 1e-9


def test_borrow_corrected_strips_unpaid_carry(tmp_path):
    sim = _cooldown_sim(tmp_path, [], hours=0)
    # A legacy short-spot winner charged $0 borrow (pre-model) but owes plenty.
    legacy = PaperPosition(
        symbol="XCNUSDT", exchange="Binance", direction="SHORT_SPOT_LONG_PERP",
        entry_apy=-137.0, entry_rate_8h=-0.00125, size_usd=900.0,
        entry_time_iso=datetime.now(timezone.utc).isoformat(),
        funding_collected=11.69, entry_cost=2.0, borrow_cost=0.0, cycles_collected=21,
    )
    sim.closed_positions = [legacy]
    booked = sim._total_pnl()
    corrected = sim.borrow_corrected_pnl()
    assert booked > 0                          # looks profitable as booked
    assert corrected < booked                  # honest carry deflates it
    owed = sim._borrow_owed(legacy)
    assert abs(corrected - (booked - owed)) < 1e-9


def test_borrow_corrected_equals_total_for_positive_only(tmp_path):
    # An arm with no short-spot legs: corrected == booked (nothing to charge).
    sim = _cooldown_sim(tmp_path, [], hours=0)
    sim.closed_positions = [PaperPosition(
        symbol="BTCUSDT", exchange="Binance", direction="LONG_SPOT_SHORT_PERP",
        entry_apy=30.0, entry_rate_8h=0.000274, size_usd=500.0,
        entry_time_iso=datetime.now(timezone.utc).isoformat(),
        funding_collected=4.0, entry_cost=1.1, cycles_collected=8,
    )]
    assert abs(sim.borrow_corrected_pnl() - sim._total_pnl()) < 1e-9
