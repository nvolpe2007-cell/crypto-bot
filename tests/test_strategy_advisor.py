"""
Unit tests for src/strategy_advisor.py

Covers:
- StrategyAdvisor.start(): reloads its TradeJournal from disk on every loop
  iteration, so it picks up trades the trading loop wrote through a SEPARATE
  TradeJournal instance (bot.py constructs one TradeJournal() for the
  advisor and another for run_paper_trading_session/LiveTrader — they don't
  share in-memory state, only the file on disk).
- Analysis functions: _pct, _grade, _conf_label, _analyse_trades, _streak,
  _today_records, _strategic_advice, _hourly_message, _eod_message
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.strategy_advisor import (
    StrategyAdvisor,
    _pct, _grade, _conf_label,
    _analyse_trades, _today_records, _streak,
    _strategic_advice, _hourly_message, _eod_message,
)
from src.trade_journal import TradeRecord


# ── Shared fixture factory ────────────────────────────────────────────────────

def _rec(
    won=True,
    pnl=10.0,
    symbol="BTC/USD",
    closed_at="2026-06-30T12:00:00+00:00",
    confidence=70.0,
    ofi_score=0.0,
    lead_lag_score=0.0,
    regime_score=0.0,
    regime="TRENDING",
):
    return TradeRecord(
        trade_id="t1",
        symbol=symbol,
        opened_at="2026-06-30T11:00:00+00:00",
        closed_at=closed_at,
        rsi=50.0,
        adx=25.0,
        volume_ratio=1.0,
        regime=regime,
        atr_pct=0.5,
        ema100_gap=0.5,
        ema200_gap=1.0,
        hour_utc=12,
        day_of_week=1,
        pnl=pnl,
        pnl_pct=pnl / 100.0,
        won=won,
        reason="TAKE_PROFIT",
        confidence=confidence,
        ofi_score=ofi_score,
        lead_lag_score=lead_lag_score,
        regime_score=regime_score,
    )


def _journal(records):
    j = MagicMock()
    j.records = records
    return j


def _make_advisor():
    journal = MagicMock()
    journal.records = []
    notifier = MagicMock()
    return StrategyAdvisor(notifier, journal), journal, notifier


class TestStrategyAdvisorReload:
    async def test_start_reloads_journal_before_first_check(self):
        advisor, journal, _ = _make_advisor()

        # First asyncio.sleep call is the 30s startup delay; the second is
        # the end of the first loop iteration — raise there to stop the
        # otherwise-infinite `while True` loop after exactly one pass.
        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        journal.reload.assert_called_once()

    async def test_start_reloads_journal_on_every_iteration(self):
        advisor, journal, _ = _make_advisor()

        with patch(
            "asyncio.sleep",
            AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()]),
        ):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        assert journal.reload.call_count == 3

    async def test_reload_exception_does_not_crash_loop(self):
        """A failed reload (e.g. transient disk hiccup) is swallowed by the
        loop's own exception handler, same as any other per-iteration error."""
        advisor, journal, _ = _make_advisor()
        journal.reload.side_effect = OSError("disk hiccup")

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        journal.reload.assert_called_once()


# ── _pct ─────────────────────────────────────────────────────────────────────

class TestPct:
    def test_normal(self):
        assert _pct(3, 10) == pytest.approx(30.0)

    def test_zero_denominator_returns_zero(self):
        assert _pct(5, 0) == 0.0

    def test_zero_numerator(self):
        assert _pct(0, 10) == 0.0

    def test_full(self):
        assert _pct(10, 10) == pytest.approx(100.0)


# ── _grade ───────────────────────────────────────────────────────────────────

class TestGrade:
    def test_excellent(self):
        assert "Excellent" in _grade(65)
        assert "Excellent" in _grade(80)

    def test_good(self):
        assert "Good" in _grade(55)
        assert "Good" in _grade(64)

    def test_marginal(self):
        assert "Marginal" in _grade(45)
        assert "Marginal" in _grade(54)

    def test_poor(self):
        assert "Poor" in _grade(0)
        assert "Poor" in _grade(44)


# ── _conf_label ───────────────────────────────────────────────────────────────

class TestConfLabel:
    def test_very_high(self):
        assert _conf_label(85) == "Very High"
        assert _conf_label(95) == "Very High"

    def test_high(self):
        assert _conf_label(70) == "High"
        assert _conf_label(84) == "High"

    def test_moderate(self):
        assert _conf_label(55) == "Moderate"
        assert _conf_label(69) == "Moderate"

    def test_low(self):
        assert _conf_label(0) == "Low"
        assert _conf_label(54) == "Low"


