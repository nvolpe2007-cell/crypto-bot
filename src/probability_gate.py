"""
Probability Gate — stacked-edge decision layer.

Sits between signal generation and order execution. Takes a signal that has
already passed all rule-based checks (cooldowns, correlation, MTF, kill
filters, etc.) and asks: what is the actual probability this trade wins,
given every independent edge we can see right now?

Math:
    P(success) = 1 - ∏(1 - p_i)   for each *present* edge i

Each edge contributes a calibrated p_win prior. Priors are deliberately
modest (most sit between 0.52 and 0.62) — stacking is what builds the
probability up. Edges that are absent or opposed contribute nothing
(or are caught earlier as hard skips).

The gate then sizes via quarter-Kelly with R:R taken from the signal's
own stop/target ratio. Output is a `TradeReasoning` object containing
both the math and a list of human-readable edge descriptions for
Telegram.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Tunables (calibrate from trade_journal later) ──────────────────────────

# Hard threshold: don't take trades below this combined probability
MIN_PROBABILITY = float(os.getenv("PROB_GATE_MIN_P", "0.58"))

# Quarter-Kelly target: when the trade's Kelly fraction reaches this, size = 1.0x
# Below it, size is scaled down proportionally. Never scales up.
KELLY_REF = float(os.getenv("PROB_GATE_KELLY_REF", "0.10"))

# Master switch
ENABLED = os.getenv("PROB_GATE_ENABLED", "1") == "1"

# ── Conviction tiers (size in USD, intended hold) ─────────────────────────
#   Tier is selected by combined P(win) and number of edges present.
#   Sizes are concrete dollar amounts (per user preference), capped at MAX.
#   Hold durations are guidance — actual exit is trailing-stop or signal.
MAX_TRADE_USD = float(os.getenv("PROB_GATE_MAX_TRADE_USD", "75"))

# (tier_name, min_p, min_edges, target_usd, hold_minutes, trail_style)
TIERS = [
    ("conviction", 0.80, 5, 75.0, 24 * 60, "ema50_4h"),
    ("position",   0.75, 4, 50.0, 12 * 60, "ema50_4h"),
    ("swing",      0.65, 3, 25.0,  4 * 60, "ema21_1h"),
    ("scalp",      0.58, 2,  5.0,       60, "atr_stop"),
]


# ── Types ──────────────────────────────────────────────────────────────────

@dataclass
class Edge:
    name: str          # short tag: "ofi", "btc-lead", "regime", ...
    p_win: float       # prior probability of win given this edge alone
    note: str          # human-readable description for Telegram
    present: bool = True

@dataclass
class TradeReasoning:
    direction: str                   # "LONG" / "SHORT"
    edges: List[Edge]                # all edges considered (present + absent)
    combined_p: float                # stacked probability across present edges
    kelly_fraction: float            # full-Kelly f*
    quarter_kelly: float             # f*/4 (what we actually use)
    size_scale: float                # final size multiplier (≤ 1.0)
    rejected: bool                   # True if combined_p < MIN_PROBABILITY
    rejection_reason: Optional[str] = None
    is_macro_driven: bool = False    # True if gold or contagion edge is present
    # Conviction tier (set by _classify_tier)
    tier: str = "scalp"              # scalp | swing | position | conviction
    target_usd: float = 5.0          # dollar size suggested by tier
    hold_minutes: int = 60           # intended max hold (trail can exit sooner)
    trail_style: str = "atr_stop"    # atr_stop | ema21_1h | ema50_4h

    @property
    def present_edges(self) -> List[Edge]:
        return [e for e in self.edges if e.present]


# ── Core math ──────────────────────────────────────────────────────────────

def _classify_tier(combined_p: float, n_edges: int) -> tuple:
    """Pick tier from (probability, edge count). Returns (name, usd, hold_min, trail)."""
    for name, min_p, min_edges, usd, hold, trail in TIERS:
        if combined_p >= min_p and n_edges >= min_edges:
            return name, min(usd, MAX_TRADE_USD), hold, trail
    name, _, _, usd, hold, trail = TIERS[-1]  # scalp
    return name, min(usd, MAX_TRADE_USD), hold, trail


def _stack(probs: List[float]) -> float:
    """P_total = 1 - ∏(1 - p_i). Clipped to [0, 0.97]."""
    if not probs:
        return 0.5
    failure = 1.0
    for p in probs:
        # Only edges with positive lift over coinflip contribute
        p_eff = max(0.5, min(0.95, p))
        failure *= (1.0 - p_eff)
    # Re-anchor to 0.5 baseline: convert back so a single 0.5 edge = 0.5
    # We want stacking to *raise* P above the strongest single edge
    p_total = 1.0 - failure
    return min(0.97, p_total)


def _kelly(p_win: float, rr: float) -> float:
    """Full Kelly: f* = (p(b+1) - 1) / b, where b = reward/risk ratio."""
    if rr <= 0:
        return 0.0
    b = rr
    f = (p_win * (b + 1.0) - 1.0) / b
    return max(0.0, f)


# ── Edge evaluators ────────────────────────────────────────────────────────

def _ofi_edge(ofi: Optional[float], is_buy: bool) -> Edge:
    if ofi is None:
        return Edge("ofi", 0.5, "OFI: no data", present=False)
    aligned = (ofi > 0 and is_buy) or (ofi < 0 and not is_buy)
    mag = abs(ofi)
    if not aligned:
        return Edge("ofi", 0.5, f"OFI {ofi:+.2f}: opposes direction", present=False)
    if mag >= 0.35:
        return Edge("ofi", 0.64, f"OFI {ofi:+.2f}: heavy aligned flow")
    if mag >= 0.20:
        return Edge("ofi", 0.58, f"OFI {ofi:+.2f}: moderate aligned flow")
    if mag >= 0.10:
        return Edge("ofi", 0.54, f"OFI {ofi:+.2f}: mild aligned flow")
    return Edge("ofi", 0.5, f"OFI {ofi:+.2f}: too weak to count", present=False)


def _lead_lag_edge(lead_dir: Optional[str], lead_strength: float, is_buy: bool) -> Edge:
    want = "BUY" if is_buy else "SELL"
    if lead_dir != want:
        return Edge("btc-lead", 0.5, "BTC lead: not aligned", present=False)
    if lead_strength >= 0.6:
        return Edge("btc-lead", 0.62, f"BTC just moved {want} (strength {lead_strength:.2f})")
    if lead_strength >= 0.4:
        return Edge("btc-lead", 0.57, f"BTC leading {want} (strength {lead_strength:.2f})")
    return Edge("btc-lead", 0.53, f"BTC weakly leading {want} ({lead_strength:.2f})")


def _regime_edge(regime: str, is_buy: bool, entry_path: str) -> Edge:
    if entry_path in ("mr", "mr-extreme"):
        if regime == "RANGING":
            return Edge("regime", 0.60, "Regime RANGING: MR has positive edge")
        if regime in ("VOLATILE", "UNKNOWN"):
            return Edge("regime", 0.54, f"Regime {regime}: MR has small edge")
        return Edge("regime", 0.5, f"Regime {regime}: MR fights the trend", present=False)

    if is_buy and regime == "TRENDING_UP":
        return Edge("regime", 0.62, "Regime TRENDING_UP: long has trend tailwind")
    if (not is_buy) and regime == "TRENDING_DOWN":
        return Edge("regime", 0.60, "Regime TRENDING_DOWN: short has trend tailwind")
    if regime == "CRASH" and not is_buy:
        return Edge("regime", 0.58, "Regime CRASH: short side has edge")
    if regime in ("RANGING", "VOLATILE", "UNKNOWN"):
        return Edge("regime", 0.5, f"Regime {regime}: no directional edge", present=False)
    # trend-vs-counter-trend
    return Edge("regime", 0.5, f"Regime {regime}: counter-trend entry", present=False)


def _rsi_edge(rsi: float, is_buy: bool) -> Edge:
    if is_buy:
        if rsi <= 27:   return Edge("rsi", 0.60, f"RSI {rsi:.0f}: deeply oversold")
        if rsi <= 38:   return Edge("rsi", 0.56, f"RSI {rsi:.0f}: oversold pullback")
        if rsi >= 70:   return Edge("rsi", 0.5, f"RSI {rsi:.0f}: overbought (risky long)", present=False)
        return Edge("rsi", 0.5, f"RSI {rsi:.0f}: neutral", present=False)
    else:
        if rsi >= 73:   return Edge("rsi", 0.58, f"RSI {rsi:.0f}: deeply overbought")
        if rsi >= 62:   return Edge("rsi", 0.55, f"RSI {rsi:.0f}: overbought")
        if rsi <= 30:   return Edge("rsi", 0.5, f"RSI {rsi:.0f}: oversold (risky short)", present=False)
        return Edge("rsi", 0.5, f"RSI {rsi:.0f}: neutral", present=False)


def _adx_edge(adx: float) -> Edge:
    if adx >= 30:   return Edge("adx", 0.57, f"ADX {adx:.0f}: very strong trend")
    if adx >= 22:   return Edge("adx", 0.54, f"ADX {adx:.0f}: solid trend")
    return Edge("adx", 0.5, f"ADX {adx:.0f}: weak/no trend", present=False)


def _htf_edge(htf_alignment: float, is_buy: bool) -> Edge:
    """htf_alignment is the score returned by MultiTimeframeFilter (positive = aligned)."""
    if htf_alignment is None:
        return Edge("htf", 0.5, "HTF: no data", present=False)
    if htf_alignment > 5:
        return Edge("htf", 0.58, f"Higher TF aligned (+{htf_alignment:.0f})")
    if htf_alignment > 0:
        return Edge("htf", 0.53, f"Higher TF mildly aligned (+{htf_alignment:.0f})")
    if htf_alignment < -5:
        return Edge("htf", 0.5, f"Higher TF against ({htf_alignment:.0f})", present=False)
    return Edge("htf", 0.5, "HTF neutral", present=False)


def _funding_edge(funding_rate: Optional[float], is_buy: bool) -> Edge:
    """funding_rate is per-8h. Annualize ≈ rate * 3 * 365 = rate * 1095."""
    if funding_rate is None:
        return Edge("funding", 0.5, "Funding: n/a", present=False)
    apy = funding_rate * 1095 * 100  # percent
    if is_buy and apy < -5:
        return Edge("funding", 0.55, f"Funding {apy:+.0f}% APY: shorts pay longs")
    if (not is_buy) and apy > 15:
        return Edge("funding", 0.55, f"Funding {apy:+.0f}% APY: longs pay shorts")
    return Edge("funding", 0.5, f"Funding {apy:+.0f}% APY: not favorable", present=False)


def _gold_edge(macro_state, is_buy: bool) -> Edge:
    """
    Conditional edge from BTC-gold correlation regime.

    Only fires when |corr_30d| > 0.5 (clear regime). When inverse regime is
    active and gold moved meaningfully yesterday, it implies a directional
    push on BTC (and via contagion, alts) today.

    See memory: btc-gold-correlation. Gold-BTC has flipped sign multiple
    times since 2023 — a static rule is wrong; this gate it conditionally.
    """
    if macro_state is None:
        return Edge("gold", 0.5, "Gold: no macro data", present=False)

    corr = macro_state.btc_gold_corr_30d
    g_chg = macro_state.gold_change_1d

    if abs(corr) < 0.5:
        return Edge("gold", 0.5,
                    f"Gold corr {corr:+.2f}: too weak to act on", present=False)
    if abs(g_chg) < 0.5:
        return Edge("gold", 0.5,
                    f"Gold {g_chg:+.2f}%: move too small", present=False)

    # Inverse regime: gold down → crypto up, gold up → crypto down
    if corr <= -0.5:
        gold_implies_up = g_chg < 0   # gold dropping → crypto rises
        if (gold_implies_up and is_buy) or (not gold_implies_up and not is_buy):
            magnitude = min(0.62, 0.54 + abs(g_chg) * 0.02 + (abs(corr) - 0.5) * 0.1)
            return Edge("gold", magnitude,
                        f"Gold {g_chg:+.2f}%, inverse corr {corr:+.2f} → "
                        f"{'long' if is_buy else 'short'} edge")
        return Edge("gold", 0.5,
                    f"Gold {g_chg:+.2f}% inverse corr {corr:+.2f}: against trade",
                    present=False)

    # Positive regime (rare): gold up → crypto up
    if corr >= 0.5:
        gold_implies_up = g_chg > 0
        if (gold_implies_up and is_buy) or (not gold_implies_up and not is_buy):
            magnitude = min(0.58, 0.53 + abs(g_chg) * 0.01)
            return Edge("gold", magnitude,
                        f"Gold {g_chg:+.2f}%, positive corr {corr:+.2f} → "
                        f"{'long' if is_buy else 'short'} edge")
        return Edge("gold", 0.5,
                    f"Gold {g_chg:+.2f}% positive corr {corr:+.2f}: against trade",
                    present=False)

    return Edge("gold", 0.5, "Gold: no regime edge", present=False)


def _contagion_edge(symbol: str, macro_state, is_buy: bool) -> Edge:
    """
    Macro contagion: when a BTC-wide macro shock is active, ETH/SOL inherit
    the directional bias with size scaled by alt-beta. See memory:
    alt-beta-to-btc — SOL ~1.3x, ETH ~1.05x BTC on downside moves.

    This only fires for non-BTC symbols and only when a macro driver is active.
    Returns a small positive edge — the *real* sizing amplification is applied
    in the orchestration layer (paper_trading) via beta-scaled size_usd.
    """
    if macro_state is None or symbol.startswith("BTC"):
        return Edge("contagion", 0.5, "Contagion: n/a", present=False)
    if macro_state.corr_strength < 0.5 or abs(macro_state.gold_change_1d) < 0.5:
        return Edge("contagion", 0.5, "Contagion: no active macro driver", present=False)

    # Same direction check as gold edge
    inverse = macro_state.is_inverse_regime
    gold_implies_up = (macro_state.gold_change_1d < 0) if inverse else (macro_state.gold_change_1d > 0)
    if (gold_implies_up and is_buy) or (not gold_implies_up and not is_buy):
        from .macro_data import alt_beta
        beta = alt_beta(symbol)
        # higher beta → higher P bump (alts amplify the macro move)
        p = 0.53 + min(0.07, (beta - 1.0) * 0.12)
        return Edge("contagion", p,
                    f"Macro contagion via BTC (β={beta:.2f} for {symbol.split('/')[0]})")
    return Edge("contagion", 0.5, "Contagion: macro against trade", present=False)


def _confidence_edge(rule_confidence: float) -> Edge:
    """The legacy ScientificStrategy confidence is itself a (weak) calibrated edge."""
    if rule_confidence >= 85:
        return Edge("rules", 0.60, f"Rule confidence {rule_confidence:.0f}: very strong stack")
    if rule_confidence >= 70:
        return Edge("rules", 0.56, f"Rule confidence {rule_confidence:.0f}: strong stack")
    if rule_confidence >= 55:
        return Edge("rules", 0.53, f"Rule confidence {rule_confidence:.0f}: above threshold")
    return Edge("rules", 0.5, f"Rule confidence {rule_confidence:.0f}: marginal", present=False)


# ── Public API ─────────────────────────────────────────────────────────────

class ProbabilityGate:
    """
    Stateless evaluator. Build one and reuse.

    Usage:
        gate = ProbabilityGate()
        reasoning = gate.evaluate(sig, is_buy=True, entry_path='main',
                                  lead_strength=lead_lag.get_strength(symbol),
                                  htf_alignment=htf_filter.alignment_score(symbol, True))
        if reasoning.rejected:
            log/skip
        else:
            size_usd *= reasoning.size_scale
            notifier.send_trade_reasoning(symbol, side, price, reasoning, size_usd, entry_path)
    """

    def __init__(self, min_p: float = MIN_PROBABILITY, kelly_ref: float = KELLY_REF):
        self.min_p = min_p
        self.kelly_ref = kelly_ref

    def evaluate(self,
                 sig,
                 is_buy: bool,
                 entry_path: str = "main",
                 lead_strength: float = 0.0,
                 htf_alignment: Optional[float] = None,
                 macro_state=None,
                 symbol: str = "BTC/USD") -> TradeReasoning:

        edges: List[Edge] = [
            _confidence_edge(getattr(sig, "confidence", 0.0)),
            _ofi_edge(getattr(sig, "ofi", None), is_buy),
            _lead_lag_edge(getattr(sig, "lead_lag_dir", None), lead_strength, is_buy),
            _regime_edge(getattr(sig, "regime", "UNKNOWN"), is_buy, entry_path),
            _rsi_edge(getattr(sig, "rsi", 50.0), is_buy),
            _adx_edge(getattr(sig, "adx", 20.0)),
            _htf_edge(htf_alignment, is_buy),
            _funding_edge(getattr(sig, "funding_rate", None), is_buy),
            _gold_edge(macro_state, is_buy),
            _contagion_edge(symbol, macro_state, is_buy),
        ]

        present_probs = [e.p_win for e in edges if e.present]
        combined_p = _stack(present_probs)

        # R:R from the signal's own stop/target
        try:
            sl_pct = sig.stop_loss_pct()
            tp_pct = sig.take_profit_pct()
            rr = (tp_pct / sl_pct) if sl_pct > 0 else 2.0
        except Exception:
            rr = 2.0

        k_full = _kelly(combined_p, rr)
        k_quarter = k_full * 0.25
        size_scale = min(1.0, k_quarter / self.kelly_ref) if self.kelly_ref > 0 else 1.0
        size_scale = max(0.0, size_scale)

        rejected = combined_p < self.min_p
        reason = None
        if rejected:
            reason = f"P={combined_p:.2f} < min {self.min_p:.2f} (only {len(present_probs)} edges present)"

        macro_driven = any(e.present and e.name in ("gold", "contagion") for e in edges)
        tier, target_usd, hold_min, trail = _classify_tier(combined_p, len(present_probs))

        return TradeReasoning(
            direction="LONG" if is_buy else "SHORT",
            edges=edges,
            combined_p=combined_p,
            kelly_fraction=k_full,
            quarter_kelly=k_quarter,
            size_scale=size_scale,
            rejected=rejected,
            rejection_reason=reason,
            is_macro_driven=macro_driven,
            tier=tier,
            target_usd=target_usd,
            hold_minutes=hold_min,
            trail_style=trail,
        )
