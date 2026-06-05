"""
Volume Profile — volume-by-price structure from OHLCV bars.

WHY THIS EXISTS / WHAT IT IS *NOT*:
This is a RESEARCH SIGNAL, not a strategy. It computes where volume has traded
by PRICE (not time) so we can annotate swing decisions with structural context
(POC / value area / high- & low-volume nodes) and later MEASURE — via the proof
scorecard — whether that context separates winners from losers. It is deliberately
NOT wired into entries/exits: the swing strategy stays locked until a signal earns
its way in with forward evidence. See [[swing_forward_test]] / proof_scorecard.

Input is OHLCV bars (the same Kraken data the swing runner pulls). With only bar
data — no tick prints — each bar's volume is distributed uniformly across the
[low, high] range it covers (the standard "VP split" approximation). That's an
estimate of true volume-at-price, and it's labelled as such.

Definitions (see the user's market-structure notes):
  POC  — Point of Control: price bin with the most volume (strongest magnet)
  VA   — Value Area: smallest contiguous band around POC holding ~70% of volume
  VAH/VAL — Value Area High / Low (band edges; breakout/retest levels)
  HVN  — High-Volume Node: local volume peak (price consolidates / returns)
  LVN  — Low-Volume Node: local volume trough (price moves fast through it)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

# Fraction of total volume that defines the value area (CME/TPO convention: 70%).
VALUE_AREA_FRAC = float(os.getenv("VP_VALUE_AREA_FRAC", "0.70"))
DEFAULT_BINS = int(os.getenv("VP_BINS", "50"))


@dataclass
class VolumeProfile:
    poc: float                       # price at the point of control
    vah: float                       # value-area high
    val: float                       # value-area low
    bin_prices: List[float] = field(default_factory=list)   # bin centers (asc)
    bin_volumes: List[float] = field(default_factory=list)  # volume per bin
    hvn: List[float] = field(default_factory=list)          # high-volume node prices
    lvn: List[float] = field(default_factory=list)          # low-volume node prices
    lo: float = 0.0                  # profiled price range
    hi: float = 0.0

    def classify(self, price: float) -> str:
        """Where `price` sits relative to the profile — the bit a strategy would
        actually consume. Returns one of: above_value / below_value / at_poc /
        in_value / lvn (fast-move zone) / hvn (consolidation)."""
        if price > self.vah:
            return "above_value"
        if price < self.val:
            return "below_value"
        # inside the value area — refine by node type / POC proximity
        if self.bin_prices:
            span = (self.hi - self.lo) or 1.0
            tol = span / max(len(self.bin_prices), 1)
            if abs(price - self.poc) <= tol:
                return "at_poc"
            for p in self.lvn:
                if abs(price - p) <= tol:
                    return "lvn"
            for p in self.hvn:
                if abs(price - p) <= tol:
                    return "hvn"
        return "in_value"


def _overlap(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> float:
    return max(0.0, min(hi_a, hi_b) - max(lo_a, lo_b))


def volume_profile(bars: List[dict], n_bins: int = DEFAULT_BINS,
                   value_area_frac: float = VALUE_AREA_FRAC) -> Optional[VolumeProfile]:
    """Build a volume profile from OHLCV bars.

    bars: list of dicts with keys h, l, c (and ideally `volume`/`v`). Volume is
    spread across each bar's [low, high] in proportion to each bin's overlap;
    bars with no range land entirely in their close bin. Returns None if there's
    not enough data or no volume.
    """
    if not bars or n_bins < 2:
        return None
    lows = [float(b["l"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        return None

    width = (hi - lo) / n_bins
    edges = [lo + i * width for i in range(n_bins + 1)]
    centers = [(edges[i] + edges[i + 1]) / 2.0 for i in range(n_bins)]
    vols = [0.0] * n_bins

    for b in bars:
        bl, bh = float(b["l"]), float(b["h"])
        v = float(b.get("volume", b.get("v", 0.0)) or 0.0)
        if v <= 0:
            continue
        if bh <= bl:                       # zero-range bar → close bin
            idx = min(int((float(b["c"]) - lo) / width), n_bins - 1)
            vols[max(0, idx)] += v
            continue
        rng = bh - bl
        for i in range(n_bins):
            ov = _overlap(bl, bh, edges[i], edges[i + 1])
            if ov > 0:
                vols[i] += v * (ov / rng)   # proportional split

    total = sum(vols)
    if total <= 0:
        return None

    poc_idx = max(range(n_bins), key=lambda i: vols[i])

    # Value area: grow out from the POC, each step taking the heavier of the two
    # adjacent bins, until ~value_area_frac of total volume is enclosed.
    target = value_area_frac * total
    lo_i = hi_i = poc_idx
    acc = vols[poc_idx]
    while acc < target and (lo_i > 0 or hi_i < n_bins - 1):
        below = vols[lo_i - 1] if lo_i > 0 else -1.0
        above = vols[hi_i + 1] if hi_i < n_bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += vols[hi_i]
        else:
            lo_i -= 1
            acc += vols[lo_i]

    # Nodes: a bin is an HVN if it's a local max above the mean, an LVN if it's a
    # local min below the mean. Interior bins only (need both neighbors).
    mean_v = total / n_bins
    hvn, lvn = [], []
    for i in range(1, n_bins - 1):
        if vols[i] >= vols[i - 1] and vols[i] >= vols[i + 1] and vols[i] > mean_v:
            hvn.append(centers[i])
        elif vols[i] <= vols[i - 1] and vols[i] <= vols[i + 1] and vols[i] < mean_v:
            lvn.append(centers[i])

    return VolumeProfile(
        poc=centers[poc_idx], vah=edges[hi_i + 1], val=edges[lo_i],
        bin_prices=centers, bin_volumes=vols, hvn=hvn, lvn=lvn, lo=lo, hi=hi,
    )