# ── _analyse_trades ───────────────────────────────────────────────────────────

class TestAnalyseTrades:
    def test_empty_returns_empty_dict(self):
        assert _analyse_trades([]) == {}

    def test_single_win(self):
        r = _rec(won=True, pnl=20.0, confidence=80.0)
        s = _analyse_trades([r])
        assert s["total"] == 1
        assert s["wins"] == 1
        assert s["losses"] == 0
        assert s["win_rate"] == pytest.approx(100.0)
        assert s["total_pnl"] == pytest.approx(20.0)
        assert s["avg_win"] == pytest.approx(20.0)

    def test_single_loss(self):
        r = _rec(won=False, pnl=-10.0, confidence=60.0)
        s = _analyse_trades([r])
        assert s["wins"] == 0
        assert s["losses"] == 1
        assert s["win_rate"] == pytest.approx(0.0)
        assert s["total_pnl"] == pytest.approx(-10.0)
        assert s["avg_loss"] == pytest.approx(-10.0)

    def test_mixed_win_rate(self):
        records = [
            _rec(won=True, pnl=10.0),
            _rec(won=True, pnl=15.0),
            _rec(won=False, pnl=-5.0),
        ]
        s = _analyse_trades(records)
        assert s["total"] == 3
        assert s["wins"] == 2
        assert s["win_rate"] == pytest.approx(100 * 2 / 3)
        assert s["total_pnl"] == pytest.approx(20.0)

    def test_signal_wr_requires_at_least_3_triggers(self):
        # Only 2 records with high ofi_score — should NOT appear in signal_wr
        records = [
            _rec(won=True, ofi_score=20.0),
            _rec(won=False, ofi_score=20.0),
        ]
        s = _analyse_trades(records)
        assert "OFI" not in s["signal_wr"]

    def test_signal_wr_computed_when_enough_triggers(self):
        # 3 records with ofi_score > 15; 2 win → 66.7% WR for OFI
        records = [
            _rec(won=True, ofi_score=20.0),
            _rec(won=True, ofi_score=20.0),
            _rec(won=False, ofi_score=20.0),
        ]
        s = _analyse_trades(records)
        assert "OFI" in s["signal_wr"]
        assert s["signal_wr"]["OFI"] == pytest.approx(100 * 2 / 3)

    def test_by_symbol_requires_at_least_2_trades(self):
        records = [_rec(symbol="BTC/USD"), _rec(symbol="ETH/USD")]
        s = _analyse_trades(records)
        # ETH only has 1 record → not in sym_wr; BTC only 1 → not either
        assert "BTC/USD" not in s["sym_wr"]
        assert "ETH/USD" not in s["sym_wr"]

    def test_by_symbol_grouped_correctly(self):
        records = [
            _rec(symbol="BTC/USD", won=True),
            _rec(symbol="BTC/USD", won=False),
        ]
        s = _analyse_trades(records)
        assert "BTC/USD" in s["sym_wr"]
        assert s["sym_wr"]["BTC/USD"] == pytest.approx(50.0)

    def test_confidence_calibration_bucketing(self):
        records = [
            _rec(won=True,  confidence=90.0),   # >85
            _rec(won=False, confidence=90.0),   # >85
            _rec(won=True,  confidence=60.0),   # 55-70
        ]
        s = _analyse_trades(records)
        cal = s["conf_calibration"]
        assert ">85" in cal
        assert "55-70" in cal
        assert cal[">85"] == pytest.approx(50.0)
        assert cal["55-70"] == pytest.approx(100.0)

    def test_best_and_worst_trade(self):
        records = [
            _rec(pnl=100.0, won=True),
            _rec(pnl=-50.0, won=False),
            _rec(pnl=10.0,  won=True),
        ]
        s = _analyse_trades(records)
        assert s["best"].pnl == pytest.approx(100.0)
        assert s["worst"].pnl == pytest.approx(-50.0)


# ── _streak ───────────────────────────────────────────────────────────────────

