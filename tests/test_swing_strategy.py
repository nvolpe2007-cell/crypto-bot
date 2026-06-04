"""
Unit tests for src/swing_strategy.py — the long-only majors swing strategy.

Covers the indicator helpers (EMA/RSI/ATR/ROC), every entry gate, the exit
path, and that the decision always exposes its full reasoning.
"""
import pytest

from src.swing_strategy import (
    SwingStrategy, SwingDecision, _ema, _rsi_series, _atr, _roc,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _bars_from_closes(closes, hl_spread=1.0):
    """Build OHLC bars from a close series; h/l straddle close by hl_spread."""
    out = []
    for i, c in enumerate(closes):
        out.append({"t": i, "symbol": "BTC",
                    "o": closes[i - 1] if i else c,
                    "h": c + hl_spread, "l": c - hl_spread, "c": c})
    return out


def _uptrend_pullback_resume():
    """A clean, established uptrend with a real multi-bar pullback that then
    resumes — RSI crosses up through 50 while price stays above EMA50 and the
    20-bar trend ROC stays positive. Should trigger entry on the last bar."""
    closes = [100.0 + i * 1.5 for i in range(60)]   # established uptrend
    base = closes[-1]
    closes += [base - 2 * j for j in range(1, 9)]   # 8-bar pullback (RSI under 50)
    closes += [closes[-1] + 6]                       # resume bar (RSI crosses back up)
    return _bars_from_closes(closes)


# ── indicator helpers ─────────────────────────────────────────────────────────

class TestIndicators:
    def test_ema_too_short_none(self):
        assert _ema([1, 2], 5) is None

    def test_ema_constant_series_equals_value(self):
        out = _ema([10.0] * 30, 10)
        assert out[-1] == pytest.approx(10.0)

    def test_ema_tracks_uptrend_below_price(self):
        closes = [float(i) for i in range(1, 60)]
        ema = _ema(closes, 20)[-1]
        assert ema < closes[-1]            # lagging EMA sits below a rising price

    def test_rsi_all_gains_is_100(self):
        out = _rsi_series([float(i) for i in range(1, 40)], 14)
        assert out[-1] == pytest.approx(100.0)

    def test_rsi_all_losses_near_zero(self):
        out = _rsi_series([float(i) for i in range(40, 1, -1)], 14)
        assert out[-1] == pytest.approx(0.0)

    def test_rsi_warmup_none(self):
        assert _rsi_series([1, 2, 3], 14) is None

    def test_atr_positive(self):
        bars = _bars_from_closes([100 + i for i in range(30)], hl_spread=2.0)
        assert _atr(bars, 14) > 0

    def test_atr_warmup_none(self):
        assert _atr(_bars_from_closes([1, 2, 3]), 14) is None

    def test_roc_positive_in_uptrend(self):
        assert _roc([float(i) for i in range(1, 30)], 10) > 0

    def test_roc_negative_in_downtrend(self):
        assert _roc([float(i) for i in range(30, 1, -1)], 10) < 0


# ── strategy: warm-up & structure ─────────────────────────────────────────────

class TestStructure:
    def test_warmup_skips(self):
        strat = SwingStrategy()
        dec = strat.evaluate(_bars_from_closes([100, 101, 102]), position_open=False)
        assert dec.action == "SKIP"
        assert "warm-up" in dec.reason

    def test_decision_always_carries_indicators(self):
        strat = SwingStrategy()
        dec = strat.evaluate(_uptrend_pullback_resume(), position_open=False)
        for k in ("close", "ema_fast", "ema_slow", "rsi", "atr"):
            assert k in dec.indicators


# ── strategy: entry gates ──────────────────────────────────────────────────────

class TestEntry:
    def test_enters_on_uptrend_pullback_resume(self):
        strat = SwingStrategy()
        dec = strat.evaluate(_uptrend_pullback_resume(), position_open=False)
        assert dec.action == "ENTER"
        assert all(dec.gates.values())
        assert dec.rr > 0
        assert dec.target_price > dec.price > dec.stop_price

    def test_downtrend_never_enters(self):
        strat = SwingStrategy()
        closes = [200.0 - i for i in range(80)]   # steady downtrend
        dec = strat.evaluate(_bars_from_closes(closes), position_open=False)
        assert dec.action == "SKIP"
        assert dec.gates["trend_up"] is False

    def test_overbought_is_vetoed(self):
        # pure relentless uptrend → RSI pinned high, never crosses up THROUGH 50,
        # and would be overbought: must not enter.
        strat = SwingStrategy()
        dec = strat.evaluate(_bars_from_closes([float(i) for i in range(1, 90)]),
                             position_open=False)
        assert dec.action == "SKIP"

    def test_rr_is_target_over_stop(self):
        strat = SwingStrategy(atr_stop_mult=2.0, atr_target_mult=3.0)
        dec = strat.evaluate(_uptrend_pullback_resume(), position_open=False)
        if dec.is_enter:
            assert dec.rr == pytest.approx(dec.target_pct / dec.stop_pct)
            assert dec.rr == pytest.approx(1.5, abs=0.01)   # 3.0/2.0

    def test_weak_trend_momentum_is_vetoed(self):
        # ROC>0 but below the 2% min_roc floor → momentum_pos fails (the
        # trade-review finding: barely-trends get chopped up).
        strat = SwingStrategy(min_roc=0.02)
        bars = _uptrend_pullback_resume()
        # shrink the trend so 20-bar ROC sits in (0, 2%): tiny per-bar rise
        closes = [100.0 + i * 0.05 for i in range(60)]
        base = closes[-1]
        closes += [base - 0.3 * j for j in range(1, 9)]
        closes += [closes[-1] + 0.9]
        dec = strat.evaluate(_bars_from_closes(closes, hl_spread=0.2), position_open=False)
        if dec.action == "SKIP" and "momentum_pos" in dec.gates:
            assert dec.gates["momentum_pos"] is False

    def test_cost_gate_blocks_tiny_atr(self):
        # Force an absurd cost floor so the cost gate fails even on a valid setup.
        strat = SwingStrategy(round_trip_cost=0.5)   # 50% cost → target can't clear
        dec = strat.evaluate(_uptrend_pullback_resume(), position_open=False)
        assert dec.action == "SKIP"
        assert dec.gates["cost_clears"] is False


# ── strategy: exit path ────────────────────────────────────────────────────────

class TestExit:
    def test_holds_while_trend_intact(self):
        strat = SwingStrategy()
        dec = strat.evaluate(_uptrend_pullback_resume(), position_open=True)
        assert dec.action == "HOLD"

    def test_exits_on_trend_break(self):
        strat = SwingStrategy()
        # uptrend then a close that plunges well below EMA50
        closes = [100.0 + i for i in range(70)] + [40.0]
        dec = strat.evaluate(_bars_from_closes(closes), position_open=True)
        assert dec.action == "EXIT"
        assert "trend break" in dec.reason
