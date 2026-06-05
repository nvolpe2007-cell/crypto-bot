"""
Unit tests for src/triangular_arb.py

Focus: the staleness gate and no-arbitrage backstop that stop the scanner from
logging phantom edges. Background: the thin cross pairs (ETH/BTC, SOL/BTC)
update far less often than the USD legs on the live WS book; pairing a fresh
USD quote with a stale cross quote manufactures an edge that isn't tradeable.

Covers:
- A genuinely fresh, small positive edge is recorded as an opportunity.
- A cycle with one stale leg is voided (skipped_stale), not logged as profit.
- An implausibly large edge (data artifact) is dropped (skipped_implausible)
  and excluded from cum_paper_pnl.
- With no staleness source the gate is inert (old behavior preserved).
"""

from src.triangular_arb import Cycle, Leg, TriangularArbScanner


def _book(bid: float, ask: float):
    """One-level (bids, asks) book in REST shape."""
    return ([[bid, 1.0]], [[ask, 1.0]])


# A single cycle: USD -> A -> B -> USD.
#   leg1 BUY  A/USD  (cross ask)
#   leg2 BUY  B/A    (cross ask)
#   leg3 SELL B/USD  (cross bid)
CYCLE = Cycle("USD→A→B→USD", (
    Leg("A/USD", "ask"),
    Leg("B/A",   "ask"),
    Leg("B/USD", "bid"),
))


def _make_scanner(books, staleness=None, **kw):
    return TriangularArbScanner(
        cycles=[CYCLE],
        get_book=lambda s: books[s],
        get_staleness=(None if staleness is None else (lambda s: staleness[s])),
        leg_fee=0.0,            # isolate the gate logic from fee math
        min_edge_bps=1.0,
        **kw,
    )


def test_fresh_small_edge_is_recorded():
    # ratio = (1/100) * (1/0.01) * 1.02 = 1.02 -> +200 bps (under default cap).
    books = {
        "A/USD": _book(bid=100.0, ask=100.0),
        "B/A":   _book(bid=0.01,  ask=0.01),
        "B/USD": _book(bid=1.02,  ask=1.02),
    }
    stale = {"A/USD": 0.1, "B/A": 0.2, "B/USD": 0.1}
    sc = _make_scanner(books, stale, max_edge_bps=0.0)  # cap off for this case
    opps = sc.scan_once()
    assert len(opps) == 1
    assert opps[0].edge_bps > 0
    assert sc.summary()["skipped_stale"] == 0


def test_stale_leg_voids_cycle():
    books = {
        "A/USD": _book(bid=100.0, ask=100.0),
        "B/A":   _book(bid=0.01,  ask=0.01),
        "B/USD": _book(bid=1.02,  ask=1.02),
    }
    # B/A (the thin cross) is 30s stale; cycle must be voided.
    stale = {"A/USD": 0.1, "B/A": 30.0, "B/USD": 0.1}
    sc = _make_scanner(books, stale, max_staleness_sec=3.0, max_edge_bps=0.0)
    opps = sc.scan_once()
    assert opps == []
    assert sc.summary()["skipped_stale"] == 1
    assert sc.summary()["cum_paper_pnl_usd"] == 0.0


def test_implausible_edge_is_dropped():
    # ratio = (1/100)*(1/0.01)*1.50 = 1.50 -> +5000 bps, clearly an artifact.
    books = {
        "A/USD": _book(bid=100.0, ask=100.0),
        "B/A":   _book(bid=0.01,  ask=0.01),
        "B/USD": _book(bid=1.50,  ask=1.50),
    }
    stale = {"A/USD": 0.1, "B/A": 0.1, "B/USD": 0.1}
    sc = _make_scanner(books, stale, max_edge_bps=50.0)
    opps = sc.scan_once()
    assert opps == []
    assert sc.summary()["skipped_implausible"] == 1
    assert sc.summary()["cum_paper_pnl_usd"] == 0.0


def test_no_staleness_source_keeps_old_behavior():
    books = {
        "A/USD": _book(bid=100.0, ask=100.0),
        "B/A":   _book(bid=0.01,  ask=0.01),
        "B/USD": _book(bid=1.02,  ask=1.02),
    }
    sc = _make_scanner(books, staleness=None, max_edge_bps=0.0)
    opps = sc.scan_once()
    assert len(opps) == 1
    assert sc.summary()["skipped_stale"] == 0