class TestStreak:
    def test_empty_journal(self):
        j = _journal([])
        assert _streak(j) == (0, "none")

    def test_single_win(self):
        j = _journal([_rec(won=True)])
        n, kind = _streak(j)
        assert n == 1
        assert kind == "win"

    def test_single_loss(self):
        j = _journal([_rec(won=False)])
        n, kind = _streak(j)
        assert n == 1
        assert kind == "loss"

    def test_consecutive_wins(self):
        records = [
            _rec(won=True, closed_at="2026-06-30T10:00:00+00:00"),
            _rec(won=True, closed_at="2026-06-30T11:00:00+00:00"),
            _rec(won=True, closed_at="2026-06-30T12:00:00+00:00"),
        ]
        j = _journal(records)
        n, kind = _streak(j)
        assert n == 3
        assert kind == "win"

    def test_streak_breaks_on_loss(self):
        # Most-recent is a win; but the oldest is a loss — streak = 2 wins
        records = [
            _rec(won=False, closed_at="2026-06-30T09:00:00+00:00"),
            _rec(won=True,  closed_at="2026-06-30T10:00:00+00:00"),
            _rec(won=True,  closed_at="2026-06-30T11:00:00+00:00"),
        ]
        j = _journal(records)
        n, kind = _streak(j)
        assert n == 2
        assert kind == "win"

    def test_loss_streak(self):
        records = [
            _rec(won=False, closed_at="2026-06-30T10:00:00+00:00"),
            _rec(won=False, closed_at="2026-06-30T11:00:00+00:00"),
            _rec(won=False, closed_at="2026-06-30T12:00:00+00:00"),
            _rec(won=False, closed_at="2026-06-30T13:00:00+00:00"),
        ]
        j = _journal(records)
        n, kind = _streak(j)
        assert n == 4
        assert kind == "loss"


# ── _today_records ────────────────────────────────────────────────────────────

class TestTodayRecords:
    def test_filters_to_today(self):
        records = [
            _rec(closed_at="2026-06-30T12:00:00+00:00"),  # today
            _rec(closed_at="2026-06-29T12:00:00+00:00"),  # yesterday
        ]
        j = _journal(records)
        with patch("src.strategy_advisor.datetime") as mock_dt:
            from datetime import datetime, timezone, date
            mock_dt.now.return_value = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            result = _today_records(j)
        assert len(result) == 1
        assert result[0].closed_at == "2026-06-30T12:00:00+00:00"

    def test_empty_journal(self):
        j = _journal([])
        with patch("src.strategy_advisor.datetime") as mock_dt:
            from datetime import datetime, timezone
            mock_dt.now.return_value = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            result = _today_records(j)
        assert result == []

    def test_malformed_date_skipped_gracefully(self):
        bad = _rec(closed_at="not-a-date")
        j = _journal([bad])
        with patch("src.strategy_advisor.datetime") as mock_dt:
            from datetime import datetime, timezone
            mock_dt.now.return_value = datetime(2026, 6, 30, 15, 0, 0, tzinfo=timezone.utc)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            result = _today_records(j)
        assert result == []


# ── _strategic_advice ─────────────────────────────────────────────────────────

class TestStrategicAdvice:
    def _run(self, today_stats, alltime_stats, records=None):
        j = _journal(records or [])
        return _strategic_advice(today_stats, alltime_stats, j)

    def test_four_loss_streak_triggers_strong_warning(self):
        losses = [
            _rec(won=False, closed_at=f"2026-06-30T{h:02d}:00:00+00:00")
            for h in range(4)
        ]
        advice = self._run({}, {}, losses)
        assert any("4 losses" in a for a in advice)

    def test_three_loss_streak_triggers_moderate_warning(self):
        losses = [
            _rec(won=False, closed_at=f"2026-06-30T{h:02d}:00:00+00:00")
            for h in range(3)
        ]
        advice = self._run({}, {}, losses)
        assert any("3-loss" in a for a in advice)

    def test_conf_cal_zero_high_bucket_still_triggers_advice(self):
        # Bug fix: 0.0 win-rate for '>85' was falsy → advice silently dropped.
        # With the fix, the advice fires correctly.
        alltime = {
            "conf_calibration": {
                ">85": 0.0,     # 0% WR for high-confidence trades (falsy float)
                "55-70": 60.0,
            }
        }
        advice = self._run({}, alltime)
        assert any("Confidence score" in a or "confidence" in a.lower() for a in advice), \
            "Expected calibration advice when high-confidence WR=0 but none was emitted"

    def test_conf_cal_high_beats_low_by_10pct(self):
        alltime = {"conf_calibration": {">85": 75.0, "55-70": 60.0}}
        advice = self._run({}, alltime)
        assert any("raise your minimum confidence" in a for a in advice)

    def test_conf_cal_high_underperforms_low(self):
        alltime = {"conf_calibration": {">85": 40.0, "55-70": 60.0}}
        advice = self._run({}, alltime)
        assert any("not predicting wins well" in a for a in advice)

    def test_worst_signal_below_40_pct(self):
        alltime = {"signal_wr": {"OFI": 35.0, "Lead-Lag": 60.0}}
        advice = self._run({}, alltime)
        assert any("OFI" in a for a in advice)

    def test_best_signal_at_65_pct(self):
        alltime = {"signal_wr": {"OFI": 65.0, "Lead-Lag": 40.0}}
        advice = self._run({}, alltime)
        assert any("OFI" in a and "best signal" in a for a in advice)

    def test_worst_symbol_below_40_pct(self):
        alltime = {"sym_wr": {"BTC/USD": 35.0, "ETH/USD": 60.0}}
        advice = self._run({}, alltime)
        assert any("BTC" in a for a in advice)

    def test_low_win_rate_over_20_trades(self):
        alltime = {"total": 20, "win_rate": 40.0}
        advice = self._run({}, alltime)
        assert any("below breakeven" in a or "40%" in a for a in advice)

    def test_high_win_rate_over_20_trades(self):
        alltime = {"total": 20, "win_rate": 60.0}
        advice = self._run({}, alltime)
        assert any("working" in a for a in advice)

    def test_no_conditions_returns_fallback(self):
        advice = self._run({}, {})
        assert any("Not enough data" in a for a in advice)


