"""Tests for the honest maker-only fill model (src/maker_fill.py).

The whole point of maker-only is that fills are NOT guaranteed and are adversely
selected. These tests pin that behaviour so a 90-day proof can't be flattered by
an optimistic touch-fill.
"""
from src.maker_fill import post_maker, apply_trade, expire, MakerOrder, MAKER_FEE_SPOT


def test_buy_fills_only_when_taker_sell_trades_through():
    # resting BUY at 100; a taker SELL at 99.9 trades DOWN into our bid → fill.
    o = post_maker("buy", 100.0, 500.0, ts=0.0, timeout_secs=30)
    apply_trade(o, trade_price=99.9, trade_side="sell", trade_ts=1.0)
    assert o.filled and o.fill_price == 100.0      # fills at OUR limit, no spread cross


def test_buy_does_not_fill_on_taker_buy_or_above_limit():
    # a taker BUY (lifting the ask) does NOT hit our resting bid …
    o = post_maker("buy", 100.0, 500.0, ts=0.0)
    apply_trade(o, trade_price=99.0, trade_side="buy", trade_ts=1.0)
    assert not o.filled and not o.cancelled
    # … nor does a sell that prints ABOVE our bid (didn't trade through us)
    apply_trade(o, trade_price=100.5, trade_side="sell", trade_ts=2.0)
    assert not o.filled


def test_sell_fills_only_when_taker_buy_trades_through():
    o = post_maker("sell", 100.0, 500.0, ts=0.0)
    apply_trade(o, trade_price=100.1, trade_side="buy", trade_ts=1.0)  # buyers up into us
    assert o.filled and o.fill_price == 100.0


def test_nonfill_timeout_means_no_trade():
    # the tape runs away (only prints that don't trade through), past the timeout
    o = post_maker("buy", 100.0, 500.0, ts=0.0, timeout_secs=30)
    apply_trade(o, trade_price=101.0, trade_side="buy", trade_ts=40.0)
    assert o.cancelled and not o.filled            # a maker signal can simply not execute


def test_expire_cancels_quiet_book():
    o = post_maker("buy", 100.0, 500.0, ts=0.0, timeout_secs=30)
    expire(o, now_ts=29.0)
    assert not o.resolved                           # still within window
    expire(o, now_ts=31.0)
    assert o.cancelled


def test_fee_charged_only_on_fill_and_no_spread_cross():
    o = post_maker("buy", 100.0, 500.0, ts=0.0)
    assert o.fee_usd == 0.0                          # unfilled → no fee
    apply_trade(o, 99.0, "sell", 1.0)
    assert o.fee_usd == 500.0 * MAKER_FEE_SPOT       # maker fee on notional
    assert o.fill_price == o.limit_price             # never pays the spread


def test_resolution_is_idempotent():
    o = post_maker("buy", 100.0, 500.0, ts=0.0)
    apply_trade(o, 99.0, "sell", 1.0)
    first_price = o.fill_price
    apply_trade(o, 95.0, "sell", 2.0)               # later prints don't re-fill
    assert o.fill_price == first_price
