"""
Pre-trade entry checklist.

Consolidates the scattered if/continue veto checks in paper_trading.py into a
single composable Checklist with two flavors of rules:

  hard checks → any failure vetoes the entry (regime block, kill filter, etc.)
  soft checks → contribute to a 0..1 quality score; setup must clear a threshold

The Checklist returns a ChecklistResult with:
  - passed: bool                  ← gate verdict
  - score: float                  ← soft-check weighted score
  - failed_hard / soft_misses     ← human-readable diagnostics
  - results: list[CheckResult]    ← full trace, one per rule

Per-symbol audit and Telegram analysis can quote `result.trace()` verbatim so
the user can see *exactly* which rule killed a setup (or which weak rule a
losing trade had).
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Any, Callable, Deque, Dict, List, Literal, Optional

CheckKind = Literal["hard", "soft"]


@dataclass
class CheckContext:
    """Snapshot of everything a check might inspect. The caller fills this
    once per evaluation tick — checks read only, never mutate."""
    symbol: str
    side: Literal["buy", "sell"]
    sig: Any                                # ScientificSignal
    regime_name: str
    min_confidence: float
    now_ts: float
    bar_ts: float
    last_exit_reason: str
    last_exit_time: float
    last_entry_bar_ts: Optional[float]
    cooldown_for: Callable[[str], float]
    last_ws_price_time: float
    ws_staleness_sec: float
    open_positions_count: int
    max_open_positions: int
    sentiment_allows: bool                  # already evaluated by caller
    kill_filter_reason: Optional[str]       # output of _kill_filter_skip
    circuit_breaker_reason: Optional[str]   # None if can_enter
    # Spread anomaly guard: caller passes current spread (as a fraction of price,
    # e.g. 0.0008 = 8bp) and the rolling median over the last ~N samples. The
    # _spread_normal check rejects when current is more than SPREAD_MAX_MULT ×
    # median, which catches news spikes, whale events, and stop hunts. Both
    # fields are optional so older call sites (and tests) still work.
    current_spread_pct: Optional[float] = None
    median_spread_pct:  Optional[float] = None


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    kind: CheckKind
    weight: float


@dataclass
class ChecklistResult:
    passed: bool
    score: float
    failed_hard: List[str]
    soft_misses: List[str]
    results: List[CheckResult]

    def trace(self) -> str:
        return " | ".join(
            f"{'PASS' if r.passed else 'FAIL'} {r.name}({r.reason})"
            for r in self.results
        )

    def short_trace(self) -> str:
        return " ".join(
            f"{'+' if r.passed else '-'}{r.name}" for r in self.results
        )

    def reason_summary(self) -> str:
        if self.failed_hard:
            first = next(r for r in self.results if r.name == self.failed_hard[0])
            return f"{first.name}: {first.reason}"
        if self.soft_misses:
            return f"soft score {self.score:.2f} — missed: {', '.join(self.soft_misses)}"
        return f"score={self.score:.2f}"


@dataclass
class Check:
    name: str
    kind: CheckKind
    fn: Callable[[CheckContext], "tuple[bool, str]"]
    weight: float = 1.0


class Checklist:
    """Runs an ordered list of Check objects against a CheckContext.

    `soft_threshold` is the minimum weighted score required when no hard
    veto fires. Defaults to 0.6 — i.e. setups must clear 60% of soft weight.
    """

    def __init__(self, checks: List[Check], *, soft_threshold: float = 0.4):
        self.checks = checks
        self.soft_threshold = soft_threshold

    def run(self, ctx: CheckContext) -> ChecklistResult:
        results: List[CheckResult] = []
        failed_hard: List[str] = []
        soft_misses: List[str] = []
        weight_total = weight_hit = 0.0

        for c in self.checks:
            try:
                ok, reason = c.fn(ctx)
            except Exception as exc:
                ok, reason = False, f"check error: {exc!r}"
            results.append(CheckResult(c.name, ok, reason, c.kind, c.weight))
            if c.kind == "hard":
                if not ok:
                    failed_hard.append(c.name)
            else:
                weight_total += c.weight
                if ok:
                    weight_hit += c.weight
                else:
                    soft_misses.append(c.name)

        score = (weight_hit / weight_total) if weight_total > 0 else 1.0
        passed = (not failed_hard) and (score >= self.soft_threshold)
        return ChecklistResult(
            passed=passed,
            score=score,
            failed_hard=failed_hard,
            soft_misses=soft_misses,
            results=results,
        )


# ── Hard predicates ─────────────────────────────────────────────────────────

def _min_confidence(ctx: CheckContext):
    if ctx.sig.confidence < ctx.min_confidence:
        return False, f"conf={ctx.sig.confidence:.0f}<{ctx.min_confidence:.0f}"
    return True, f"conf={ctx.sig.confidence:.0f}"


def _circuit_breaker(ctx: CheckContext):
    if ctx.circuit_breaker_reason:
        return False, ctx.circuit_breaker_reason
    return True, "ok"


def _cooldown(ctx: CheckContext):
    cd = ctx.cooldown_for(ctx.last_exit_reason or "")
    since = ctx.now_ts - (ctx.last_exit_time or 0)
    if since < cd:
        return False, f"{since:.0f}s<{cd:.0f}s after {ctx.last_exit_reason}"
    return True, f"clear ({since:.0f}s)"


def _bar_dedup(ctx: CheckContext):
    if ctx.last_entry_bar_ts == ctx.bar_ts:
        return False, "already evaluated this bar"
    return True, "fresh bar"


def _ws_fresh(ctx: CheckContext):
    if ctx.last_ws_price_time <= 0:
        return True, "no ws yet"
    age = ctx.now_ts - ctx.last_ws_price_time
    if age > ctx.ws_staleness_sec:
        return False, f"ws stale ({age:.0f}s)"
    return True, f"ws {age:.0f}s"


def _max_positions(ctx: CheckContext):
    if ctx.open_positions_count >= ctx.max_open_positions:
        return False, f"at max ({ctx.open_positions_count}/{ctx.max_open_positions})"
    return True, f"{ctx.open_positions_count}/{ctx.max_open_positions}"


def _ofi_aligned(ctx: CheckContext):
    if ctx.sig.ofi is None:
        return True, "no ofi"
    if ctx.sig.ofi_score < 0:
        return False, f"ofi={ctx.sig.ofi:.2f} opposes (score={ctx.sig.ofi_score:.0f})"
    return True, f"ofi={ctx.sig.ofi:.2f}"


def _sentiment(ctx: CheckContext):
    if ctx.sentiment_allows:
        return True, "ok"
    return False, "F&G extreme fear blocks longs"


def _kill_filter(ctx: CheckContext):
    if ctx.kill_filter_reason:
        return False, ctx.kill_filter_reason
    return True, "ok"


def _regime_short_block(ctx: CheckContext):
    if ctx.regime_name == "TRENDING_UP":
        return False, "TRENDING_UP blocks shorts"
    return True, ctx.regime_name


# Multiplier above the rolling-median spread at which entries get vetoed.
# 1.5× is the textbook setting from the algo-spread-monitor literature;
# tightening to 1.3× or 1.2× refuses more trades during turbulent tape.
SPREAD_MAX_MULT = 1.5


def _spread_normal(ctx: CheckContext):
    """Hard veto: refuse to enter when the current spread is anomalously wide
    relative to its rolling median. Wide spreads typically mean (a) a news
    spike just hit, (b) a whale is sweeping levels, or (c) a stop run is in
    progress — all of which adversely select market-takers. Passes silently
    when no baseline data is available (warm-up window)."""
    cur = ctx.current_spread_pct
    med = ctx.median_spread_pct
    if cur is None or med is None or med <= 0:
        return True, "no baseline"
    mult = cur / med
    if mult > SPREAD_MAX_MULT:
        return False, f"spread {cur*100:.3f}% = {mult:.1f}× median {med*100:.3f}%"
    return True, f"spread {cur*100:.3f}% ({mult:.2f}× median)"


# ── SpreadTracker — caller side, populated each tick from live order books ──

class SpreadTracker:
    """Tiny per-symbol rolling buffer of spread-as-fraction-of-price.

    Used by the paper_trading main loop: on every tick that observes a fresh
    bid/ask, call tracker.push(symbol, spread/price). At checklist time,
    tracker.median(symbol) feeds CheckContext.median_spread_pct.

    Asyncio-single-threaded — no locks. maxlen of 60 ≈ last ~2 minutes when the
    loop ticks every ~2s; long enough to be robust, short enough to react when
    the tape regime changes.
    """

    def __init__(self, maxlen: int = 60):
        self.maxlen = maxlen
        self._buf: Dict[str, Deque[float]] = {}

    def push(self, symbol: str, spread_pct: float) -> None:
        if spread_pct is None or spread_pct <= 0:
            return
        buf = self._buf.get(symbol)
        if buf is None:
            buf = deque(maxlen=self.maxlen)
            self._buf[symbol] = buf
        buf.append(float(spread_pct))

    def current(self, symbol: str) -> Optional[float]:
        buf = self._buf.get(symbol)
        if not buf:
            return None
        return buf[-1]

    def median(self, symbol: str, min_samples: int = 10) -> Optional[float]:
        buf = self._buf.get(symbol)
        if not buf or len(buf) < min_samples:
            return None
        return median(buf)


# ── Soft predicates ─────────────────────────────────────────────────────────

def _rsi_healthy(ctx: CheckContext):
    rsi = ctx.sig.rsi
    if ctx.side == "buy":
        if rsi < 70:
            return True, f"rsi={rsi:.0f}"
        return False, f"rsi={rsi:.0f} overbought"
    if rsi > 30:
        return True, f"rsi={rsi:.0f}"
    return False, f"rsi={rsi:.0f} oversold"


def _adx_strong(ctx: CheckContext):
    if ctx.regime_name == "RANGING":
        return True, "ranging — n/a"
    if ctx.sig.adx >= 18:
        return True, f"adx={ctx.sig.adx:.0f}"
    return False, f"adx={ctx.sig.adx:.0f}<18"


def _volume_strong(ctx: CheckContext):
    vr = getattr(ctx.sig, "volume_ratio", 1.0) or 1.0
    if vr >= 1.0:
        return True, f"vol={vr:.2f}x"
    return False, f"vol={vr:.2f}x<1.0"


def _atr_alive(ctx: CheckContext):
    """Reject dead-volatility setups. AGGRESSIVE: ATR / price must be ≥ 0.05%
    (was 0.08%). Most 1m crypto bars clear this; only the absolute dead-tape
    setups get vetoed."""
    atr = getattr(ctx.sig, "atr", None)
    px  = getattr(ctx.sig, "close", None)
    if not atr or not px:
        return True, "no atr/price"
    ratio = atr / px
    if ratio >= 0.0005:
        return True, f"atr/px={ratio*100:.3f}%"
    return False, f"atr/px={ratio*100:.3f}%<0.05%"


def _lead_lag_aligned(ctx: CheckContext):
    if ctx.sig.lead_lag_dir is None:
        return True, "no lead-lag"
    want = "BUY" if ctx.side == "buy" else "SELL"
    if ctx.sig.lead_lag_dir == want:
        return True, f"lead={ctx.sig.lead_lag_dir}"
    return False, f"lead={ctx.sig.lead_lag_dir} opposes"


def _funding_favorable(ctx: CheckContext):
    fr = getattr(ctx.sig, "funding_rate", None)
    if fr is None:
        return True, "no funding"
    if ctx.side == "buy" and fr < 0.0005:
        return True, f"funding={fr:.4f}"
    if ctx.side == "sell" and fr > -0.0005:
        return True, f"funding={fr:.4f}"
    return False, f"funding={fr:.4f} unfavorable"


# ── Factories ───────────────────────────────────────────────────────────────

def build_long_checklist(*, soft_threshold: float = 0.4) -> Checklist:
    return Checklist([
        Check("min_confidence",    "hard", _min_confidence),
        Check("circuit_breaker",   "hard", _circuit_breaker),
        Check("cooldown",          "hard", _cooldown),
        Check("bar_dedup",         "hard", _bar_dedup),
        Check("ws_fresh",          "hard", _ws_fresh),
        Check("max_positions",    "hard", _max_positions),
        Check("ofi_aligned",       "hard", _ofi_aligned),
        Check("sentiment",         "hard", _sentiment),
        Check("kill_filter",       "hard", _kill_filter),
        Check("atr_alive",         "hard", _atr_alive),
        Check("spread_normal",     "hard", _spread_normal),
        Check("rsi_healthy",       "soft", _rsi_healthy,       weight=2.0),
        Check("adx_strong",        "soft", _adx_strong,        weight=2.0),
        Check("volume_strong",     "soft", _volume_strong,     weight=1.0),
        Check("lead_lag_aligned",  "soft", _lead_lag_aligned,  weight=2.0),
        Check("funding_favorable", "soft", _funding_favorable, weight=1.0),
    ], soft_threshold=soft_threshold)


def build_short_checklist(*, soft_threshold: float = 0.4) -> Checklist:
    return Checklist([
        Check("min_confidence",     "hard", _min_confidence),
        Check("circuit_breaker",    "hard", _circuit_breaker),
        Check("cooldown",           "hard", _cooldown),
        Check("bar_dedup",          "hard", _bar_dedup),
        Check("ws_fresh",           "hard", _ws_fresh),
        Check("max_positions",     "hard", _max_positions),
        Check("ofi_aligned",        "hard", _ofi_aligned),
        Check("regime_short_block", "hard", _regime_short_block),
        Check("kill_filter",        "hard", _kill_filter),
        Check("atr_alive",          "hard", _atr_alive),
        Check("spread_normal",      "hard", _spread_normal),
        Check("rsi_healthy",        "soft", _rsi_healthy,       weight=2.0),
        Check("adx_strong",         "soft", _adx_strong,        weight=2.0),
        Check("volume_strong",      "soft", _volume_strong,     weight=1.0),
        Check("lead_lag_aligned",   "soft", _lead_lag_aligned,  weight=2.0),
        Check("funding_favorable",  "soft", _funding_favorable, weight=1.0),
    ], soft_threshold=soft_threshold)