# ── _hourly_message ───────────────────────────────────────────────────────────

class TestHourlyMessage:
    _EMPTY_STATE = {"account": {"total_equity": 500.0, "total_pnl": 0.0}}

    def test_no_trades_today(self):
        j = _journal([])
        with patch("src.strategy_advisor.read_state", return_value=self._EMPTY_STATE), \
             patch("src.strategy_advisor._today_records", return_value=[]):
            msg = _hourly_message(j)
        assert "No trades" in msg
        assert "500" in msg

    def test_with_trades_includes_win_rate(self):
        records = [
            _rec(won=True,  pnl=10.0, closed_at="2026-06-30T12:00:00+00:00"),
            _rec(won=False, pnl=-5.0, closed_at="2026-06-30T13:00:00+00:00"),
        ]
        j = _journal(records)
        with patch("src.strategy_advisor.read_state", return_value=self._EMPTY_STATE), \
             patch("src.strategy_advisor._today_records", return_value=records):
            msg = _hourly_message(j)
        assert "50%" in msg
        assert "P&L" in msg

    def test_win_streak_shown(self):
        records = [
            _rec(won=True, closed_at=f"2026-06-30T{h:02d}:00:00+00:00")
            for h in range(3)
        ]
        j = _journal(records)
        with patch("src.strategy_advisor.read_state", return_value=self._EMPTY_STATE), \
             patch("src.strategy_advisor._today_records", return_value=records):
            msg = _hourly_message(j)
        assert "streak" in msg.lower()


# ── _eod_message ─────────────────────────────────────────────────────────────

class TestEodMessage:
    _STATE = {"account": {"total_equity": 520.0, "total_pnl": 20.0}}

    def test_no_trades_today_shows_placeholder(self):
        all_records = [_rec(won=True, pnl=5.0, closed_at="2026-06-29T12:00:00+00:00")]
        j = _journal(all_records)
        with patch("src.strategy_advisor.read_state", return_value=self._STATE), \
             patch("src.strategy_advisor._today_records", return_value=[]):
            msg = _eod_message(j)
        assert "No trades today" in msg

    def test_with_today_trades_shows_stats(self):
        records = [
            _rec(won=True,  pnl=30.0, closed_at="2026-06-30T12:00:00+00:00"),
            _rec(won=False, pnl=-10.0, closed_at="2026-06-30T13:00:00+00:00"),
        ]
        j = _journal(records)
        with patch("src.strategy_advisor.read_state", return_value=self._STATE), \
             patch("src.strategy_advisor._today_records", return_value=records):
            msg = _eod_message(j)
        assert "TODAY" in msg
        assert "50%" in msg
        assert "ALL-TIME" in msg

    def test_includes_recommendations_section(self):
        # 4 losses in a row → strong warning in advice section
        losses = [
            _rec(won=False, closed_at=f"2026-06-30T{h:02d}:00:00+00:00")
            for h in range(4)
        ]
        j = _journal(losses)
        with patch("src.strategy_advisor.read_state", return_value=self._STATE), \
             patch("src.strategy_advisor._today_records", return_value=losses):
            msg = _eod_message(j)
        assert "Recommendations" in msg
