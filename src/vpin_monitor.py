"""
VPIN — Volume-Synchronized Probability of Informed Trading.

Easley, Lopez de Prado & O'Hara (2012). A real-time order-flow toxicity metric:
how much of the recent volume came from one side (informed) versus the other.
When toxicity is high, market-takers get adversely selected — exactly the kind
of trade the bot should refuse.

Algorithm (volume-bucketed, NOT time-bucketed):
  1. Stream trades as (price, qty, side="buy"|"sell"). Side is the TAKER side.
  2. Pack qty into fixed-size volume buckets of `bucket_volume` units.
  3. For each closed bucket, record (buy_vol, sell_vol).
  4. VPIN = mean(|buy_vol - sell_vol|) / bucket_volume   over the last N buckets.

Interpretation (from the crypto-specific literature):
  VPIN < 0.45 → balanced flow, safe to act
  0.45–0.55  → mildly skewed
  0.55–0.70  → toxic — refuse new entries here
  > 0.70     → severely toxic, cascade risk

Per-symbol bucket sizing matters. Defaults are calibrated for liquid crypto
majors on Kraken; tune via env per pair if needed.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


# ── Tunables ──────────────────────────────────────────────────────────────────

# Bucket size in BASE units (e.g. BTC). Calibrated so a liquid major fills
# ~1000 buckets/day. Per-symbol override via VPIN_BUCKET_<BASE> env (e.g.
# VPIN_BUCKET_BTC=0.5 for finer buckets on BTC).
_DEFAULT_BUCKETS = {
    "BTC": float(os.getenv("VPIN_BUCKET_BTC", "0.5")),    # ~$30K/bucket at $60K
    "ETH": float(os.getenv("VPIN_BUCKET_ETH", "10.0")),   # ~$30K/bucket at $3K
    "SOL": float(os.getenv("VPIN_BUCKET_SOL", "150.0")),  # ~$30K/bucket at $200
}
_FALLBACK_BUCKET = float(os.getenv("VPIN_BUCKET_DEFAULT", "1.0"))

# Number of closed buckets to average over. 50 is the canonical literature value.
N_BUCKETS = int(os.getenv("VPIN_WINDOW", "50"))

# Threshold for "toxic" — the entry checklist veto. 0.55 is the conservative
# pick: research literature flags 0.55+ as actionable warning, 0.70+ as
# cascade-imminent. Override via VPIN_TOXIC_THRESHOLD.
TOXIC_THRESHOLD = float(os.getenv("VPIN_TOXIC_THRESHOLD", "0.55"))


def _bucket_size_for(symbol: str) -> float:
    base = symbol.split("/")[0].upper()
    return _DEFAULT_BUCKETS.get(base, _FALLBACK_BUCKET)


@dataclass
class _SymbolState:
    bucket_volume: float
    window:   int = N_BUCKETS   # rolling window size for `closed`
    buy_acc:  float = 0.0       # accumulating buy qty in the open bucket
    sell_acc: float = 0.0       # accumulating sell qty in the open bucket
    closed:   Deque = field(default_factory=deque)
    last_vpin: Optional[float] = None
    n_trades: int = 0

    def __post_init__(self) -> None:
        # default_factory above can't see `window`, so size the deque here.
        self.closed = deque(self.closed, maxlen=self.window)


class VPINMonitor:
    """Computes per-symbol VPIN from a stream of (price, qty, side) trade ticks.

    Wire as the `on_trade` callback on KrakenTradeFeed:
        vpin = VPINMonitor()
        feed = KrakenTradeFeed(symbols, on_trade=vpin.on_trade)

    Then read with vpin.current(symbol) or vpin.is_toxic(symbol).
    """

    def __init__(self, window: int = N_BUCKETS, threshold: float = TOXIC_THRESHOLD):
        self.window    = window
        self.threshold = threshold
        self._state: Dict[str, _SymbolState] = {}

    # ── ingest ─────────────────────────────────────────────────────────────
    def on_trade(self, tick) -> None:
        """Accept a TradeTick (duck-typed: needs .symbol, .qty, .side)."""
        sym = getattr(tick, "symbol", None)
        if not sym:
            return
        try:
            qty  = float(getattr(tick, "qty", 0.0))
            side = str(getattr(tick, "side", "")).lower()
        except Exception:
            return
        if qty <= 0 or side not in ("buy", "sell"):
            return

        st = self._state.get(sym)
        if st is None:
            st = _SymbolState(bucket_volume=_bucket_size_for(sym), window=self.window)
            self._state[sym] = st

        st.n_trades += 1
        remaining = qty
        # A single trade may span multiple buckets if its qty exceeds bucket_volume;
        # split it across buckets so each closed bucket carries exactly bucket_volume
        # of total volume (the spec requirement).
        while remaining > 0:
            cur_filled = st.buy_acc + st.sell_acc
            room       = st.bucket_volume - cur_filled
            take       = min(remaining, room)
            if side == "buy":
                st.buy_acc  += take
            else:
                st.sell_acc += take
            remaining -= take
            # Close the bucket if it just filled
            if (st.buy_acc + st.sell_acc) >= st.bucket_volume - 1e-12:
                st.closed.append((st.buy_acc, st.sell_acc))
                st.buy_acc = 0.0
                st.sell_acc = 0.0
                # Recompute VPIN over the last N closed buckets
                if len(st.closed) >= 5:   # need a few buckets before reporting
                    imbalance = sum(abs(b - s) for b, s in st.closed)
                    total_vol = st.bucket_volume * len(st.closed)
                    st.last_vpin = imbalance / total_vol if total_vol > 0 else None

    # ── read ───────────────────────────────────────────────────────────────
    def current(self, symbol: str) -> Optional[float]:
        """Current VPIN reading for symbol, or None if not enough data yet."""
        st = self._state.get(symbol)
        return st.last_vpin if st else None

    def is_toxic(self, symbol: str) -> bool:
        """True if VPIN currently exceeds the toxic threshold. False on no data
        (warm-up window) — we don't block trades just because data is missing."""
        v = self.current(symbol)
        return v is not None and v > self.threshold

    def n_buckets(self, symbol: str) -> int:
        st = self._state.get(symbol)
        return len(st.closed) if st else 0

    def snapshot(self) -> Dict[str, dict]:
        """Diagnostic snapshot of every tracked symbol — for the funnel log."""
        out = {}
        for sym, st in self._state.items():
            out[sym] = {
                "vpin":     st.last_vpin,
                "buckets":  len(st.closed),
                "trades":   st.n_trades,
                "toxic":    self.is_toxic(sym),
            }
        return out
