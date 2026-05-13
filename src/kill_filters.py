"""
Kill filters — hard gates checked before any entry.

All 8 filters from the strategy spec. Each filter is independent and
checked in priority order. Returns (is_killed, reason) on the FIRST
failure. Returns (False, '') if all pass.

Filters:
  1. Funding extreme:    |funding_rate| > 0.001 per 8h
  2. Low liquidity:      UTC 01:00–03:00
  3. Spread too wide:    spread > 3× 24h median
  4. Thin book:          top-5 depth < 20% of 1h average
  5. WS stale:           last price update > 5 seconds ago
  6. Whale print:        single candle volume > 10× 20-bar average
  7. Daily loss:         account daily_pnl_pct < -0.02
  8. Weekend:            Saturday or Sunday (UTC)

Usage:
    kill_state = KillFilterState()
    is_killed, reason = kill_state.check(
        symbol=symbol, bids=bids, asks=asks,
        last_price_time=timestamp, candle_volume=vol,
        volume_sma20=vol_sma, funding_opportunities=opps,
        daily_pnl_pct=pnl_pct
    )
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Filter thresholds (from spec)
_FUNDING_EXTREME_THRESH   = 0.001      # |rate| per 8h
_LOW_LIQ_HOUR_START       = 1          # UTC hour (inclusive)
_LOW_LIQ_HOUR_END         = 3          # UTC hour (exclusive)
_SPREAD_MULTIPLE          = 3.0        # spread > N × median → kill
_DEPTH_RATIO_MIN          = 0.20       # top-5 depth must be ≥ 20% of 1h avg
_WS_STALE_SECONDS         = 5.0        # last price older than this → kill
_WHALE_VOLUME_MULTIPLE    = 10.0       # volume > N × SMA20 → kill
_DAILY_LOSS_THRESHOLD     = -0.02      # daily PnL pct < this → kill

# Rolling history lengths for tracking
_SPREAD_HISTORY_WINDOW    = 1440       # 24h at 1m candles = 1440 entries
_DEPTH_HISTORY_WINDOW     = 60         # 1h at 1m candles = 60 entries


def _compute_spread(bids: list, asks: list) -> float:
    """Return bid-ask spread in price units. Returns 0 if book is empty."""
    if not bids or not asks:
        return 0.0
    best_bid = float(bids[0][0]) if len(bids[0]) >= 1 else 0.0
    best_ask = float(asks[0][0]) if len(asks[0]) >= 1 else 0.0
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return 0.0
    return best_ask - best_bid


def _compute_top5_depth(bids: list, asks: list) -> float:
    """Return total volume in top-5 bid + top-5 ask levels."""
    bid_vol = sum(float(row[1]) for row in bids[:5] if len(row) >= 2)
    ask_vol = sum(float(row[1]) for row in asks[:5] if len(row) >= 2)
    return bid_vol + ask_vol


def _median(values: list) -> float:
    """Compute median of a list of floats."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    n = len(sorted_v)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_v[mid - 1] + sorted_v[mid]) / 2.0
    return sorted_v[mid]


