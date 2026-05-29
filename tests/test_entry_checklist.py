"""Unit tests for src/entry_checklist.py

Every trade entry passes through the Checklist gate.  These tests verify
each of the 15 individual predicates in isolation, then verify that the
Checklist aggregator correctly:
  - vetoes on any hard failure
  - computes soft-check scores accurately
  - rejects when score < soft_threshold
  - exposes useful trace strings

Covers:
  Hard checks: min_confidence, circuit_breaker, cooldown, bar_dedup,
               ws_fresh, max_positions, ofi_aligned, sentiment,
               kill_filter, atr_alive, regime_short_block (shorts only)
  Soft checks: rsi_healthy, adx_strong, volume_strong, lead_lag_aligned,
               funding_favorable
  Factories: build_long_checklist, build_short_checklist
  Checklist: hard veto, soft scoring, threshold, trace helpers
"""

import pytest
from dataclasses import dataclass
from typing import Optional

from src.entry_checklist import (
    CheckContext, Checklist, Check, ChecklistResult,
    build_long_checklist, build_short_checklist,
    _min_confidence, _circuit_breaker, _cooldown, _bar_dedup,
    _ws_fresh, _max_positions, _ofi_aligned, _sentiment,
    _kill_filter, _regime_short_block, _rsi_healthy, _adx_strong,
    _volume_strong, _atr_alive, _lead_lag_aligned, _funding_favorable,
    _spread_normal, _vpin_safe, SpreadTracker, SPREAD_MAX_MULT,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

@dataclass
class _Sig:
    """Minimal fake signal object that passes all checks by default."""
    confidence:   float          = 60.0
    ofi:          Optional[float] = 1.0
    ofi_score:    float          = 10.0
    rsi:          float          = 50.0
    adx:          float          = 25.0
    volume_ratio: float          = 1.5
    lead_lag_dir: Optional[str]  = "BUY"
    funding_rate: Optional[float] = 0.0001
    atr:          Optional[float] = 1.0
    close:        Optional[float] = 100.0


def _ctx(
    *,
    side: str = "buy",
    sig: _Sig = None,
    regime_name: str = "TRENDING_UP",
    min_confidence: float = 35.0,
    now_ts: float = 1_000.0,
    bar_ts: float = 900.0,
    last_exit_reason: str = "TP",
    last_exit_time: float = 0.0,
    last_entry_bar_ts: Optional[float] = None,
    cooldown_for=None,
    last_ws_price_time: float = 990.0,
    ws_staleness_sec: float = 30.0,
    open_positions_count: int = 0,
    max_open_positions: int = 3,
    sentiment_allows: bool = True,
    kill_filter_reason: Optional[str] = None,
    circuit_breaker_reason: Optional[str] = None,
) -> CheckContext:
    """Return a CheckContext that passes every check by default."""
    return CheckContext(
        symbol="BTC/USD",
        side=side,
        sig=sig or _Sig(),
        regime_name=regime_name,
        min_confidence=min_confidence,
        now_ts=now_ts,
        bar_ts=bar_ts,
        last_exit_reason=last_exit_reason,
        last_exit_time=last_exit_time,
        last_entry_bar_ts=last_entry_bar_ts,
        cooldown_for=cooldown_for or (lambda _r: 0.0),
        last_ws_price_time=last_ws_price_time,
        ws_staleness_sec=ws_staleness_sec,
        open_positions_count=open_positions_count,
        max_open_positions=max_open_positions,
        sentiment_allows=sentiment_allows,
        kill_filter_reason=kill_filter_reason,
        circuit_breaker_reason=circuit_breaker_reason,
    )


# ── Hard check: min_confidence ────────────────────────────────────────────────

class TestMinConfidence:
    def test_passes_when_confidence_above_min(self):
        ok, _ = _min_confidence(_ctx(sig=_Sig(confidence=50.0), min_confidence=35.0))
        assert ok is True

    def test_passes_when_confidence_equals_min(self):
        ok, _ = _min_confidence(_ctx(sig=_Sig(confidence=35.0), min_confidence=35.0))
        assert ok is True

    def test_fails_when_confidence_below_min(self):
        ok, reason = _min_confidence(_ctx(sig=_Sig(confidence=30.0), min_confidence=35.0))
        assert ok is False
        assert "30" in reason and "35" in reason

    def test_fails_at_one_below_min(self):
        ok, _ = _min_confidence(_ctx(sig=_Sig(confidence=34.9), min_confidence=35.0))
        assert ok is False

    def test_reason_contains_values(self):
        _, reason = _min_confidence(_ctx(sig=_Sig(confidence=20.0), min_confidence=50.0))
        assert "20" in reason and "50" in reason


# ── Hard check: circuit_breaker ───────────────────────────────────────────────

class TestCircuitBreaker:
    def test_passes_when_no_reason(self):
        ok, _ = _circuit_breaker(_ctx(circuit_breaker_reason=None))
        assert ok is True

    def test_fails_when_reason_set(self):
        ok, reason = _circuit_breaker(_ctx(circuit_breaker_reason="2 losses today"))
        assert ok is False
        assert "2 losses today" in reason

    def test_fails_with_empty_string_reason(self):
        # An empty string is falsy — the predicate checks truthiness implicitly
        ok, _ = _circuit_breaker(_ctx(circuit_breaker_reason=""))
        assert ok is True   # empty string → no halt


# ── Hard check: cooldown ──────────────────────────────────────────────────────

class TestCooldown:
    def test_passes_when_no_cooldown(self):
        ok, _ = _cooldown(_ctx(
            cooldown_for=lambda _r: 0.0,
            now_ts=1000.0, last_exit_time=100.0,
        ))
        assert ok is True

    def test_passes_when_cooldown_elapsed(self):
        ok, _ = _cooldown(_ctx(
            cooldown_for=lambda _r: 60.0,
            now_ts=1000.0, last_exit_time=900.0,   # 100s > 60s
        ))
        assert ok is True

    def test_fails_when_in_cooldown(self):
        ok, reason = _cooldown(_ctx(
            cooldown_for=lambda _r: 120.0,
            now_ts=1000.0, last_exit_time=950.0,   # 50s < 120s
            last_exit_reason="SL",
        ))
        assert ok is False
        assert "50" in reason and "120" in reason

    def test_fails_at_boundary(self):
        ok, _ = _cooldown(_ctx(
            cooldown_for=lambda _r: 60.0,
            now_ts=1000.0, last_exit_time=940.001,  # 59.999s < 60s
        ))
        assert ok is False

    def test_cooldown_uses_exit_reason(self):
        recorded = []

        def _cd(r):
            recorded.append(r)
            return 30.0

        _cooldown(_ctx(
            cooldown_for=_cd,
            last_exit_reason="TRAIL_STOP",
            now_ts=1000.0, last_exit_time=0.0,
        ))
        assert recorded == ["TRAIL_STOP"]


# ── Hard check: bar_dedup ─────────────────────────────────────────────────────

class TestBarDedup:
    def test_passes_when_no_previous_bar(self):
        ok, _ = _bar_dedup(_ctx(last_entry_bar_ts=None, bar_ts=900.0))
        assert ok is True

    def test_passes_when_different_bar(self):
        ok, _ = _bar_dedup(_ctx(last_entry_bar_ts=800.0, bar_ts=900.0))
        assert ok is True

    def test_fails_when_same_bar(self):
        ok, reason = _bar_dedup(_ctx(last_entry_bar_ts=900.0, bar_ts=900.0))
        assert ok is False
        assert "bar" in reason.lower()


# ── Hard check: ws_fresh ──────────────────────────────────────────────────────

class TestWsFresh:
    def test_passes_when_no_ws_data_yet(self):
        ok, _ = _ws_fresh(_ctx(last_ws_price_time=0, ws_staleness_sec=30.0))
        assert ok is True

    def test_passes_when_ws_recent(self):
        ok, _ = _ws_fresh(_ctx(now_ts=1000.0, last_ws_price_time=980.0, ws_staleness_sec=30.0))
        assert ok is True

    def test_fails_when_ws_stale(self):
        ok, reason = _ws_fresh(_ctx(now_ts=1000.0, last_ws_price_time=900.0, ws_staleness_sec=30.0))
        assert ok is False
        assert "stale" in reason.lower() or "100" in reason

    def test_passes_at_exact_staleness_boundary(self):
        ok, _ = _ws_fresh(_ctx(now_ts=1000.0, last_ws_price_time=970.0, ws_staleness_sec=30.0))
        assert ok is True    # age==30, threshold==30: NOT > so still fresh

    def test_fails_one_past_boundary(self):
        ok, _ = _ws_fresh(_ctx(now_ts=1000.0, last_ws_price_time=969.9, ws_staleness_sec=30.0))
        assert ok is False   # age=30.1 > 30 → stale


# ── Hard check: max_positions ─────────────────────────────────────────────────

class TestMaxPositions:
    def test_passes_when_below_max(self):
        ok, _ = _max_positions(_ctx(open_positions_count=2, max_open_positions=3))
        assert ok is True

    def test_fails_when_at_max(self):
        ok, reason = _max_positions(_ctx(open_positions_count=3, max_open_positions=3))
        assert ok is False
        assert "3" in reason

    def test_fails_when_over_max(self):
        ok, _ = _max_positions(_ctx(open_positions_count=5, max_open_positions=3))
        assert ok is False

    def test_passes_when_zero_open(self):
        ok, _ = _max_positions(_ctx(open_positions_count=0, max_open_positions=1))
        assert ok is True


# ── Hard check: ofi_aligned ───────────────────────────────────────────────────

class TestOfiAligned:
    def test_passes_when_ofi_is_none(self):
        ok, _ = _ofi_aligned(_ctx(sig=_Sig(ofi=None, ofi_score=0)))
        assert ok is True

    def test_passes_when_ofi_score_positive(self):
        ok, _ = _ofi_aligned(_ctx(sig=_Sig(ofi=0.5, ofi_score=5.0)))
        assert ok is True

    def test_passes_when_ofi_score_zero(self):
        ok, _ = _ofi_aligned(_ctx(sig=_Sig(ofi=0.1, ofi_score=0.0)))
        assert ok is True

    def test_fails_when_ofi_score_negative(self):
        ok, reason = _ofi_aligned(_ctx(sig=_Sig(ofi=-0.5, ofi_score=-10.0)))
        assert ok is False
        assert "ofi" in reason.lower()


# ── Hard check: sentiment ─────────────────────────────────────────────────────

class TestSentiment:
    def test_passes_when_allowed(self):
        ok, _ = _sentiment(_ctx(sentiment_allows=True))
        assert ok is True

    def test_fails_when_blocked(self):
        ok, reason = _sentiment(_ctx(sentiment_allows=False))
        assert ok is False
        assert "fear" in reason.lower() or "block" in reason.lower()


# ── Hard check: kill_filter ───────────────────────────────────────────────────

class TestKillFilter:
    def test_passes_when_no_reason(self):
        ok, _ = _kill_filter(_ctx(kill_filter_reason=None))
        assert ok is True

    def test_fails_when_reason_set(self):
        ok, reason = _kill_filter(_ctx(kill_filter_reason="flash crash detected"))
        assert ok is False
        assert "flash crash" in reason

    def test_empty_string_reason_passes(self):
        ok, _ = _kill_filter(_ctx(kill_filter_reason=""))
        assert ok is True


# ── Hard check: regime_short_block (shorts only) ──────────────────────────────

class TestRegimeShortBlock:
    def test_blocks_shorts_in_trending_up(self):
        ok, reason = _regime_short_block(_ctx(regime_name="TRENDING_UP"))
        assert ok is False
        assert "TRENDING_UP" in reason

    def test_allows_shorts_in_ranging(self):
        ok, _ = _regime_short_block(_ctx(regime_name="RANGING"))
        assert ok is True

    def test_allows_shorts_in_trending_down(self):
        ok, _ = _regime_short_block(_ctx(regime_name="TRENDING_DOWN"))
        assert ok is True

    def test_allows_shorts_in_volatile(self):
        ok, _ = _regime_short_block(_ctx(regime_name="VOLATILE"))
        assert ok is True


# ── Hard check: atr_alive ─────────────────────────────────────────────────────

class TestAtrAlive:
    def test_passes_when_atr_sufficient(self):
        # atr=1.0, close=100 → ratio=0.01 = 1.0% >> 0.05%
        ok, _ = _atr_alive(_ctx(sig=_Sig(atr=1.0, close=100.0)))
        assert ok is True

    def test_fails_when_atr_too_small(self):
        # atr=0.04, close=100 → ratio=0.0004 = 0.04% < 0.05%
        ok, reason = _atr_alive(_ctx(sig=_Sig(atr=0.04, close=100.0)))
        assert ok is False
        assert "atr" in reason.lower()

    def test_passes_at_exact_boundary(self):
        # atr=0.05, close=100 → ratio=0.0005 = 0.05% ≥ 0.05%
        ok, _ = _atr_alive(_ctx(sig=_Sig(atr=0.05, close=100.0)))
        assert ok is True

    def test_passes_when_atr_none(self):
        ok, _ = _atr_alive(_ctx(sig=_Sig(atr=None, close=100.0)))
        assert ok is True

    def test_passes_when_close_none(self):
        ok, _ = _atr_alive(_ctx(sig=_Sig(atr=1.0, close=None)))
        assert ok is True

    def test_passes_when_both_none(self):
        ok, _ = _atr_alive(_ctx(sig=_Sig(atr=None, close=None)))
        assert ok is True


# ── Soft check: rsi_healthy ───────────────────────────────────────────────────

class TestRsiHealthy:
    def test_buy_passes_below_overbought(self):
        ok, _ = _rsi_healthy(_ctx(side="buy", sig=_Sig(rsi=65.0)))
        assert ok is True

    def test_buy_fails_at_overbought(self):
        ok, reason = _rsi_healthy(_ctx(side="buy", sig=_Sig(rsi=70.0)))
        assert ok is False
        assert "overbought" in reason.lower()

    def test_buy_fails_above_overbought(self):
        ok, _ = _rsi_healthy(_ctx(side="buy", sig=_Sig(rsi=85.0)))
        assert ok is False

    def test_sell_passes_above_oversold(self):
        ok, _ = _rsi_healthy(_ctx(side="sell", sig=_Sig(rsi=35.0)))
        assert ok is True

    def test_sell_fails_at_oversold(self):
        ok, reason = _rsi_healthy(_ctx(side="sell", sig=_Sig(rsi=30.0)))
        assert ok is False
        assert "oversold" in reason.lower()

    def test_sell_fails_below_oversold(self):
        ok, _ = _rsi_healthy(_ctx(side="sell", sig=_Sig(rsi=20.0)))
        assert ok is False

    def test_buy_passes_at_rsi_50(self):
        ok, _ = _rsi_healthy(_ctx(side="buy", sig=_Sig(rsi=50.0)))
        assert ok is True

    def test_reason_contains_rsi_value(self):
        _, reason = _rsi_healthy(_ctx(side="buy", sig=_Sig(rsi=75.0)))
        assert "75" in reason


# ── Soft check: adx_strong ───────────────────────────────────────────────────

class TestAdxStrong:
    def test_passes_when_adx_sufficient(self):
        ok, _ = _adx_strong(_ctx(sig=_Sig(adx=20.0), regime_name="TRENDING_UP"))
        assert ok is True

    def test_passes_at_exact_threshold(self):
        ok, _ = _adx_strong(_ctx(sig=_Sig(adx=18.0), regime_name="TRENDING_UP"))
        assert ok is True

    def test_fails_below_threshold(self):
        ok, reason = _adx_strong(_ctx(sig=_Sig(adx=15.0), regime_name="TRENDING_UP"))
        assert ok is False
        assert "18" in reason or "adx" in reason.lower()

    def test_ranging_regime_always_passes(self):
        # In ranging regime ADX check is N/A — always pass
        ok, reason = _adx_strong(_ctx(sig=_Sig(adx=5.0), regime_name="RANGING"))
        assert ok is True
        assert "n/a" in reason.lower() or "ranging" in reason.lower()


# ── Soft check: volume_strong ─────────────────────────────────────────────────

class TestVolumeStrong:
    def test_passes_when_volume_high(self):
        ok, _ = _volume_strong(_ctx(sig=_Sig(volume_ratio=2.0)))
        assert ok is True

    def test_passes_at_exact_1x(self):
        ok, _ = _volume_strong(_ctx(sig=_Sig(volume_ratio=1.0)))
        assert ok is True

    def test_fails_below_1x(self):
        ok, reason = _volume_strong(_ctx(sig=_Sig(volume_ratio=0.8)))
        assert ok is False
        assert "0.80" in reason or "vol" in reason.lower()

    def test_none_volume_defaults_to_one(self):
        sig = _Sig(volume_ratio=None)
        ok, _ = _volume_strong(_ctx(sig=sig))
        assert ok is True  # None coerced to 1.0 → passes


# ── Soft check: lead_lag_aligned ─────────────────────────────────────────────

class TestLeadLagAligned:
    def test_passes_when_no_lead_lag(self):
        ok, _ = _lead_lag_aligned(_ctx(side="buy", sig=_Sig(lead_lag_dir=None)))
        assert ok is True

    def test_passes_when_aligned_for_buy(self):
        ok, _ = _lead_lag_aligned(_ctx(side="buy", sig=_Sig(lead_lag_dir="BUY")))
        assert ok is True

    def test_passes_when_aligned_for_sell(self):
        ok, _ = _lead_lag_aligned(_ctx(side="sell", sig=_Sig(lead_lag_dir="SELL")))
        assert ok is True

    def test_fails_when_opposing_buy(self):
        ok, reason = _lead_lag_aligned(_ctx(side="buy", sig=_Sig(lead_lag_dir="SELL")))
        assert ok is False
        assert "SELL" in reason

    def test_fails_when_opposing_sell(self):
        ok, reason = _lead_lag_aligned(_ctx(side="sell", sig=_Sig(lead_lag_dir="BUY")))
        assert ok is False
        assert "BUY" in reason


# ── Soft check: funding_favorable ─────────────────────────────────────────────

class TestFundingFavorable:
    def test_passes_when_no_funding(self):
        ok, _ = _funding_favorable(_ctx(side="buy", sig=_Sig(funding_rate=None)))
        assert ok is True

    def test_buy_passes_when_funding_low(self):
        ok, _ = _funding_favorable(_ctx(side="buy", sig=_Sig(funding_rate=0.0004)))
        assert ok is True

    def test_buy_fails_when_funding_high(self):
        ok, reason = _funding_favorable(_ctx(side="buy", sig=_Sig(funding_rate=0.001)))
        assert ok is False
        assert "funding" in reason.lower()

    def test_buy_passes_negative_funding(self):
        ok, _ = _funding_favorable(_ctx(side="buy", sig=_Sig(funding_rate=-0.001)))
        assert ok is True   # negative funding favors longs

    def test_sell_passes_when_funding_moderate_negative(self):
        ok, _ = _funding_favorable(_ctx(side="sell", sig=_Sig(funding_rate=-0.0004)))
        assert ok is True

    def test_sell_fails_when_funding_very_negative(self):
        ok, reason = _funding_favorable(_ctx(side="sell", sig=_Sig(funding_rate=-0.001)))
        assert ok is False
        assert "funding" in reason.lower()


# ── Checklist: aggregator behaviour ──────────────────────────────────────────

class TestChecklistHardVeto:
    """Any single hard check failure must veto the entry, regardless of soft scores."""

    def _passing_ctx(self) -> CheckContext:
        return _ctx()

    def test_all_pass_returns_passed_true(self):
        cl = build_long_checklist()
        result = cl.run(self._passing_ctx())
        assert result.passed is True

    def test_hard_fail_kills_entry(self):
        cl = build_long_checklist()
        ctx = _ctx(circuit_breaker_reason="halted")
        result = cl.run(ctx)
        assert result.passed is False
        assert "circuit_breaker" in result.failed_hard

    def test_hard_fail_recorded_in_failed_hard(self):
        cl = build_long_checklist()
        ctx = _ctx(kill_filter_reason="flash crash")
        result = cl.run(ctx)
        assert "kill_filter" in result.failed_hard

    def test_multiple_hard_fails_all_recorded(self):
        cl = build_long_checklist()
        ctx = _ctx(
            circuit_breaker_reason="halted",
            kill_filter_reason="flash crash",
            open_positions_count=5,
            max_open_positions=3,
        )
        result = cl.run(ctx)
        assert "circuit_breaker" in result.failed_hard
        assert "kill_filter" in result.failed_hard
        assert "max_positions" in result.failed_hard

    def test_hard_fail_with_all_soft_passing_still_fails(self):
        cl = build_long_checklist()
        ctx = _ctx(sentiment_allows=False)  # hard fail
        result = cl.run(ctx)
        assert result.passed is False


class TestChecklistSoftScore:
    """Soft-check scoring and threshold logic."""

    def test_score_is_1_when_all_soft_pass(self):
        cl = build_long_checklist()
        result = cl.run(_ctx())
        assert result.score == pytest.approx(1.0)

    def test_score_below_threshold_fails(self):
        # Force all soft checks to fail by pushing RSI overbought,
        # ADX low, volume low, lead-lag opposing, funding unfavorable
        sig = _Sig(
            rsi=80.0,             # overbought (buy) → soft fail
            adx=10.0,             # below 18 → soft fail
            volume_ratio=0.5,     # below 1.0 → soft fail
            lead_lag_dir="SELL",  # opposes buy → soft fail
            funding_rate=0.002,   # too high for long → soft fail
        )
        cl = build_long_checklist(soft_threshold=0.4)
        result = cl.run(_ctx(sig=sig))
        assert result.score < 0.4
        assert result.passed is False

    def test_score_is_weighted_correctly(self):
        """Two soft checks pass, rest fail; verify weighted calculation."""
        # long checklist soft checks and weights:
        #   rsi_healthy:       2.0
        #   adx_strong:        2.0
        #   volume_strong:     1.0
        #   lead_lag_aligned:  2.0
        #   funding_favorable: 1.0
        # total weight = 8.0
        # If only rsi (2) and volume (1) pass → hit = 3 → score = 3/8 = 0.375
        sig = _Sig(
            rsi=50.0,             # buy, rsi<70 → PASS (weight 2)
            adx=10.0,             # FAIL (weight 2)
            volume_ratio=1.5,     # PASS (weight 1)
            lead_lag_dir="SELL",  # FAIL for buy (weight 2)
            funding_rate=0.002,   # FAIL for buy (weight 1)
        )
        cl = build_long_checklist()
        result = cl.run(_ctx(sig=sig))
        assert result.score == pytest.approx(3.0 / 8.0, rel=1e-6)

    def test_soft_misses_listed_correctly(self):
        sig = _Sig(adx=5.0, lead_lag_dir="SELL")
        cl = build_long_checklist()
        result = cl.run(_ctx(sig=sig))
        assert "adx_strong" in result.soft_misses
        assert "lead_lag_aligned" in result.soft_misses
        assert "rsi_healthy" not in result.soft_misses

    def test_no_soft_checks_score_is_1(self):
        # Checklist with no soft checks should score 1.0 (zero-weight guard)
        cl = Checklist([], soft_threshold=0.5)
        result = cl.run(_ctx())
        assert result.score == pytest.approx(1.0)
        assert result.passed is True


class TestChecklistTrace:
    """Trace string helpers for diagnostics and Telegram reporting."""

    def test_trace_contains_all_check_names(self):
        cl = build_long_checklist()
        result = cl.run(_ctx())
        trace = result.trace()
        for name in ["min_confidence", "circuit_breaker", "cooldown",
                     "rsi_healthy", "adx_strong"]:
            assert name in trace

    def test_trace_shows_pass_fail(self):
        cl = build_long_checklist()
        ctx = _ctx(circuit_breaker_reason="halted")
        result = cl.run(ctx)
        assert "FAIL circuit_breaker" in result.trace()
        assert "PASS min_confidence" in result.trace()

    def test_short_trace_uses_plus_minus(self):
        cl = build_long_checklist()
        ctx = _ctx(circuit_breaker_reason="halted")
        result = cl.run(ctx)
        st = result.short_trace()
        assert "-circuit_breaker" in st
        assert "+min_confidence" in st

    def test_reason_summary_names_first_hard_fail(self):
        cl = build_long_checklist()
        ctx = _ctx(circuit_breaker_reason="halted")
        result = cl.run(ctx)
        summary = result.reason_summary()
        assert "circuit_breaker" in summary

    def test_reason_summary_mentions_score_when_only_soft_fails(self):
        sig = _Sig(rsi=80.0, adx=5.0, lead_lag_dir="SELL",
                   volume_ratio=0.3, funding_rate=0.005)
        cl = build_long_checklist()
        result = cl.run(_ctx(sig=sig))
        if not result.failed_hard:
            summary = result.reason_summary()
            assert "score" in summary.lower() or "soft" in summary.lower()

    def test_reason_summary_when_all_pass(self):
        cl = build_long_checklist()
        result = cl.run(_ctx())
        assert result.passed
        summary = result.reason_summary()
        assert "score" in summary.lower()


# ── Factory: build_long_checklist ────────────────────────────────────────────

class TestBuildLongChecklist:
    def test_has_sentiment_check(self):
        cl = build_long_checklist()
        names = [c.name for c in cl.checks]
        assert "sentiment" in names

    def test_does_not_have_regime_short_block(self):
        cl = build_long_checklist()
        names = [c.name for c in cl.checks]
        assert "regime_short_block" not in names

    def test_soft_threshold_default_is_sensible(self):
        cl = build_long_checklist()
        assert 0.0 < cl.soft_threshold <= 1.0

    def test_custom_threshold_respected(self):
        cl = build_long_checklist(soft_threshold=0.9)
        assert cl.soft_threshold == pytest.approx(0.9)

    def test_all_hard_checks_are_hard(self):
        cl = build_long_checklist()
        hard_names = {"min_confidence", "circuit_breaker", "cooldown", "bar_dedup",
                      "ws_fresh", "max_positions", "ofi_aligned", "sentiment",
                      "kill_filter", "atr_alive", "spread_normal", "vpin_safe"}
        for c in cl.checks:
            if c.name in hard_names:
                assert c.kind == "hard", f"{c.name} should be hard"

    def test_soft_checks_are_soft(self):
        cl = build_long_checklist()
        soft_names = {"rsi_healthy", "adx_strong", "volume_strong",
                      "lead_lag_aligned", "funding_favorable"}
        for c in cl.checks:
            if c.name in soft_names:
                assert c.kind == "soft", f"{c.name} should be soft"


# ── Factory: build_short_checklist ────────────────────────────────────────────

class TestBuildShortChecklist:
    def test_has_regime_short_block(self):
        cl = build_short_checklist()
        names = [c.name for c in cl.checks]
        assert "regime_short_block" in names

    def test_regime_short_block_is_hard(self):
        cl = build_short_checklist()
        check = next(c for c in cl.checks if c.name == "regime_short_block")
        assert check.kind == "hard"

    def test_does_not_have_sentiment_check(self):
        cl = build_short_checklist()
        names = [c.name for c in cl.checks]
        assert "sentiment" not in names

    def test_blocks_trending_up_short(self):
        cl = build_short_checklist()
        ctx = _ctx(side="sell", regime_name="TRENDING_UP")
        result = cl.run(ctx)
        assert result.passed is False
        assert "regime_short_block" in result.failed_hard

    def test_allows_short_in_ranging(self):
        cl = build_short_checklist()
        sig = _Sig(lead_lag_dir="SELL")
        ctx = _ctx(side="sell", regime_name="RANGING", sig=sig)
        result = cl.run(ctx)
        assert result.passed is True

    def test_custom_threshold_respected(self):
        cl = build_short_checklist(soft_threshold=0.75)
        assert cl.soft_threshold == pytest.approx(0.75)


# ── Checklist: check exception isolation ─────────────────────────────────────

class TestCheckExceptionIsolation:
    """A buggy check must not crash the whole Checklist.run() — it records a failure."""

    def test_buggy_check_caught_and_marked_failed(self):
        def _exploding(ctx):
            raise RuntimeError("intentional boom")

        cl = Checklist([
            Check("boom", "hard", _exploding),
        ])
        result = cl.run(_ctx())
        assert result.passed is False
        assert "boom" in result.failed_hard
        boom_result = next(r for r in result.results if r.name == "boom")
        assert "check error" in boom_result.reason

    def test_other_checks_still_run_after_buggy_check(self):
        def _exploding(ctx):
            raise ValueError("boom")

        def _always_pass(ctx):
            return True, "ok"

        cl = Checklist([
            Check("boom", "hard", _exploding),
            Check("ok_check", "soft", _always_pass, weight=1.0),
        ])
        result = cl.run(_ctx())
        names = [r.name for r in result.results]
        assert "boom" in names
        assert "ok_check" in names


# ── Hard check: spread_normal ─────────────────────────────────────────────────

def _ctx_spread(
    current_spread_pct: Optional[float],
    median_spread_pct: Optional[float],
) -> CheckContext:
    """Build a CheckContext with spread fields set; all other fields pass by default."""
    base = _ctx()
    base.current_spread_pct = current_spread_pct
    base.median_spread_pct = median_spread_pct
    return base


class TestSpreadNormal:
    def test_passes_when_both_none(self):
        ok, reason = _spread_normal(_ctx_spread(None, None))
        assert ok is True
        assert "baseline" in reason.lower()

    def test_passes_when_current_none(self):
        ok, _ = _spread_normal(_ctx_spread(None, 0.0005))
        assert ok is True

    def test_passes_when_median_none(self):
        ok, _ = _spread_normal(_ctx_spread(0.001, None))
        assert ok is True

    def test_passes_when_median_zero(self):
        ok, _ = _spread_normal(_ctx_spread(0.001, 0.0))
        assert ok is True

    def test_passes_when_spread_at_exactly_max_mult(self):
        # current = SPREAD_MAX_MULT × median → mult == SPREAD_MAX_MULT → NOT > → passes
        median = 0.001
        current = median * SPREAD_MAX_MULT
        ok, _ = _spread_normal(_ctx_spread(current, median))
        assert ok is True

    def test_passes_when_spread_well_within_normal(self):
        ok, reason = _spread_normal(_ctx_spread(0.0008, 0.001))
        assert ok is True
        assert "spread" in reason.lower()

    def test_fails_when_spread_just_above_max_mult(self):
        median = 0.001
        current = median * (SPREAD_MAX_MULT + 0.001)
        ok, reason = _spread_normal(_ctx_spread(current, median))
        assert ok is False
        assert "spread" in reason.lower()

    def test_fails_when_spread_massively_wide(self):
        ok, reason = _spread_normal(_ctx_spread(0.05, 0.001))
        assert ok is False
        assert "×" in reason or "median" in reason.lower()

    def test_reason_contains_spread_percentage(self):
        _, reason = _spread_normal(_ctx_spread(0.002, 0.001))
        assert "%" in reason

    def test_reason_contains_multiplier(self):
        # 0.003 / 0.001 = 3.0× → above SPREAD_MAX_MULT (1.5)
        _, reason = _spread_normal(_ctx_spread(0.003, 0.001))
        assert "×" in reason or "x" in reason.lower()

    def test_passes_when_spread_slightly_below_mult(self):
        median = 0.001
        current = median * (SPREAD_MAX_MULT - 0.01)
        ok, _ = _spread_normal(_ctx_spread(current, median))
        assert ok is True

    def test_long_checklist_includes_spread_normal_as_hard(self):
        cl = build_long_checklist()
        names_and_kinds = {c.name: c.kind for c in cl.checks}
        assert "spread_normal" in names_and_kinds
        assert names_and_kinds["spread_normal"] == "hard"

    def test_short_checklist_includes_spread_normal_as_hard(self):
        cl = build_short_checklist()
        names_and_kinds = {c.name: c.kind for c in cl.checks}
        assert "spread_normal" in names_and_kinds
        assert names_and_kinds["spread_normal"] == "hard"

    def test_wide_spread_vetoes_full_long_checklist(self):
        cl = build_long_checklist()
        ctx = _ctx()
        ctx.current_spread_pct = 0.01    # 1% current
        ctx.median_spread_pct  = 0.001   # 0.1% median → 10× — way above 1.5×
        result = cl.run(ctx)
        assert result.passed is False
        assert "spread_normal" in result.failed_hard

    def test_normal_spread_does_not_veto_long_checklist(self):
        cl = build_long_checklist()
        ctx = _ctx()
        ctx.current_spread_pct = 0.001
        ctx.median_spread_pct  = 0.001   # 1.0× — within limit
        result = cl.run(ctx)
        assert "spread_normal" not in result.failed_hard


# ── Hard check: vpin_safe ─────────────────────────────────────────────────────

def _ctx_vpin(
    vpin: Optional[float],
    vpin_threshold: float = 0.55,
) -> CheckContext:
    """Build a CheckContext with VPIN fields set; everything else passes."""
    base = _ctx()
    base.vpin = vpin
    base.vpin_threshold = vpin_threshold
    return base


class TestVpinSafe:
    def test_passes_when_vpin_none(self):
        ok, reason = _vpin_safe(_ctx_vpin(None))
        assert ok is True
        assert "no vpin" in reason.lower()

    def test_passes_when_vpin_well_below_threshold(self):
        ok, reason = _vpin_safe(_ctx_vpin(0.3, vpin_threshold=0.55))
        assert ok is True
        assert "vpin" in reason.lower()

    def test_passes_when_vpin_at_exactly_threshold(self):
        # vpin == threshold → NOT > → passes
        ok, _ = _vpin_safe(_ctx_vpin(0.55, vpin_threshold=0.55))
        assert ok is True

    def test_fails_when_vpin_just_above_threshold(self):
        ok, reason = _vpin_safe(_ctx_vpin(0.551, vpin_threshold=0.55))
        assert ok is False
        assert "toxic" in reason.lower()

    def test_fails_when_vpin_clearly_toxic(self):
        ok, reason = _vpin_safe(_ctx_vpin(0.9, vpin_threshold=0.55))
        assert ok is False
        assert "0.90" in reason or "0.9" in reason

    def test_reason_contains_vpin_value_when_passing(self):
        _, reason = _vpin_safe(_ctx_vpin(0.4, vpin_threshold=0.55))
        assert "0.40" in reason or "0.4" in reason

    def test_reason_contains_threshold_when_failing(self):
        _, reason = _vpin_safe(_ctx_vpin(0.7, vpin_threshold=0.55))
        assert "0.55" in reason

    def test_custom_threshold_respected(self):
        ok, _ = _vpin_safe(_ctx_vpin(0.6, vpin_threshold=0.70))
        assert ok is True  # 0.6 <= 0.70

    def test_custom_threshold_blocks_at_higher_value(self):
        ok, _ = _vpin_safe(_ctx_vpin(0.75, vpin_threshold=0.70))
        assert ok is False

    def test_long_checklist_includes_vpin_safe_as_hard(self):
        cl = build_long_checklist()
        names_and_kinds = {c.name: c.kind for c in cl.checks}
        assert "vpin_safe" in names_and_kinds
        assert names_and_kinds["vpin_safe"] == "hard"

    def test_short_checklist_includes_vpin_safe_as_hard(self):
        cl = build_short_checklist()
        names_and_kinds = {c.name: c.kind for c in cl.checks}
        assert "vpin_safe" in names_and_kinds
        assert names_and_kinds["vpin_safe"] == "hard"

    def test_toxic_vpin_vetoes_full_long_checklist(self):
        cl = build_long_checklist()
        ctx = _ctx()
        ctx.vpin = 0.9
        ctx.vpin_threshold = 0.55
        result = cl.run(ctx)
        assert result.passed is False
        assert "vpin_safe" in result.failed_hard

    def test_none_vpin_does_not_veto_long_checklist(self):
        cl = build_long_checklist()
        ctx = _ctx()
        ctx.vpin = None
        result = cl.run(ctx)
        assert "vpin_safe" not in result.failed_hard


# ── SpreadTracker ─────────────────────────────────────────────────────────────

class TestSpreadTracker:
    def test_empty_tracker_current_returns_none(self):
        t = SpreadTracker()
        assert t.current("BTC/USD") is None

    def test_empty_tracker_median_returns_none(self):
        t = SpreadTracker()
        assert t.median("BTC/USD") is None

    def test_push_and_current(self):
        t = SpreadTracker()
        t.push("BTC/USD", 0.001)
        assert t.current("BTC/USD") == pytest.approx(0.001)

    def test_current_returns_last_pushed(self):
        t = SpreadTracker()
        t.push("BTC/USD", 0.001)
        t.push("BTC/USD", 0.002)
        assert t.current("BTC/USD") == pytest.approx(0.002)

    def test_symbols_are_independent(self):
        t = SpreadTracker()
        t.push("BTC/USD", 0.001)
        t.push("ETH/USD", 0.005)
        assert t.current("BTC/USD") == pytest.approx(0.001)
        assert t.current("ETH/USD") == pytest.approx(0.005)

    def test_median_requires_min_samples(self):
        t = SpreadTracker(maxlen=60)
        for i in range(9):   # push 9 — below default min_samples=10
            t.push("BTC/USD", 0.001)
        assert t.median("BTC/USD") is None

    def test_median_available_at_min_samples(self):
        t = SpreadTracker(maxlen=60)
        for _ in range(10):
            t.push("BTC/USD", 0.001)
        assert t.median("BTC/USD") == pytest.approx(0.001)

    def test_median_is_correct_for_known_values(self):
        t = SpreadTracker(maxlen=60)
        values = [0.001, 0.002, 0.003, 0.004, 0.005,
                  0.006, 0.007, 0.008, 0.009, 0.010]
        for v in values:
            t.push("BTC/USD", v)
        # median of 10 values: average of 5th and 6th = (0.005+0.006)/2 = 0.0055
        assert t.median("BTC/USD") == pytest.approx(0.0055)

    def test_maxlen_evicts_oldest(self):
        t = SpreadTracker(maxlen=5)
        for _ in range(5):
            t.push("BTC/USD", 0.001)
        t.push("BTC/USD", 0.999)   # evicts oldest 0.001
        assert t.current("BTC/USD") == pytest.approx(0.999)

    def test_push_ignores_none(self):
        t = SpreadTracker()
        t.push("BTC/USD", None)
        assert t.current("BTC/USD") is None

    def test_push_ignores_zero(self):
        t = SpreadTracker()
        t.push("BTC/USD", 0.0)
        assert t.current("BTC/USD") is None

    def test_push_ignores_negative(self):
        t = SpreadTracker()
        t.push("BTC/USD", -0.001)
        assert t.current("BTC/USD") is None

    def test_custom_min_samples(self):
        t = SpreadTracker()
        t.push("BTC/USD", 0.001)
        t.push("BTC/USD", 0.002)
        # min_samples=2 → median available after 2 pushes
        assert t.median("BTC/USD", min_samples=2) is not None

    def test_median_unknown_symbol_returns_none(self):
        t = SpreadTracker()
        assert t.median("XRP/USD") is None

    def test_current_unknown_symbol_returns_none(self):
        t = SpreadTracker()
        assert t.current("XRP/USD") is None
