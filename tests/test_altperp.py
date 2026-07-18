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


def test_runner_routes_fade_in_volatile(tmp_path):
    from src.altperp.runner import evaluate_and_act
    from src.altperp import regime as rg
    pm, db = _pm(tmp_path)
    # VOLATILE regime: flat closes but huge bar ranges → fade is eligible
    vol_klines = [{"ts": i * 14400000, "open": 100.0, "high": 108.0, "low": 92.0,
                   "close": 100.0 + (i % 2) * 0.1, "volume": 1000.0} for i in range(60)]
    market = {
        "price": 100.0, "funding_rate": 0.0007, "funding_history": [0.0002] * 6,
        "oi_points": [{"ts": 1, "oi": 90}, {"ts": 2, "oi": 100}, {"ts": 3, "oi": 132}],
        "perp_trades": [{"side": "Buy", "size": 80}, {"side": "Sell", "size": 10}],
        "spot_trades": [{"side": "Sell", "size": 50}],
        "orderbook": {"bids": [[99, 5], [98, 6]], "asks": [[101, 5], [102, 6]]},
        "klines": vol_klines,
    }
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    reg, routed = evaluate_and_act("SOLUSDT", market, pm, btc_uptrend_ok=True, now=now, db_path=db)
    assert reg.regime == rg.VOLATILE, reg
    assert routed.active_strategy == "fade", routed
    assert "SOLUSDT" in pm.positions and pm.positions["SOLUSDT"].direction == "short"


def test_runner_routes_trend_in_uptrend(tmp_path):
    from src.altperp.runner import evaluate_and_act
    from src.altperp import regime as rg
    pm, db = _pm(tmp_path)
    kl = [{"ts": i * 14400000, "open": 100 + i * 2, "high": 101 + i * 2,
           "low": 99 + i * 2, "close": 100 + i * 2, "volume": 1000} for i in range(60)]
    market = {"price": kl[-1]["close"], "funding_rate": 0.0001,
              "funding_history": [0.0001] * 6,
              "oi_points": [{"ts": 1, "oi": 100}, {"ts": 2, "oi": 101}, {"ts": 3, "oi": 102}],
              "perp_trades": [], "spot_trades": [], "orderbook": {}, "klines": kl}
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    reg, routed = evaluate_and_act("SOLUSDT", market, pm, btc_uptrend_ok=True, now=now, db_path=db)
    assert reg.regime == rg.TRENDING_UP, reg
    assert routed.active_strategy == "trend", routed
    assert pm.positions["SOLUSDT"].setup_type == "trend_long"


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


# ── AI brain (gate-keeper) ────────────────────────────────────────────────────

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Resp:
    def __init__(self, blocks, usage=None):
        self.content, self.usage = blocks, usage


class _FakeClient:
    """Mimics anthropic.Anthropic: .messages.create() → tool_use response."""
    def __init__(self, tool_input):
        self._inp = tool_input
        self.messages = self
        self.kw = None

    def create(self, **kw):
        self.kw = kw
        return _Resp([_Block(type="tool_use", name="submit_trade_decision", input=self._inp)],
                     _Usage(1200, 90))


class _RaisingClient:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise RuntimeError("api down")


class _StubBrain:
    """Stands in for AIBrain in runner tests — returns a fixed decision."""
    def __init__(self, decision):
        self._d = decision
        self.ctx = None

    def decide(self, coin, setup, ctx, now):
        self.ctx = ctx
        return self._d


def _fade_market():
    vol_klines = [{"ts": i * 14400000, "open": 100.0, "high": 108.0, "low": 92.0,
                   "close": 100.0 + (i % 2) * 0.1, "volume": 1000.0} for i in range(60)]
    return {
        "price": 100.0, "funding_rate": 0.0007, "funding_history": [0.0002] * 6,
        "oi_points": [{"ts": 1, "oi": 90}, {"ts": 2, "oi": 100}, {"ts": 3, "oi": 132}],
        "perp_trades": [{"side": "Buy", "size": 80}, {"side": "Sell", "size": 10}],
        "spot_trades": [{"side": "Sell", "size": 50}],
        "orderbook": {"bids": [[99, 5], [98, 6]], "asks": [[101, 5], [102, 6]]},
        "klines": vol_klines,
    }


