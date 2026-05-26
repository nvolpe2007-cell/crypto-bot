"""
Integration tests for the alt-perp execution layer: position lifecycle (open →
scaled exits → close), equity/PnL accounting, DB persistence, circuit breakers.
"""

from datetime import datetime, timezone, timedelta

from src.altperp import config, database
from src.altperp.confluence import Setup
from src.altperp.position_sizing import compute_size
from src.altperp.orders import PaperExecutor
from src.altperp.position_manager import PositionManager

T0 = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)


def _short_setup():
    return Setup(
        coin="SOLUSDT", direction="short", setup_type="fade_short",
        tier1_ok=True, tier2_score=1, cvd_confirmed=True, liq_proximity=False,
        size_multiplier=config.TIER2_CVD_SIZE_BOOST,
        context={"funding": {"funding_rate": 0.0007}, "oi": {"oi_4hr_change": 0.32}},
    )


def _pm(tmp_path):
    db = str(tmp_path / "altperp_test.db")
    return PositionManager(PaperExecutor(), db_path=db, alerter=None, starting_equity=1000.0), db


def test_short_lifecycle_tp1_then_tp3(tmp_path):
    pm, db = _pm(tmp_path)
    setup = _short_setup()
    plan = compute_size(pm.equity, 100.0, "short", "fade_short", setup.size_multiplier)
    assert plan is not None

    ok, reason = pm.can_open("SOLUSDT", T0)
    assert ok, reason
    pos = pm.open_position(setup, plan, price=100.0, now=T0)
    assert pos and "SOLUSDT" in pm.positions
    entry = pos.entry_price  # ~99.95 after sell slippage

    # Tick down 1.5%+ → TP1 partial (close 40%), trail activates
    pm.on_tick("SOLUSDT", entry * 0.984, 0.0006, 0.1, T0 + timedelta(minutes=5))
    p = pm.positions["SOLUSDT"]
    assert p.tp1_hit and p.trail_active and abs(p.remaining_fraction - 0.60) < 1e-6, p

    # Tick down 5%+ → TP3 full close of the remainder → position gone
    pm.on_tick("SOLUSDT", entry * 0.945, 0.0006, 0.1, T0 + timedelta(minutes=10))
    assert "SOLUSDT" not in pm.positions
    assert pm.equity > 1000.0, f"profitable short should grow equity, got {pm.equity}"

    # DB row closed with TP flags + net PnL recorded
    import sqlite3
    con = sqlite3.connect(db)
    row = con.execute("SELECT exit_reason, tp1_hit, tp3_hit, net_pnl_usdt FROM trades").fetchone()
    con.close()
    assert row[0] == "TP3" and row[1] == 1 and row[2] == 1 and row[3] > 0, row


def test_hard_stop_loses(tmp_path):
    pm, _ = _pm(tmp_path)
    setup = _short_setup()
    plan = compute_size(pm.equity, 100.0, "short", "fade_short", setup.size_multiplier)
    pos = pm.open_position(setup, plan, price=100.0, now=T0)
    entry = pos.entry_price
    # Price +2% against the short → hard STOP
    pm.on_tick("SOLUSDT", entry * 1.021, 0.0006, 0.1, T0 + timedelta(minutes=5))
    assert "SOLUSDT" not in pm.positions
    assert pm.equity < 1000.0


def test_concurrent_and_per_coin_limits(tmp_path):
    pm, _ = _pm(tmp_path)
    s = _short_setup()
    plan = compute_size(pm.equity, 100.0, "short", "fade_short", s.size_multiplier)
    pm.open_position(s, plan, price=100.0, now=T0)
    # same coin → blocked
    ok, why = pm.can_open("SOLUSDT", T0)
    assert not ok and why == "position_exists"
    # second coin ok (under max 2)
    ok2, _ = pm.can_open("AVAXUSDT", T0)
    assert ok2
    s2 = Setup(coin="AVAXUSDT", direction="short", setup_type="fade_short",
               tier1_ok=True, size_multiplier=1.0,
               context={"funding": {"funding_rate": 0.0007}, "oi": {"oi_4hr_change": 0.3}})
    pm.open_position(s2, compute_size(pm.equity, 30.0, "short", "fade_short", 1.0), price=30.0, now=T0)
    # third coin → max concurrent
    ok3, why3 = pm.can_open("ARBUSDT", T0)
    assert not ok3 and why3 == "max_concurrent"


