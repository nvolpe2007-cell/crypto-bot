"""
Funding-rate history tracker + persistence metrics.

The funding scanner only ever reports a CURRENT snapshot. That's blind to the
one thing that actually determines whether a funding-arb entry makes money:
does the funding PERSIST long enough to clear the round-trip cost, or does it
flip back to ~0 within a cycle (the "cycle-0 flip" that bled the Kraken arm —
see memory funding_arb_kraken_bleed)?

This module accumulates per-symbol funding observations over time and derives:
  • consecutive_positive_cycles — how long funding has held the SAME positive
    sign without interruption (in 8h funding cycles), the key entry signal.
  • flip_count — how many times the sign changed within the retention window,
    i.e. how "twitchy" / untrustworthy a symbol's funding is.

It is sign-agnostic in storage but the persistence query is positive-only,
matching the conservative cash-and-carry arms (long spot / short perp on
positive funding — no borrow risk).

Persisted to data/funding_history.json. Lives in data/ (untracked), so it
survives deploys/restarts and keeps accumulating.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('data/funding_history.json')
FUNDING_CYCLE_HOURS = 8.0


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class FundingHistory:
    """Per-symbol funding observation log with persistence metrics.

    Samples are downsampled to at most one per `sample_interval` and pruned to
    `retention`. A symbol key is the scanner's "Exchange:SYMBOL" so the same
    base asset on different venues never collide.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        retention_days: float = float(os.getenv('FUNDING_HISTORY_RETENTION_DAYS', '30')),
        sample_interval_min: float = float(os.getenv('FUNDING_HISTORY_SAMPLE_MIN', '60')),
        max_gap_hours: float = float(os.getenv('FUNDING_HISTORY_MAX_GAP_HOURS', '3')),
        save_interval_sec: float = float(os.getenv('FUNDING_HISTORY_SAVE_SEC', '600')),
    ):
        self.path = path or DEFAULT_PATH
        self.retention = timedelta(days=retention_days)
        self.sample_interval = timedelta(minutes=sample_interval_min)
        self.max_gap = timedelta(hours=max_gap_hours)
        self.save_interval = timedelta(seconds=save_interval_sec)
        # symbol_key -> list of (iso_ts, apy), ascending by time
        self.samples: Dict[str, List[Tuple[str, float]]] = {}
        self._last_save: Optional[datetime] = None
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.samples = {
                k: [(t, float(a)) for t, a in v]
                for k, v in data.get('samples', {}).items()
            }
            n = sum(len(v) for v in self.samples.values())
            logger.info(f"[FundingHistory] loaded {len(self.samples)} symbols, {n} samples")
        except Exception as e:
            logger.warning(f"[FundingHistory] load failed: {e}")

    def save(self):
        try:
            self.path.parent.mkdir(exist_ok=True)
            payload = {'samples': {k: [[t, a] for t, a in v] for k, v in self.samples.items()}}
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.path)
            self._last_save = datetime.now(timezone.utc)
        except Exception as e:
            logger.warning(f"[FundingHistory] save failed: {e}")

    def maybe_save(self, now: Optional[datetime] = None):
        """Throttled save — writes at most once per save_interval."""
        now = now or datetime.now(timezone.utc)
        if self._last_save is None or (now - self._last_save) >= self.save_interval:
            self.save()

    # ── recording ──────────────────────────────────────────────────────────────

    def record(self, key: str, apy: float, now: datetime):
        s = self.samples.setdefault(key, [])
        # Downsample: skip if the last sample is newer than sample_interval.
        if s and (now - _parse(s[-1][0])) < self.sample_interval:
            return
        s.append((now.isoformat(), float(apy)))
        # Prune anything past the retention horizon.
        cutoff = now - self.retention
        if _parse(s[0][0]) < cutoff:
            self.samples[key] = [(t, a) for t, a in s if _parse(t) >= cutoff]

    def record_many(self, opps: List[dict], now: Optional[datetime] = None):
        """Record every opportunity in a scanner snapshot."""
        now = now or datetime.now(timezone.utc)
        for o in opps:
            try:
                key = f"{o['exchange']}:{o['symbol']}"
                self.record(key, float(o['apy']), now)
            except Exception:
                continue

    # ── metrics ──────────────────────────────────────────────────────────────

    def consecutive_positive_hours(self, key: str, now: Optional[datetime] = None) -> float:
        """Hours funding has held a positive sign continuously, ending at the
        latest sample. A gap larger than max_gap (symbol off scanner) breaks
        continuity — we don't trust a rate we stopped seeing."""
        s = self.samples.get(key)
        if not s or s[-1][1] <= 0:
            return 0.0
        end_ts = _parse(s[-1][0])
        start_ts = end_ts
        prev_ts = end_ts
        for t, a in reversed(s):
            ts = _parse(t)
            if a <= 0:
                break
            if (prev_ts - ts) > self.max_gap:
                break
            start_ts = ts
            prev_ts = ts
        return (end_ts - start_ts).total_seconds() / 3600.0

    def consecutive_positive_cycles(self, key: str, now: Optional[datetime] = None) -> float:
        return self.consecutive_positive_hours(key, now) / FUNDING_CYCLE_HOURS

    def flip_count(self, key: str) -> int:
        """Number of sign changes within the retained window. Non-positive
        funding counts as the negative sign (uncollectable for a positive arm)."""
        s = self.samples.get(key)
        if not s or len(s) < 2:
            return 0
        flips = 0
        prev = 1 if s[0][1] > 0 else -1
        for _, a in s[1:]:
            cur = 1 if a > 0 else -1
            if cur != prev:
                flips += 1
                prev = cur
        return flips

    def is_stable(
        self,
        key: str,
        min_cycles: float,
        max_flips: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """Entry gate: funding has held positive for >= min_cycles AND the
        symbol hasn't flipped more than max_flips times in the retention window."""
        return (self.consecutive_positive_cycles(key, now) >= min_cycles
                and self.flip_count(key) <= max_flips)

    def stats(self, key: str, now: Optional[datetime] = None) -> dict:
        return {
            'samples': len(self.samples.get(key, [])),
            'consec_cycles': round(self.consecutive_positive_cycles(key, now), 2),
            'flips_30d': self.flip_count(key),
        }
