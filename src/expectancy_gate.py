"""
Expectancy gate — size discipline for the directional engine.

The directional scalper has no *proven* edge: the May-2026 journal audit found
gross pre-cost expectancy ≈ 0 and a 0.9% win rate. Until a given entry path
demonstrates POSITIVE GROSS expectancy over a meaningful sample, its trades are
capped to a small "probe" size — the bot keeps gathering evidence without sizing
up into an unproven (or negative) edge. Once a path proves out, the cap lifts and
the ProbabilityGate's conviction-tier sizing ($25–$75) applies in full.

Why GROSS (pre-cost) is the bar:
  * gross ≤ 0  → the signal has no directional edge at all; never size up.
  * gross > 0 but net < 0 → the edge is real but cost/execution eats it; the
    distinction tells you where to look, but it stays probe-only until net works.

Only records at or above the current probability-model version (see
calibration.MIN_MODEL_VERSION) count, so the legacy broken-regime trades neither
condemn a path forever nor count as proof. Pairs with [[directional_cost_bleed_fix]].
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ENABLED     = os.getenv("EXPECTANCY_GATE_ENABLED", "1") == "1"
MIN_TRADES  = int(os.getenv("EXPECTANCY_MIN_TRADES", "30"))     # proof requires ≥ this many
PROBE_USD   = float(os.getenv("EXPECTANCY_PROBE_USD", "5.0"))   # cap while unproven (= scalp tier)
REFIT_EVERY = int(os.getenv("EXPECTANCY_REFIT_EVERY", "10"))    # recompute after this many new records

try:
    from .calibration import MIN_MODEL_VERSION as _MIN_VER
except Exception:                                   # pragma: no cover - import-order safety
    _MIN_VER = int(os.getenv("CALIB_MIN_MODEL_VERSION", "2"))


class ExpectancyGate:
    """Per-path gross-expectancy tracker that returns a USD size cap.

    Usage:
        gate = ExpectancyGate()
        gate.update(journal, force=True)        # at startup
        ...
        gate.update(journal)                    # cheap; recomputes only on growth
        cap = gate.cap_for(entry_path)          # None = proven (uncapped), else USD cap
    """

    def __init__(self, min_trades: int = MIN_TRADES, probe_usd: float = PROBE_USD,
                 refit_every: int = REFIT_EVERY, min_model_version: int = _MIN_VER):
        self.min_trades = min_trades
        self.probe_usd = probe_usd
        self.refit_every = refit_every
        self.min_model_version = min_model_version
        # entry_path -> (proven, n, gross_mean, net_mean)
        self._stats: Dict[str, Tuple[bool, int, float, float]] = {}
        self._n_seen = 0

    def update(self, journal, force: bool = False) -> bool:
        """Recompute per-path stats. Gated on journal growth unless ``force``.
        Returns True if a recompute ran."""
        records = getattr(journal, 'records', [])
        if not force and len(records) - self._n_seen < self.refit_every:
            return False
        self._n_seen = len(records)

        agg: Dict[str, list] = {}
        for r in records:
            if int(getattr(r, 'prob_model_version', 0)) < self.min_model_version:
                continue
            path = getattr(r, 'entry_path', 'main') or 'main'
            pnl = float(getattr(r, 'pnl', 0.0))
            gross = pnl + float(getattr(r, 'fees_paid', 0.0)) + float(getattr(r, 'slippage_cost', 0.0))
            agg.setdefault(path, []).append((gross, pnl))

        stats: Dict[str, Tuple[bool, int, float, float]] = {}
        for path, rows in agg.items():
            n = len(rows)
            gross_mean = sum(g for g, _ in rows) / n
            net_mean = sum(p for _, p in rows) / n
            proven = n >= self.min_trades and gross_mean > 0.0
            stats[path] = (proven, n, gross_mean, net_mean)
        self._stats = stats
        return True

    def cap_for(self, entry_path: str) -> Optional[float]:
        """Return a USD size cap for this path, or None if it is proven (uncapped)."""
        if not ENABLED:
            return None
        st = self._stats.get(entry_path or 'main')
        if st is None:
            return self.probe_usd          # no model-current data for this path → probe
        proven = st[0]
        return None if proven else self.probe_usd

    def status(self) -> str:
        if not self._stats:
            return "ExpectancyGate: no model-current data yet — all paths probe-only"
        parts = [
            f"{path}:{'PROVEN' if proven else 'probe'}(n={n} gross={gross:+.4f}/trade)"
            for path, (proven, n, gross, _net) in sorted(self._stats.items())
        ]
        return "ExpectancyGate: " + "  ".join(parts)
