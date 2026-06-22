"""
Crypto Implied Volatility Monitor
Fetches ATM implied volatility from Deribit's free public API.
No API key required.

Uses IV percentile rank to scale position sizing:
  - IV percentile > 75  → reduce position size (expensive options = expected big move)
  - IV percentile < 25  → increase confidence (calm market, trends more reliable)
  - Term structure inverted (near IV > far IV) → stress signal, pause longs

Data refreshed every 15 minutes (Deribit rate limits generously).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_DERIBIT_OPTIONS = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
_DERIBIT_INDEX   = "https://www.deribit.com/api/v2/public/get_index_price"
_REFRESH_SECS    = 900   # 15 minutes
_IV_HISTORY_LEN  = 96    # keep 24h of 15-min samples for percentile

_MONTHS = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
           'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
_EXPIRY_RE = re.compile(r'^(\d{1,2})([A-Z]{3})(\d{2})$')


def _parse_expiry(expiry_str: str) -> Optional[date]:
    """Parse Deribit's 'DDMonYY' expiry token (e.g. '29APR26') into a real date.

    A plain string sort of these tokens orders by day-of-month first, not by
    year/month — e.g. '5JAN27' < '29APR26' alphabetically despite being later.
    Returns None for an unparseable token so the caller can sort it last
    (treat as far-future rather than risk it being picked as the near expiry).
    """
    m = _EXPIRY_RE.match(expiry_str)
    if not m:
        return None
    day, mon, yy = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        return date(2000 + int(yy), month, int(day))
    except ValueError:
        return None


@dataclass
class IVSnapshot:
    symbol:         str
    atm_iv:         float    # current ATM implied vol (annualised %)
    iv_percentile:  float    # 0–100, rank vs last 24h
    term_structure: str      # NORMAL | INVERTED | FLAT
    near_iv:        float    # nearest expiry ATM IV
    far_iv:         float    # next expiry ATM IV
    spot_price:     float
    fetched_at:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def signal(self) -> str:
        if self.iv_percentile > 80:
            return 'EXTREME'
        if self.iv_percentile > 65:
            return 'HIGH'
        if self.iv_percentile < 20:
            return 'LOW'
        return 'NORMAL'

    @property
    def position_size_multiplier(self) -> float:
        """Scale factor for position sizing based on IV level."""
        if self.iv_percentile > 80:
            return 0.4    # 40% of normal size — very uncertain
        if self.iv_percentile > 65:
            return 0.65
        if self.term_structure == 'INVERTED':
            return 0.5    # term inversion = stress
        if self.iv_percentile < 20:
            return 1.2    # calm market — can size up slightly
        return 1.0

    def color(self) -> str:
        return {'EXTREME': '#ff1744', 'HIGH': '#ff9500',
                'LOW': '#00f5a0', 'NORMAL': '#4d9fff'}.get(self.signal, '#aaa')

    def to_dict(self) -> dict:
        return {
            'symbol':        self.symbol,
            'atm_iv':        round(self.atm_iv, 1),
            'iv_percentile': round(self.iv_percentile, 1),
            'term_structure': self.term_structure,
            'near_iv':       round(self.near_iv, 1),
            'far_iv':        round(self.far_iv, 1),
            'signal':        self.signal,
            'size_mult':     self.position_size_multiplier,
            'color':         self.color(),
            'spot_price':    self.spot_price,
            'fetched_at':    self.fetched_at.isoformat(),
        }


class CryptoVolMonitor:
    """
    Polls Deribit for BTC and ETH implied volatility surfaces.
    Extracts ATM IV from near-term options and tracks a 24h percentile.
    """

    def __init__(self):
        self._snapshots:  Dict[str, IVSnapshot] = {}
        self._iv_history: Dict[str, List[float]] = {'BTC': [], 'ETH': []}
        self._last_fetch: float = 0.0
        self._running = False

    def get_snapshot(self, symbol: str) -> Optional[IVSnapshot]:
        key = symbol.split('/')[0]
        return self._snapshots.get(key)

    def get_size_multiplier(self, symbol: str) -> float:
        snap = self.get_snapshot(symbol)
        return snap.position_size_multiplier if snap else 1.0

    async def start(self):
        self._running = True
        logger.info("[IV] CryptoVolMonitor starting")
        await self._refresh()
        while self._running:
            await asyncio.sleep(10)
            if time.monotonic() - self._last_fetch >= _REFRESH_SECS:
                await self._refresh()

    def stop(self):
        self._running = False

    async def _refresh(self):
        await asyncio.gather(
            self._fetch_currency('BTC'),
            self._fetch_currency('ETH'),
            return_exceptions=True,
        )
        self._last_fetch = time.monotonic()

    async def _fetch_currency(self, currency: str):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                # Get spot price
                async with session.get(_DERIBIT_INDEX,
                                       params={'index_name': f'{currency.lower()}_usd'}) as r:
                    idx = await r.json(content_type=None)
                    spot = float(idx['result']['index_price'])

                # Get all options
                async with session.get(_DERIBIT_OPTIONS,
                                       params={'currency': currency, 'kind': 'option'}) as r:
                    data = await r.json(content_type=None)

                options = data.get('result', [])
                if not options:
                    return

                # Parse expiry timestamps and find nearest two expiries
                from collections import defaultdict
                by_expiry = defaultdict(list)
                for opt in options:
                    name = opt.get('instrument_name', '')
                    if not name:
                        continue
                    parts = name.split('-')
                    if len(parts) < 4:
                        continue
                    expiry_str = parts[1]   # e.g. '29APR26'
                    by_expiry[expiry_str].append(opt)

                if len(by_expiry) < 2:
                    return

                expiry_keys = sorted(by_expiry.keys(),
                                     key=lambda k: _parse_expiry(k) or date.max)
                near_iv = self._atm_iv(by_expiry[expiry_keys[0]], spot)
                far_iv  = self._atm_iv(by_expiry[expiry_keys[1]], spot)

                if near_iv is None:
                    return

                atm_iv = near_iv

                # Update history and calculate percentile
                hist = self._iv_history[currency]
                hist.append(atm_iv)
                if len(hist) > _IV_HISTORY_LEN:
                    hist.pop(0)

                if len(hist) >= 5:
                    import numpy as np
                    pct = float(np.sum(np.array(hist[:-1]) <= atm_iv) / len(hist[:-1]) * 100)
                else:
                    pct = 50.0

                # Term structure
                if far_iv is None:
                    term = 'FLAT'
                elif near_iv > far_iv * 1.05:
                    term = 'INVERTED'
                elif near_iv < far_iv * 0.95:
                    term = 'NORMAL'
                else:
                    term = 'FLAT'

                snap = IVSnapshot(
                    symbol=currency,
                    atm_iv=atm_iv,
                    iv_percentile=pct,
                    term_structure=term,
                    near_iv=near_iv,
                    far_iv=far_iv if far_iv else near_iv,
                    spot_price=spot,
                )
                self._snapshots[currency] = snap
                logger.info(
                    f"[IV] {currency}: ATM IV={atm_iv:.1f}% "
                    f"pct={pct:.0f} term={term} signal={snap.signal}"
                )

        except Exception as e:
            logger.warning(f"[IV] {currency} fetch failed: {e}")

    def _atm_iv(self, options: list, spot: float) -> Optional[float]:
        """Find the ATM call IV closest to spot price."""
        calls = [o for o in options
                 if o.get('instrument_name', '').endswith('-C')
                 and o.get('mark_iv') and o.get('mark_iv') > 0]
        if not calls:
            return None

        # Find call with strike closest to spot
        def strike(o):
            try:
                return float(o['instrument_name'].split('-')[2])
            except Exception:
                return float('inf')

        closest = min(calls, key=lambda o: abs(strike(o) - spot))
        return float(closest['mark_iv'])

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self._snapshots.items()}