class KillFilterState:
    """
    Maintains rolling state for filters that require historical tracking
    (spread median, depth average). One instance per symbol is recommended,
    or share a single instance and pass symbol for per-symbol tracking.

    Also exposes the public check() method which runs all 8 filters.
    """

    def __init__(self):
        # Per-symbol rolling histories
        self._spread_history: dict  = {}   # symbol → deque of spread values
        self._depth_history:  dict  = {}   # symbol → deque of top-5 depth values

    def _ensure_symbol(self, symbol: str):
        if symbol not in self._spread_history:
            self._spread_history[symbol] = deque(maxlen=_SPREAD_HISTORY_WINDOW)
        if symbol not in self._depth_history:
            self._depth_history[symbol] = deque(maxlen=_DEPTH_HISTORY_WINDOW)

    def update_book_history(self, symbol: str, bids: list, asks: list):
        """
        Update rolling spread and depth history from a fresh order book.
        Call this every time you fetch the order book for a symbol.
        """
        self._ensure_symbol(symbol)
        spread = _compute_spread(bids, asks)
        depth  = _compute_top5_depth(bids, asks)

        if spread > 0:
            self._spread_history[symbol].append(spread)
        if depth > 0:
            self._depth_history[symbol].append(depth)

    def get_spread_median(self, symbol: str) -> float:
        """24h median spread for symbol. Returns 0 if not enough history."""
        history = self._spread_history.get(symbol, deque())
        return _median(list(history)) if len(history) >= 5 else 0.0

    def get_depth_1h_avg(self, symbol: str) -> float:
        """1h average top-5 depth for symbol. Returns 0 if not enough history."""
        history = self._depth_history.get(symbol, deque())
        if not history:
            return 0.0
        return sum(history) / len(history)

    def check(self,
              symbol:                str,
              bids:                  list,
              asks:                  list,
              last_price_time:       float,
              candle_volume:         float,
              volume_sma20:          float,
              funding_opportunities: list,
              daily_pnl_pct:         float) -> Tuple[bool, str]:
        """
        Run all 8 kill filters in priority order.

        Updates rolling state for spread/depth before checking.

        Args:
            symbol:                trading pair (e.g. 'BTC/USD')
            bids:                  [[price, size], ...] current order book bids
            asks:                  [[price, size], ...] current order book asks
            last_price_time:       unix timestamp of last WS price update
            candle_volume:         current candle volume
            volume_sma20:          20-bar volume SMA
            funding_opportunities: list of dicts from state.json funding data
            daily_pnl_pct:         today's account PnL as fraction (e.g. -0.015)

        Returns:
            (True, reason_string) if any filter triggered → SKIP TRADE
            (False, '')           if all filters passed → OK TO TRADE
        """
        # Update rolling state first
        self.update_book_history(symbol, bids, asks)

        now_utc = datetime.now(timezone.utc)

        # ── Filter 1: Funding rate extreme ────────────────────────────────────
        funding_rate = _extract_funding_rate(symbol, funding_opportunities)
        if funding_rate is not None and abs(funding_rate) > _FUNDING_EXTREME_THRESH:
            return True, f"FUNDING_EXTREME: rate={funding_rate:.5f}/8h (>{_FUNDING_EXTREME_THRESH})"

        # ── Filter 2: Low liquidity window ────────────────────────────────────
        utc_hour = now_utc.hour
        if _LOW_LIQ_HOUR_START <= utc_hour < _LOW_LIQ_HOUR_END:
            return True, f"LOW_LIQUIDITY_WINDOW: UTC {utc_hour:02d}:xx (dead hours)"

        # ── Filter 3: Spread too wide ─────────────────────────────────────────
        current_spread = _compute_spread(bids, asks)
        spread_median  = self.get_spread_median(symbol)
        if spread_median > 0 and current_spread > 0:
            spread_ratio = current_spread / spread_median
            if spread_ratio > _SPREAD_MULTIPLE:
                return True, (
                    f"SPREAD_TOO_WIDE: {current_spread:.4f} = "
                    f"{spread_ratio:.1f}× median {spread_median:.4f}"
                )

        # ── Filter 4: Thin book ───────────────────────────────────────────────
        current_depth = _compute_top5_depth(bids, asks)
        depth_1h_avg  = self.get_depth_1h_avg(symbol)
        if depth_1h_avg > 0 and current_depth > 0:
            depth_ratio = current_depth / depth_1h_avg
            if depth_ratio < _DEPTH_RATIO_MIN:
                return True, (
                    f"THIN_BOOK: depth={current_depth:.2f} = "
                    f"{depth_ratio:.1%} of 1h avg {depth_1h_avg:.2f}"
                )

        # ── Filter 5: WS stale ────────────────────────────────────────────────
        ws_age = time.time() - last_price_time if last_price_time > 0 else 999.0
        if ws_age > _WS_STALE_SECONDS:
            return True, f"WS_STALE: last price {ws_age:.1f}s ago (>{_WS_STALE_SECONDS}s)"

        # ── Filter 6: Whale print ─────────────────────────────────────────────
        if volume_sma20 > 0 and candle_volume > volume_sma20 * _WHALE_VOLUME_MULTIPLE:
            vol_ratio = candle_volume / volume_sma20
            return True, (
                f"WHALE_PRINT: volume={candle_volume:.2f} = "
                f"{vol_ratio:.1f}× SMA20 {volume_sma20:.2f}"
            )

        # ── Filter 7: Daily loss ──────────────────────────────────────────────
        if daily_pnl_pct < _DAILY_LOSS_THRESHOLD:
            return True, f"DAILY_LOSS: pnl={daily_pnl_pct:.2%} (<{_DAILY_LOSS_THRESHOLD:.0%})"

        # ── Filter 8: Weekend ─────────────────────────────────────────────────
        # weekday(): Monday=0, ..., Friday=4, Saturday=5, Sunday=6
        dow = now_utc.weekday()
        if dow >= 5:
            day_name = "Saturday" if dow == 5 else "Sunday"
            return True, f"WEEKEND: {day_name} UTC — liquidity too thin"

        # All filters passed
        return False, ''


