"""
Offline order-flow indicators — pure, vectorised, stateless.

WHAT THIS IS FOR, AND WHAT IT IS NOT
────────────────────────────────────
This module is a **measurement instrument**, not a strategy and not a signal
generator. It computes order-flow quantities over *historical* data and returns
plain pandas Series. It is deliberately not wired into any live path, emits no
BUY/SELL, and applies no thresholds.

That framing is not decoration — it is the point. `research/hypothesis_registry.yaml`
records `scalper-microstructure-ofi-v2` as **paper-only** behind a corpse: 228
trades, 0.9% win rate, -$20.14 net, 73.6% fee drag. Its deployment gate reads
"requires: positive OOS edge from non-loosened gates on fresh data". Adding
another order-flow *trigger* would re-propose a hypothesis the ledger has already
priced. Adding the means to *measure* one is the thing the gate actually asks for.

WHY IT DIDN'T ALREADY EXIST
───────────────────────────
The repo has plenty of order-flow machinery — `order_flow.OrderFlowImbalance`,
`ofi_v2.OFICalculatorV2`, `cvd_tracker.CVDTracker`/`TickCVDTracker`,
`orderflow_ws.obi_from_book` — but every one of them is **stateful and
live-feed-shaped**: they take an exchange handle or are fed streaming updates,
hold internal deques, and return the latest scalar. None can be run over a
historical frame, and none persist what they compute.

So the order-flow numbers the live bot sees are computed in memory and thrown
away. There is no recorded tape or book snapshot anywhere under `data/`. That is
the concrete reason the OFI hypothesis cannot clear its own gate: **the OOS
evidence it requires has never been recordable.** These functions are the half of
that problem that can be fixed without new infrastructure — given data, they
score it. A recorder is the other half, and is not in this module.

DATA HONESTY
────────────
Two grades of input are supported, and they are NOT equivalent:

  * **Tick tape** (`signed_volume_from_ticks`, `ofi_from_books`) — real trades
    and real book snapshots. This is the honest input.
  * **Bar proxy** (`bar_signed_volume`) — infers aggressor pressure from OHLCV
    geometry. It is a *proxy*, it is documented as one, and CLAUDE.md records
    that the candle-CVD proxy was replaced by tick CVD precisely because the
    proxy was inadequate. It is provided because bars are the only history that
    currently exists, and a coarse measurement that is labelled coarse beats no
    measurement. Do not report a result from it as if it came from tick data.

References
──────────
OFI follows Cont, Kukanov & Stoikov (2014), "The Price Impact of Order Book
Events". Trade-side inference follows the tick rule (Lee & Ready, 1991).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "tick_rule_side",
    "signed_volume_from_ticks",
    "bar_signed_volume",
    "cumulative_volume_delta",
    "book_imbalance",
    "ofi_from_books",
    "rolling_zscore",
    "cvd_price_divergence",
]


# ── trade-tape primitives ─────────────────────────────────────────────────────

def tick_rule_side(prices: pd.Series) -> pd.Series:
    """
    Infer trade aggressor side from price changes alone (the tick rule).

    +1 when the trade printed above the previous distinct price (buyer-initiated),
    -1 when below, and the previous sign is carried across unchanged prices.
    Leading trades with no established sign yet are 0 — deliberately NOT guessed,
    since a fabricated side at the start of a tape biases every downstream sum.

    This is an inference, not ground truth. When the venue gives you the real
    aggressor flag, pass it instead: `signed_volume_from_ticks(..., side=...)`.
    """
    prices = pd.Series(prices).astype(float)
    direction = np.sign(prices.diff().to_numpy())
    direction[0] = 0.0  # diff() is NaN at the head; np.sign(NaN) is NaN

    # Carry the last non-zero sign forward across flat prints.
    out = pd.Series(direction, index=prices.index).replace(0.0, np.nan)
    out = out.ffill().fillna(0.0)
    return out


def signed_volume_from_ticks(
    trades: pd.DataFrame,
    price_col: str = "price",
    qty_col: str = "qty",
    side_col: Optional[str] = "side",
) -> pd.Series:
    """
    Per-trade signed volume: +qty for buyer-initiated, -qty for seller-initiated.

    Uses `side_col` when present (values 'buy'/'sell', case-insensitive, or
    numeric +1/-1); otherwise falls back to the tick rule. A tape whose side
    column exists but is partly null uses the real side where it has one and the
    tick rule only for the gaps, rather than discarding either source.
    """
    if trades.empty:
        return pd.Series(dtype=float)

    qty = trades[qty_col].astype(float)
    inferred = tick_rule_side(trades[price_col])

    if side_col is not None and side_col in trades.columns:
        raw = trades[side_col]
        if pd.api.types.is_numeric_dtype(raw):
            sign = np.sign(raw.astype(float))
        else:
            lowered = raw.astype("string").str.strip().str.lower()
            sign = pd.Series(np.nan, index=trades.index, dtype=float)
            sign[lowered.isin(["buy", "b", "bid", "buyer"])] = 1.0
            sign[lowered.isin(["sell", "s", "ask", "seller"])] = -1.0
        sign = sign.replace(0.0, np.nan)
        sign = sign.fillna(inferred)
    else:
        sign = inferred

    return (sign.astype(float) * qty).rename("signed_volume")


def bar_signed_volume(df: pd.DataFrame) -> pd.Series:
    """
    PROXY. Estimate signed volume from OHLCV geometry via the close-location
    value: ((C-L) - (H-C)) / (H-L), in [-1, +1], scaled by bar volume.

    A close at the bar high scores +volume, at the low -volume, mid-range 0.
    Doji bars (H == L) score 0 rather than dividing by zero.

    This cannot see intra-bar sequencing and cannot distinguish one large
    aggressor from many small ones. It is a shape heuristic. See DATA HONESTY in
    the module docstring before reporting anything computed from it.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    span = high - low
    clv = ((close - low) - (high - close))
    clv = clv.where(span != 0, 0.0) / span.where(span != 0, 1.0)
    return (clv * volume).rename("signed_volume")


