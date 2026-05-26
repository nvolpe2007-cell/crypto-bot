"""
Confluence engine — turn the four signals (+ trend + timing) into a setup decision.

Tier 1 (gates, BOTH required): funding extreme + OI spike.
Tier 2 (boost, not required): CVD divergence, liq proximity.
Trend filter (mandatory, research-driven): never fade into a sustained trend.
Timing filter: don't open shorts right after a funding reset.

Returns a Setup describing direction, type, confluence score, the size multiplier,
and the full signal context for logging. `direction is None` → no trade.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import config


@dataclass
class Setup:
    coin: str
    direction: Optional[str] = None          # "short" | "long" | None
    setup_type: Optional[str] = None         # "fade_short" | "flush_long"
    tier1_ok: bool = False
    tier2_score: int = 0                     # 0–2 (cvd, liq)
    cvd_confirmed: bool = False
    liq_proximity: bool = False
    size_multiplier: float = 0.0             # 1.0 / 1.5 / 2.0 (shorts); 1.0 (longs)
    blocked_reason: Optional[str] = None     # why a near-miss was rejected (for logging)
    context: Dict = field(default_factory=dict)  # all raw signal values

    @property
    def should_enter(self) -> bool:
        return self.direction is not None and self.size_multiplier > 0


def evaluate(coin: str,
             funding: Dict,
             oi: Dict,
             cvd: Dict,
             liq: Dict,
             trend: Dict,
             volume_spike: bool,
             btc_uptrend_ok: bool,
             minutes_to_funding_reset: int,
             in_post_funding_block: bool) -> Setup:
    """Combine signals into a Setup. Pure function — all inputs precomputed."""
    s = Setup(coin=coin)
    s.cvd_confirmed = bool(cvd.get("bearish_divergence") or cvd.get("bullish_divergence"))
    s.liq_proximity = bool(liq.get("short_proximity") or liq.get("long_proximity"))
    s.context = {
        "funding": funding, "oi": oi, "cvd": cvd, "liq": liq, "trend": trend,
        "minutes_to_funding_reset": minutes_to_funding_reset,
    }

    # ── Setup A — Fade Short (primary) ───────────────────────────────────────
    tier1_short = funding.get("short_eligible") and oi.get("short_spike")
    if tier1_short:
        s.tier1_ok = True
        # Trend filter: never fade into a confirmed uptrend.
        if config.TREND_FILTER_ENABLED and trend.get("strong_uptrend"):
            s.blocked_reason = "trend_filter_uptrend"
            return s
        # Timing: don't open shorts inside the post-funding block window.
        if in_post_funding_block:
            s.blocked_reason = "post_funding_block"
            return s
        # Tier-2 confluence → size.
        cvd_ok = bool(cvd.get("bearish_divergence"))
        liq_ok = bool(liq.get("short_proximity"))
        s.tier2_score = int(cvd_ok) + int(liq_ok)
        s.size_multiplier = _short_size(cvd_ok, liq_ok)
        s.direction = "short"
        s.setup_type = "fade_short"
        return s

    # ── Setup B — Post-Liquidation Flush Long (secondary) ────────────────────
    tier1_long = oi.get("long_flush") and funding.get("funding_collapsed") and volume_spike
    if tier1_long:
        s.tier1_ok = True
        # Only buy flushes when BTC isn't dumping, and not into our own downtrend.
        if not btc_uptrend_ok:
            s.blocked_reason = "btc_downtrend"
            return s
        if config.TREND_FILTER_ENABLED and trend.get("strong_downtrend"):
            s.blocked_reason = "trend_filter_downtrend"
            return s
        s.cvd_confirmed = bool(cvd.get("bullish_divergence"))
        s.liq_proximity = bool(liq.get("long_proximity"))
        s.tier2_score = int(s.cvd_confirmed) + int(s.liq_proximity)
        s.size_multiplier = 1.0    # flush longs never boosted (higher risk per spec)
        s.direction = "long"
        s.setup_type = "flush_long"
        return s

    return s


def _short_size(cvd_ok: bool, liq_ok: bool) -> float:
    """Spec sizing: 1.0x Tier1 only; 1.5x +CVD; 2.0x +CVD +liq (hard cap)."""
    if cvd_ok and liq_ok:
        return config.MAX_SIZE_BOOST          # 2.0x
    if cvd_ok:
        return config.TIER2_CVD_SIZE_BOOST    # 1.5x
    return 1.0


def _selftest():
    # Helpers to build signal dicts
    def F(short=False, long=False, collapsed=False):
        return {"short_eligible": short, "long_eligible": long,
                "funding_collapsed": collapsed, "funding_rate": 0.0007}
    def OI(short=False, flush=False):
        return {"short_spike": short, "long_flush": flush,
                "oi_4hr_change": 0.32, "oi_8hr_change": 0.4}
    NO_CVD = {"bearish_divergence": False, "bullish_divergence": False}
    NO_LIQ = {"short_proximity": False, "long_proximity": False}
    CALM = {"strong_uptrend": False, "strong_downtrend": False}
    UP = {"strong_uptrend": True, "strong_downtrend": False}

    # Tier1 short only → 1.0x, enters
    s = evaluate("SOLUSDT", F(short=True), OI(short=True), NO_CVD, NO_LIQ, CALM,
                 False, True, 60, False)
    assert s.should_enter and s.direction == "short" and s.size_multiplier == 1.0, s

    # Tier1 + CVD + liq → 2.0x
    s2 = evaluate("SOLUSDT", F(short=True), OI(short=True),
                  {"bearish_divergence": True, "bullish_divergence": False},
                  {"short_proximity": True, "long_proximity": False}, CALM,
                  False, True, 60, False)
    assert s2.size_multiplier == config.MAX_SIZE_BOOST and s2.tier2_score == 2, s2

    # Trend filter blocks short in strong uptrend
    s3 = evaluate("SOLUSDT", F(short=True), OI(short=True), NO_CVD, NO_LIQ, UP,
                  False, True, 60, False)
    assert not s3.should_enter and s3.blocked_reason == "trend_filter_uptrend", s3

    # Post-funding block stops entry
    s4 = evaluate("SOLUSDT", F(short=True), OI(short=True), NO_CVD, NO_LIQ, CALM,
                  False, True, 60, True)
    assert not s4.should_enter and s4.blocked_reason == "post_funding_block", s4

    # Flush long: oi flush + funding collapsed + volume spike + BTC ok → enters 1.0x
    s5 = evaluate("SOLUSDT", F(collapsed=True), OI(flush=True), NO_CVD, NO_LIQ, CALM,
                  volume_spike=True, btc_uptrend_ok=True,
                  minutes_to_funding_reset=120, in_post_funding_block=False)
    assert s5.should_enter and s5.direction == "long" and s5.size_multiplier == 1.0, s5

    # Flush long blocked when BTC dumping
    s6 = evaluate("SOLUSDT", F(collapsed=True), OI(flush=True), NO_CVD, NO_LIQ, CALM,
                  volume_spike=True, btc_uptrend_ok=False,
                  minutes_to_funding_reset=120, in_post_funding_block=False)
    assert not s6.should_enter and s6.blocked_reason == "btc_downtrend", s6

    # No tier1 → no trade
    s7 = evaluate("SOLUSDT", F(), OI(), NO_CVD, NO_LIQ, CALM, False, True, 60, False)
    assert not s7.should_enter and s7.direction is None, s7
    print("confluence selftest OK")


if __name__ == "__main__":
    _selftest()