def _extract_funding_rate(symbol: str, funding_opportunities: list) -> Optional[float]:
    """
    Extract funding rate for a symbol from the state.json funding_opportunities list.

    Mapping: 'BTC/USD' → 'BTCUSDT', etc.
    Returns rate as fraction per 8h (e.g. 0.0001 = 0.01% per 8h).
    """
    if not funding_opportunities:
        return None

    _SYMBOL_MAP = {
        'BTC/USD': 'BTCUSDT',
        'ETH/USD': 'ETHUSDT',
        'SOL/USD': 'SOLUSDT',
    }
    target = _SYMBOL_MAP.get(symbol, symbol.replace('/', ''))

    for opp in funding_opportunities:
        if not isinstance(opp, dict):
            continue
        sym = opp.get('symbol', '')
        if sym == target or sym == symbol:
            # rate_8h is stored as percentage (e.g. 0.01 = 0.01%)
            raw = opp.get('rate_8h', None)
            if raw is not None:
                # Convert from percentage to decimal
                return float(raw) / 100.0
    return None


# ── Module-level convenience function (matches spec interface) ─────────────────

def check_kill_filters(
    symbol:                str,
    bids:                  list,
    asks:                  list,
    spread_median_24h:     float,
    depth_1h_avg:          float,
    last_price_time:       float,
    candle_volume:         float,
    volume_sma20:          float,
    funding_opportunities: list,
    daily_pnl_pct:         float,
    kill_state:            Optional['KillFilterState'] = None,
) -> Tuple[bool, str]:
    """
    Stateless convenience wrapper. Uses provided spread_median_24h and
    depth_1h_avg instead of internal rolling history.

    Prefer using KillFilterState.check() directly for proper rolling-window
    tracking. This function is provided for callers that track their own history.
    """
    now_utc = datetime.now(timezone.utc)

    # Filter 1: Funding extreme
    funding_rate = _extract_funding_rate(symbol, funding_opportunities)
    if funding_rate is not None and abs(funding_rate) > _FUNDING_EXTREME_THRESH:
        return True, f"FUNDING_EXTREME: rate={funding_rate:.5f}"

    # Filter 2: Low liquidity window
    utc_hour = now_utc.hour
    if _LOW_LIQ_HOUR_START <= utc_hour < _LOW_LIQ_HOUR_END:
        return True, f"LOW_LIQUIDITY_WINDOW: UTC {utc_hour:02d}:xx"

    # Filter 3: Spread too wide
    current_spread = _compute_spread(bids, asks)
    if spread_median_24h > 0 and current_spread > spread_median_24h * _SPREAD_MULTIPLE:
        return True, f"SPREAD_TOO_WIDE: {current_spread:.4f} = {current_spread/spread_median_24h:.1f}× median"

    # Filter 4: Thin book
    current_depth = _compute_top5_depth(bids, asks)
    if depth_1h_avg > 0 and current_depth < depth_1h_avg * _DEPTH_RATIO_MIN:
        return True, f"THIN_BOOK: depth={current_depth:.2f} = {current_depth/depth_1h_avg:.1%} of avg"

    # Filter 5: WS stale
    ws_age = time.time() - last_price_time if last_price_time > 0 else 999.0
    if ws_age > _WS_STALE_SECONDS:
        return True, f"WS_STALE: {ws_age:.1f}s"

    # Filter 6: Whale print
    if volume_sma20 > 0 and candle_volume > volume_sma20 * _WHALE_VOLUME_MULTIPLE:
        return True, f"WHALE_PRINT: {candle_volume/volume_sma20:.1f}× SMA20"

    # Filter 7: Daily loss
    if daily_pnl_pct < _DAILY_LOSS_THRESHOLD:
        return True, f"DAILY_LOSS: {daily_pnl_pct:.2%}"

    # Filter 8: Weekend
    if now_utc.weekday() >= 5:
        return True, "WEEKEND"

    return False, ''