def cumulative_volume_delta(signed_volume: pd.Series) -> pd.Series:
    """Running sum of signed volume (CVD). NaNs are treated as zero flow."""
    return pd.Series(signed_volume).astype(float).fillna(0.0).cumsum().rename("cvd")


# ── book primitives ───────────────────────────────────────────────────────────

def book_imbalance(bid_sizes, ask_sizes) -> pd.Series:
    """
    Top-of-book imbalance in [-1, +1]: (bid - ask) / (bid + ask).

    +1 is all bid, -1 all ask, 0 balanced. An empty book on both sides is 0, not
    NaN — "no resting size" is a real, meaningful state, not missing data.
    """
    bid = pd.Series(bid_sizes).astype(float)
    ask = pd.Series(ask_sizes).astype(float).reindex(bid.index)
    total = bid + ask
    imb = (bid - ask) / total.where(total != 0, np.nan)
    return imb.fillna(0.0).rename("book_imbalance")


def ofi_from_books(
    bid_prices: Sequence[float],
    bid_sizes: Sequence[float],
    ask_prices: Sequence[float],
    ask_sizes: Sequence[float],
) -> pd.Series:
    """
    Order Flow Imbalance between consecutive best-quote snapshots
    (Cont, Kukanov & Stoikov 2014).

    For snapshots n-1 -> n:

        e_n =  1{Pb_n >= Pb_n-1} * qb_n  -  1{Pb_n <= Pb_n-1} * qb_n-1
             - 1{Pa_n <= Pa_n-1} * qa_n  +  1{Pa_n >= Pa_n-1} * qa_n-1

    Reading the cases, which is where the sign conventions earn their keep:
      * bid price UP      -> +qb_n        (bid stepped up: buy pressure)
      * bid price DOWN    -> -qb_n-1      (bid pulled: sell pressure)
      * bid price FLAT    -> qb_n - qb_n-1        (size added/removed at the bid)
      * ask price DOWN    -> -qa_n        (ask stepped down: sell pressure)
      * ask price UP      -> +qa_n-1      (ask lifted/pulled: buy pressure)
      * ask price FLAT    -> qa_n-1 - qa_n        (size removed/added at the ask)

    The first element is 0.0 — there is no prior snapshot to difference against,
    and a leading NaN would silently poison any cumulative sum downstream.
    Sum this over a window to get windowed OFI; it is additive by construction.
    """
    pb = np.asarray(bid_prices, dtype=float)
    qb = np.asarray(bid_sizes, dtype=float)
    pa = np.asarray(ask_prices, dtype=float)
    qa = np.asarray(ask_sizes, dtype=float)

    if not (len(pb) == len(qb) == len(pa) == len(qa)):
        raise ValueError(
            f"book arrays must be equal length, got bid_prices={len(pb)} "
            f"bid_sizes={len(qb)} ask_prices={len(pa)} ask_sizes={len(qa)}"
        )

    n = len(pb)
    e = np.zeros(n, dtype=float)
    if n < 2:
        return pd.Series(e, name="ofi")

    bid_up = pb[1:] >= pb[:-1]
    bid_dn = pb[1:] <= pb[:-1]
    ask_dn = pa[1:] <= pa[:-1]
    ask_up = pa[1:] >= pa[:-1]

    e[1:] = (bid_up * qb[1:] - bid_dn * qb[:-1]
             - ask_dn * qa[1:] + ask_up * qa[:-1])
    return pd.Series(e, name="ofi")


# ── shaping helpers ───────────────────────────────────────────────────────────

def rolling_zscore(series: pd.Series, window: int = 50,
                   min_periods: Optional[int] = None) -> pd.Series:
    """
    Rolling z-score, for putting order-flow quantities of wildly different scale
    (share counts vs notional vs imbalance ratios) on comparable footing.

    A zero-variance window yields 0.0, not +/-inf: a flat window carries no
    information, and an infinity here propagates into every later aggregate.
    """
    s = pd.Series(series).astype(float)
    mp = window if min_periods is None else min_periods
    mean = s.rolling(window, min_periods=mp).mean()
    std = s.rolling(window, min_periods=mp).std(ddof=0)
    z = (s - mean) / std.where(std > 0, np.nan)
    return z.where(std > 0, 0.0).where(mean.notna(), np.nan).rename("zscore")


def cvd_price_divergence(price: pd.Series, cvd: pd.Series,
                         lookback: int = 20) -> pd.Series:
    """
    Sign disagreement between the price change and the CVD change over
    `lookback` bars.

    +1  price fell while cumulative delta rose  (absorption / bullish divergence)
    -1  price rose while cumulative delta fell  (distribution / bearish divergence)
     0  the two agree, or either is flat

    This REPORTS a disagreement; it does not claim one predicts anything. The
    repo's own record (memory `market_structure_signals_verdict`) is that
    intraday order-flow reads like this die at the cost wall.
    """
    p = pd.Series(price).astype(float)
    c = pd.Series(cvd).astype(float).reindex(p.index)

    dp = np.sign(p.diff(lookback))
    dc = np.sign(c.diff(lookback))

    out = pd.Series(0.0, index=p.index)
    out[(dp < 0) & (dc > 0)] = 1.0
    out[(dp > 0) & (dc < 0)] = -1.0
    out[dp.isna() | dc.isna()] = np.nan
    return out.rename("cvd_divergence")
