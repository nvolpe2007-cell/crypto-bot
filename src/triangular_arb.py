"""
Triangular Arbitrage Scanner — single-venue (Kraken) cycle scanner.

Cycle representation:
  A cycle is three legs. Each leg is (symbol, side, action) where:
    symbol = e.g. "BTC/USD"
    side   = "ask" or "bid"   ← the side of the book we'd cross as a taker
    action = "buy" or "sell"  ← what we're doing to that pair

  USD → BTC → ETH → USD via legs:
    1) BUY  BTC/USD → cross ASK → spend USD, get BTC.   units_out = in / ask
    2) BUY  ETH/BTC → cross ASK → spend BTC, get ETH.   units_out = in / ask
    3) SELL ETH/USD → cross BID → spend ETH, get USD.   units_out = in * bid

  USD → ETH → BTC → USD via legs (note: no BTC/ETH pair; we SELL ETH/BTC):
    1) BUY  ETH/USD → cross ASK → spend USD, get ETH.
    2) SELL ETH/BTC → cross BID → spend ETH, get BTC.   units_out = in * bid
    3) SELL BTC/USD → cross BID → spend BTC, get USD.

The cycle's net edge is the product of leg ratios × (1 − fee)^3. > 1 = profit.

Why scanner-only (no live execution yet):
  Triangular arb has nasty failure modes — partial fills on leg 2 or 3 leave
  you holding unwanted inventory in the middle currency. IOC + careful state
  management + fast re-quote logic = at least one full session of code +
  thorough integration testing. This module observes + paper-simulates so we
  build a record of how often real opportunities exist on Kraken's pair set
  before committing to execution code.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────────────

# Per-leg taker fee on Kraken Pro spot lowest tier.
LEG_FEE = float(os.getenv("TRIARB_LEG_FEE", "0.0040"))

# Minimum net edge AFTER fees to record an opportunity.
MIN_EDGE_BPS = float(os.getenv("TRIARB_MIN_EDGE_BPS", "5.0"))

# Test notional per cycle (paper sim — never touches real funds).
TEST_NOTIONAL_USD = float(os.getenv("TRIARB_TEST_NOTIONAL_USD", "100.0"))

JOURNAL_MAXLEN = 500


# ── Types ─────────────────────────────────────────────────────────────────────

Side = Literal["ask", "bid"]   # the side of the book we cross as taker


@dataclass(frozen=True)
class Leg:
    """One leg of a triangular cycle."""
    symbol: str    # e.g. "BTC/USD"
    side:   Side   # "ask" → we BUY (price multiplies as 1/ask)
                   # "bid" → we SELL (price multiplies as bid)


@dataclass(frozen=True)
class Cycle:
    """Three legs, plus a label for logs."""
    label: str
    legs:  Tuple[Leg, Leg, Leg]


@dataclass
class Opportunity:
    cycle:        Cycle
    edge_bps:     float
    final_usd:    float
    profit_usd:   float
    prices:       Tuple[float, float, float]   # leg1, leg2, leg3 at scan time
    ts:           float = field(default_factory=time.time)


# ── Scanner ───────────────────────────────────────────────────────────────────

BookGetter = Callable[[str], Tuple[List[List[float]], List[List[float]]]]


class TriangularArbScanner:
    def __init__(self, cycles: List[Cycle], get_book: BookGetter,
                 leg_fee: float = LEG_FEE, min_edge_bps: float = MIN_EDGE_BPS):
        self.cycles       = cycles
        self.get_book     = get_book
        self.leg_fee      = leg_fee
        self.min_edge_bps = min_edge_bps
        self.journal: Deque[Opportunity] = deque(maxlen=JOURNAL_MAXLEN)
        self._scans         = 0
        self._opps_found    = 0
        self._best_edge_bps = 0.0
        self._cum_paper_pnl = 0.0

    def scan_once(self) -> List[Opportunity]:
        self._scans += 1
        found: List[Opportunity] = []
        for cycle in self.cycles:
            opp = self._evaluate(cycle)
            if opp is not None and opp.edge_bps >= self.min_edge_bps:
                found.append(opp)
                self.journal.append(opp)
                self._opps_found += 1
                self._cum_paper_pnl += opp.profit_usd
                if opp.edge_bps > self._best_edge_bps:
                    self._best_edge_bps = opp.edge_bps
        return found

    def _leg_price_and_ratio(self, leg: Leg) -> Optional[Tuple[float, float]]:
        """Return (top_price, multiplier_for_units) for a leg.

        For an "ask" leg (we BUY): multiplier = 1/ask  (spend 1 quote, get 1/ask base)
        For a "bid" leg (we SELL): multiplier = bid    (sell 1 base, get bid quote)
        """
        bids, asks = self.get_book(leg.symbol)
        if leg.side == "ask":
            if not asks:
                return None
            try:
                p = float(asks[0][0])
            except (IndexError, TypeError, ValueError):
                return None
            if p <= 0:
                return None
            return p, 1.0 / p
        else:  # bid
            if not bids:
                return None
            try:
                p = float(bids[0][0])
            except (IndexError, TypeError, ValueError):
                return None
            if p <= 0:
                return None
            return p, p

    def _evaluate(self, cycle: Cycle) -> Optional[Opportunity]:
        prices: List[float] = []
        ratio = 1.0
        for leg in cycle.legs:
            r = self._leg_price_and_ratio(leg)
            if r is None:
                return None
            p, mult = r
            prices.append(p)
            ratio *= mult
        # Apply taker fee on each leg
        ratio *= (1 - self.leg_fee) ** len(cycle.legs)
        edge_bps = (ratio - 1.0) * 10_000
        if edge_bps < self.min_edge_bps:
            return None
        final_usd  = TEST_NOTIONAL_USD * ratio
        profit_usd = final_usd - TEST_NOTIONAL_USD
        return Opportunity(
            cycle=cycle, edge_bps=edge_bps,
            final_usd=final_usd, profit_usd=profit_usd,
            prices=(prices[0], prices[1], prices[2]),
        )

    def summary(self) -> dict:
        return {
            "scans":          self._scans,
            "opps_found":     self._opps_found,
            "hit_rate":       (self._opps_found / self._scans) if self._scans else 0.0,
            "best_edge_bps":  round(self._best_edge_bps, 2),
            "cum_paper_pnl_usd": round(self._cum_paper_pnl, 4),
            "journal_size":   len(self.journal),
        }

    def format_log(self, opps: List[Opportunity]) -> str:
        if not opps:
            return ""
        parts = []
        for o in opps:
            parts.append(
                f"{o.cycle.label} edge={o.edge_bps:+.1f}bps paper_pnl=${o.profit_usd:+.4f}"
            )
        return "[TRIARB] " + " | ".join(parts)


# ── Default cycles for the bot's current symbol set ───────────────────────────

DEFAULT_CYCLES: List[Cycle] = [
    Cycle("USD→BTC→ETH→USD", (
        Leg("BTC/USD", "ask"),       # buy BTC with USD
        Leg("ETH/BTC", "ask"),       # buy ETH with BTC
        Leg("ETH/USD", "bid"),       # sell ETH for USD
    )),
    Cycle("USD→ETH→BTC→USD", (
        Leg("ETH/USD", "ask"),       # buy ETH with USD
        Leg("ETH/BTC", "bid"),       # sell ETH for BTC (no BTC/ETH pair)
        Leg("BTC/USD", "bid"),       # sell BTC for USD
    )),
    Cycle("USD→BTC→SOL→USD", (
        Leg("BTC/USD", "ask"),
        Leg("SOL/BTC", "ask"),
        Leg("SOL/USD", "bid"),
    )),
    Cycle("USD→SOL→BTC→USD", (
        Leg("SOL/USD", "ask"),
        Leg("SOL/BTC", "bid"),
        Leg("BTC/USD", "bid"),
    )),
]


# Cross pairs to add to the WS book-feed subscription so DEFAULT_CYCLES can
# actually be evaluated.
REQUIRED_CROSS_PAIRS = ["ETH/BTC", "SOL/BTC"]