def test_runner_evaluate_and_act_opens_short(tmp_path):
    from src.altperp.runner import evaluate_and_act
    pm, db = _pm(tmp_path)
    # Mock market that should trigger a fade short with CVD confirmation
    flat_klines = [{"close": 100.0 + (i % 2) * 0.1, "volume": 1000.0} for i in range(60)]
    market = {
        "price": 100.0,
        "funding_rate": 0.0007,                       # >= short threshold
        "funding_history": [0.0002] * 6,              # calm history → spike + eligible
        "oi_points": [{"ts": 1, "oi": 90}, {"ts": 2, "oi": 100}, {"ts": 3, "oi": 132}],  # +32%
        "perp_trades": [{"side": "Buy", "size": 80}, {"side": "Sell", "size": 10}],       # perp buying
        "spot_trades": [{"side": "Sell", "size": 50}],                                    # spot selling
        "orderbook": {"bids": [[99, 5], [98, 6], [97, 5]], "asks": [[101, 5], [102, 6]]},
        "klines": flat_klines,
    }
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)  # clear of post-funding block
    setup = evaluate_and_act("SOLUSDT", market, pm, btc_uptrend_ok=True, now=now, db_path=db)
    assert setup.should_enter and setup.direction == "short", setup
    assert "SOLUSDT" in pm.positions
    # a signal_log row was written with trade_fired=1
    import sqlite3
    con = sqlite3.connect(db)
    row = con.execute("SELECT tier1_triggered, trade_fired, setup_type FROM signal_log").fetchone()
    con.close()
    assert row == (1, 1, "fade_short"), row


def test_trend_follower_lifecycle(tmp_path):
    from src.altperp.trend import TrendSetup
    pm, db = _pm(tmp_path)
    setup = TrendSetup(coin="SOLUSDT", direction="long", setup_type="trend_long",
                       size_multiplier=1.0, stop_frac=0.06, atr=2.0,
                       context={"regime": "TRENDING_UP"})
    plan = compute_size(pm.equity, 100.0, "long", "trend_long", 1.0, stop_frac=setup.stop_frac)
    pos = pm.open_position(setup, plan, price=100.0, now=T0)
    assert pos.trail_active and pos.atr_at_entry == 2.0  # trend trails from entry
    entry = pos.entry_price

    # Ride up — no exit, anchor climbs
    pm.on_tick("SOLUSDT", 110.0, None, None, T0 + timedelta(hours=8))
    assert "SOLUSDT" in pm.positions and pm.positions["SOLUSDT"].trail_anchor == 110.0

    # Pull back past 3×ATR (6) below the 110 peak → TREND_STOP, banked a winner
    pm.on_tick("SOLUSDT", 103.0, None, None, T0 + timedelta(hours=12))
    assert "SOLUSDT" not in pm.positions
    assert pm.equity > 1000.0, pm.equity
    import sqlite3
    con = sqlite3.connect(db)
    reason = con.execute("SELECT exit_reason FROM trades").fetchone()[0]
    con.close()
    assert reason == "TREND_STOP", reason


def test_regime_router_wiring(tmp_path):
    from src.altperp import regime as rg
    from src.altperp.router import route
    from src.altperp.trend import evaluate as eval_trend
    # build a clean uptrend → classify → router routes to trend → trend fires
    kl = [{"ts": i, "open": 100 + i, "high": 101 + i, "low": 99 + i,
           "close": 100 + i, "volume": 1000} for i in range(60)]
    reg = rg.classify(kl)
    assert reg.regime == rg.TRENDING_UP
    tsetup = eval_trend("SOLUSDT", kl, reg)
    rd = route(reg.regime, {"trend": tsetup if tsetup.should_enter else None})
    assert rd.active_strategy == "trend" and rd.size_scale == 1.0, rd


def test_circuit_breaker_daily_drawdown(tmp_path):
    pm, _ = _pm(tmp_path)
    pm.equity = 940.0  # -6% vs day_start 1000
    pm._check_circuit_breakers(T0)
    assert pm.is_halted(T0)
    ok, why = pm.can_open("SOLUSDT", T0)
    assert not ok and "halted" in why
    # halt clears after the window
    assert not pm.is_halted(T0 + timedelta(hours=25))
