"""Tests for the real-tape tick CVD (src/cvd_tracker.TickCVDTracker)."""

from src.cvd_tracker import TickCVDTracker, CVDState
from src.orderflow_ws import obi_from_book


def test_taker_buys_accumulate_positive_cvd():
    t = TickCVDTracker("BTC/USD")
    st = None
    for i in range(5):
        st = t.update_tick(price=100 + i, qty=1.0, side="buy", timestamp=float(i))
    assert isinstance(st, CVDState)
    assert st.cvd_now == 5.0          # +1 per taker buy
    assert st.cvd_direction == 1
    assert st.candle_count == 5


def test_taker_sells_accumulate_negative_cvd():
    t = TickCVDTracker("BTC/USD")
    st = None
    for i in range(4):
        st = t.update_tick(price=100 - i, qty=2.0, side="sell", timestamp=float(i))
    assert st.cvd_now == -8.0
    assert st.cvd_direction == -1


def test_price_responding_flag_detects_absorption():
    # heavy buying but price FALLING = absorption → not responding
    t = TickCVDTracker("BTC/USD")
    st = None
    for i in range(4):
        st = t.update_tick(price=100 - i, qty=3.0, side="buy", timestamp=float(i))
    assert st.cvd_direction == 1 and st.price_responding is False


def test_aligned_with_ofi():
    t = TickCVDTracker("BTC/USD")
    for i in range(3):
        t.update_tick(price=100 + i, qty=1.0, side="buy", timestamp=float(i))
    assert t.aligned_with_ofi(1) is True
    assert t.aligned_with_ofi(-1) is False


# ── OBI helper (pure) ─────────────────────────────────────────────────────────

def test_obi_from_book_bid_heavy():
    bids = [[100.0, 8.0], [99.0, 5.0]]
    asks = [[101.0, 2.0], [102.0, 1.0]]
    obi = obi_from_book(bids, asks)
    assert obi == 13.0 / 16.0          # bid-heavy → > 0.5


def test_obi_from_book_empty_is_none():
    assert obi_from_book([], [[101.0, 1.0]]) is None
    assert obi_from_book([[100.0, 0.0]], [[101.0, 0.0]]) is None