def test_ai_brain_parses_and_clamps_size():
    from src.altperp.ai_brain import AIBrain
    client = _FakeClient({"action": "confirm", "confidence": 9, "size_multiplier": 2.0,
                          "key_signal": "velocity flip down", "invalidation": "funding < 0.02%",
                          "urgency": "enter_now", "reasoning": "fragile leveraged top"})
    brain = AIBrain(client=client, model="test-model")
    d = brain.decide("SOLUSDT", _short_setup(), {"regime": "VOLATILE"}, T0)
    assert d.confirmed and d.confidence == 9
    # model said 2.0; hard cap is the structural MAX_SIZE_BOOST
    assert d.size_multiplier == config.MAX_SIZE_BOOST
    assert d.input_tokens == 1200 and d.model == "test-model"
    # forced tool + cached system prompt
    assert client.kw["tool_choice"] == {"type": "tool", "name": "submit_trade_decision"}
    assert client.kw["system"][0]["cache_control"]["type"] == "ephemeral"


def test_ai_brain_fail_closed_on_error():
    from src.altperp.ai_brain import AIBrain
    d = AIBrain(client=_RaisingClient()).decide("SOLUSDT", _short_setup(), {}, T0)
    assert not d.confirmed and d.action == "veto" and d.error


def test_runner_ai_veto_blocks_trade(tmp_path):
    import sqlite3
    from src.altperp.runner import evaluate_and_act
    from src.altperp.ai_brain import AIDecision
    pm, db = _pm(tmp_path)
    brain = _StubBrain(AIDecision(action="veto", confidence=2, key_signal="still rising",
                                  reasoning="funding still climbing, crowd growing"))
    reg, routed = evaluate_and_act("SOLUSDT", _fade_market(), pm, True, T0, db_path=db, brain=brain)
    assert routed.active_strategy == "fade"          # gate passed
    assert "SOLUSDT" not in pm.positions             # but AI vetoed → no trade
    con = sqlite3.connect(db)
    row = con.execute("SELECT action, trade_fired FROM ai_decisions").fetchone()
    con.close()
    assert row == ("veto", 0)


def test_runner_ai_confirm_opens_trade(tmp_path):
    import sqlite3
    from src.altperp.runner import evaluate_and_act
    from src.altperp.ai_brain import AIDecision
    pm, db = _pm(tmp_path)
    brain = _StubBrain(AIDecision(action="confirm", confidence=8, size_multiplier=0.5,
                                  key_signal="taker distribution"))
    evaluate_and_act("SOLUSDT", _fade_market(), pm, True, T0, db_path=db, brain=brain)
    assert "SOLUSDT" in pm.positions and pm.positions["SOLUSDT"].direction == "short"
    # AI received the enriched context
    assert brain.ctx["regime"] == "VOLATILE" and "funding_dynamics" in brain.ctx
    con = sqlite3.connect(db)
    row = con.execute("SELECT action, trade_fired FROM ai_decisions").fetchone()
    con.close()
    assert row == ("confirm", 1)


def test_runner_ai_below_confidence_floor_blocks(tmp_path):
    from src.altperp.runner import evaluate_and_act
    from src.altperp.ai_brain import AIDecision
    pm, db = _pm(tmp_path)
    # confirm, but below the default floor (6) → treated as no-go
    brain = _StubBrain(AIDecision(action="confirm", confidence=3, size_multiplier=1.0))
    evaluate_and_act("SOLUSDT", _fade_market(), pm, True, T0, db_path=db, brain=brain)
    assert "SOLUSDT" not in pm.positions


