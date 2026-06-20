"""
Honest maker-only (post-only) fill model for the microstructure scalper.

Paper trading historically filled every order at mid ± slippage — a TAKER that
crosses the spread and always fills. That is the wrong model for a maker-only
strategy and it flatters results. A maker posts a RESTING limit and only trades
when the tape comes to it; the honest consequences this models:

  • NO SPREAD-CROSS on entry (you set the price), and the lower MAKER fee.
  • NON-FILL: if the tape never trades through your limit before the timeout, the
    order is CANCELLED and there is NO TRADE. A signal the market ran away from
    simply doesn't execute — the real cost of being a maker, recorded not hidden.
  • ADVERSE SELECTION: a resting BUY fills only when a taker SELL trades down
    through your bid (price falling into you); a resting SELL fills only when a
    taker BUY lifts your ask (price rising into you). So you are systematically
    filled just as the tape pushes against you — captured here for free, no
    optimistic "touch fill".

Pure and synchronous so it is fully unit-testable offline (feed it a trade tape);
the live loop drives it by replaying KrakenTradeFeed ticks into apply_trade().
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Kraken SPOT maker fee (the executable, US-retail venue): 0.25%/side at tier 0.
# Perp maker is ~0.02% but Kraken Futures isn't US-retail; spot is the honest path.
MAKER_FEE_SPOT = float(os.getenv("MAKER_FEE_SPOT", "0.0025"))
# How long a resting quote waits for a through-trade before it's cancelled (no
# trade). Short, because a scalper's edge is gone if the quote sits stale.
MAKER_FILL_TIMEOUT_SECS = float(os.getenv("MAKER_FILL_TIMEOUT_SECS", "30"))


@dataclass
class MakerOrder:
    """A resting post-only limit order and its resolution."""
    side: str                 # 'buy' (rests at bid) | 'sell' (rests at ask)
    limit_price: float
    size_usd: float
    post_ts: float
    timeout_secs: float = MAKER_FILL_TIMEOUT_SECS
    fee_frac: float = MAKER_FEE_SPOT
    filled: bool = False
    cancelled: bool = False
    fill_price: Optional[float] = None
    fill_ts: Optional[float] = None

    @property
    def resolved(self) -> bool:
        return self.filled or self.cancelled

    @property
    def fee_usd(self) -> float:
        """Maker fee on the notional — charged only if/when it actually fills.
        No spread-cross cost: a maker sets the price, it doesn't pay the spread."""
        return self.size_usd * self.fee_frac if self.filled else 0.0


def post_maker(side: str, limit_price: float, size_usd: float, ts: float,
               *, timeout_secs: float = MAKER_FILL_TIMEOUT_SECS,
               fee_frac: float = MAKER_FEE_SPOT) -> MakerOrder:
    """Post a resting maker order at `limit_price` (caller passes best bid for a
    buy / best ask for a sell, so it never crosses the spread)."""
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy/sell, got {side!r}")
    return MakerOrder(side=side, limit_price=float(limit_price),
                      size_usd=float(size_usd), post_ts=float(ts),
                      timeout_secs=float(timeout_secs), fee_frac=float(fee_frac))


def apply_trade(order: MakerOrder, trade_price: float, trade_side: str,
                trade_ts: float) -> MakerOrder:
    """Resolve a resting order against one tape print (taker `trade_side`).

    Fill rule (the honest, adverse-selection-aware part):
      • resting BUY  fills only on a taker SELL at price <= our bid
        (sellers trading DOWN into us);
      • resting SELL fills only on a taker BUY  at price >= our ask
        (buyers trading UP into us).
    Times out first: a print after the timeout cancels the unfilled order
    (no trade). Mutates and returns `order` for convenience; idempotent once
    resolved."""
    if order.resolved:
        return order
    if trade_ts - order.post_ts > order.timeout_secs:
        order.cancelled = True
        return order
    ts = trade_side.lower()
    if order.side == "buy" and ts == "sell" and trade_price <= order.limit_price:
        order.filled, order.fill_price, order.fill_ts = True, order.limit_price, trade_ts
    elif order.side == "sell" and ts == "buy" and trade_price >= order.limit_price:
        order.filled, order.fill_price, order.fill_ts = True, order.limit_price, trade_ts
    return order


def expire(order: MakerOrder, now_ts: float) -> MakerOrder:
    """Cancel an unfilled order whose timeout has elapsed (call when the tape is
    quiet — no through-trade arrived in time). Idempotent once resolved."""
    if not order.resolved and now_ts - order.post_ts > order.timeout_secs:
        order.cancelled = True
    return order