def test_bounded_ai_mult_caps_flush_long():
    from src.altperp.runner import _bounded_ai_mult
    # flush longs are higher-risk → never boosted, even if the AI asks for more
    assert _bounded_ai_mult("flush_long", 1.5) == 1.0
    assert _bounded_ai_mult("flush_long", 0.5) == 0.5
    # fades may reach the structural cap
    assert _bounded_ai_mult("fade_short", 1.5) == config.MAX_SIZE_BOOST
    assert _bounded_ai_mult("fade_short", 0.5) == 0.5


def test_scenario_gate_behaviour():
    # the scenario harness asserts gate behaviour across calm/froth/flip/uptrend/flush
    from src.altperp import scenarios
    scenarios._selftest()  # raises AssertionError on any regression


def test_all_weather_backtest_runs():
    # trend rides a move to a chandelier stop; MR reverts to target — both net-positive
    from src.altperp import backtest_all
    backtest_all._selftest()  # raises AssertionError on any regression


def test_backtest_selftest():
    # synthetic extreme-funding short into a clean down-move nets positive
    from src.altperp import backtest
    backtest._selftest()  # raises AssertionError on any regression


def test_confluence_selftest():
    # tier1/tier2 sizing, trend filter, post-funding block, flush-long gating
    from src.altperp import confluence
    confluence._selftest()  # raises AssertionError on any regression


def test_database_selftest():
    # signal log + trade open/close round-trip through sqlite
    from src.altperp import database as altperp_database
    altperp_database._selftest()  # raises AssertionError on any regression


def test_exits_selftest():
    # hard stop, TP1/TP3, funding stop, trailing, time stop for both directions
    from src.altperp import exits
    exits._selftest()  # raises AssertionError on any regression


def test_math_utils_selftest():
    # CVD-from-trades, pct_change, funding/volume spike thresholds
    from src.altperp import math_utils
    math_utils._selftest()  # raises AssertionError on any regression


def test_mean_reversion_selftest():
    # mr_long/mr_short trigger on dips/spikes away from the mean, flat → no trade
    from src.altperp import mean_reversion
    mean_reversion._selftest()  # raises AssertionError on any regression


def test_orders_selftest():
    # paper fills apply slippage correctly; LIVE execution must raise, never silently fire
    from src.altperp import orders
    orders._selftest()  # raises AssertionError on any regression


def test_position_sizing_selftest():
    # risk-based notional/leverage sizing, including the leverage-cap clamp
    from src.altperp import position_sizing
    position_sizing._selftest()  # raises AssertionError on any regression


def test_regime_selftest():
    # trending/calm/ranging/volatile/crash classification on synthetic bars
    from src.altperp import regime
    regime._selftest()  # raises AssertionError on any regression


def test_router_selftest():
    # regime → strategy routing, including flat-on-no-signal and unbuilt-strategy cases
    from src.altperp import router
    router._selftest()  # raises AssertionError on any regression


def test_signals_selftest():
    # funding/OI/CVD/liquidity/trend/funding-dynamics signal extraction
    from src.altperp import signals
    signals._selftest()  # raises AssertionError on any regression


def test_telegram_selftest():
    # AltperpAlerter message formatting with no notifier (logs instead of sending)
    from src.altperp import telegram
    telegram._selftest()  # raises AssertionError on any regression


def test_time_utils_selftest():
    # minutes-to-funding-reset, pre/post-funding window boundaries
    from src.altperp import time_utils
    time_utils._selftest()  # raises AssertionError on any regression


def test_trend_selftest():
    # breakout-on-uptrend entry; flat tape stays flat
    from src.altperp import trend
    trend._selftest()  # raises AssertionError on any regression


def test_circuit_breaker_daily_drawdown(tmp_path):
    pm, _ = _pm(tmp_path)
    pm.equity = 940.0  # -6% vs day_start 1000
    pm._check_circuit_breakers(T0)
    assert pm.is_halted(T0)
    ok, why = pm.can_open("SOLUSDT", T0)
    assert not ok and "halted" in why
    # halt clears after the window
    assert not pm.is_halted(T0 + timedelta(hours=25))
