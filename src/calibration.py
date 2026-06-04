"""
Probability calibration for the ProbabilityGate.

The gate stacks hand-set edge priors (0.52–0.64) into ``combined_p``. Those
priors were never calibrated against real outcomes — probability_gate.py says
so directly: "Tunables (calibrate from trade_journal later)". That "later" is
here. This module closes the loop:

    raw combined_p  ──isotonic fit on journal outcomes──▶  calibrated P(win)

The gate then routes its *reject threshold* and *Kelly sizing* through the
calibrated probability instead of the raw guess. If the stacked 0.62 actually
wins 55% of the time, sizing should reflect 0.55 — otherwise quarter-Kelly is
systematically too large and the min-P gate lets through trades that don't
clear the bar.

Why isotonic regression:
  * Monotonic — a higher raw score must never map to a lower true win rate.
  * Non-parametric — no assumption about the shape of the miscalibration.
  * The standard tool for probability calibration (Zadrozny & Elkan 2002).

Small-sample safety (the literature's explicit <50-trade warning):
  * Stays INACTIVE (identity passthrough) until ``min_samples`` resolved
    trades with a recorded prob_win exist — below that, the empirical win
    rate per score bucket is pure noise.
  * Once active, the calibrated value is *shrunk toward the raw value* with
    weight  w = min(1, n / full_trust_n)  so a 45-trade fit barely moves the
    needle while a 400-trade fit is trusted nearly fully.

Fitting needs scikit-learn; loading/predicting needs only numpy + json, so a
persisted curve survives even if sklearn is unavailable at runtime.

CLI:
    python -m src.calibration            # fit from the live journal + report
    python -m src.calibration --selftest # synthetic check, no journal needed
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

CALIB_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'calibration.json')

# Tunables (env-overridable)
MIN_SAMPLES   = int(os.getenv('CALIB_MIN_SAMPLES', '40'))    # below this → inactive
FULL_TRUST_N  = int(os.getenv('CALIB_FULL_TRUST_N', '200'))  # n at which shrink → 1.0
REFIT_EVERY   = int(os.getenv('CALIB_REFIT_EVERY', '25'))    # refit after this many new records

# Only calibrate on records whose probability-model version is at least this.
# The gate's combiner has changed twice (noisy-OR → log-odds → correlation-shrunk
# log-odds, probability_gate.PROB_MODEL_VERSION), so older prob_win values come
# from a different distribution and would poison the fit (e.g. the May-2026
# audit's 228 trades clustered prob_win ~0.80 at a 0.9% win rate → would map
# everything to reject-all and freeze the gate). Keep this in lock-step with
# PROB_MODEL_VERSION — now v3 (the v3 shrink lowered the combined_p distribution,
# so v2 records are no longer comparable).
MIN_MODEL_VERSION = int(os.getenv('CALIB_MIN_MODEL_VERSION', '3'))

# Output is clipped to the same band the gate's _stack() uses, so calibration
# can never produce a probability the rest of the system treats as impossible.
_P_FLOOR = 0.50
_P_CEIL  = 0.97


@dataclass
class CalibrationReport:
    n: int
    active: bool
    shrink: float = 0.0
    brier_raw: float = 0.0
    brier_cal: float = 0.0
    ece_raw: float = 0.0
    ece_cal: float = 0.0
    # (lo, hi, n_bin, mean_pred, mean_actual) per occupied bin
    bins: List[Tuple[float, float, int, float, float]] = field(default_factory=list)

    def render(self) -> str:
        if not self.active:
            return (f"Calibration INACTIVE - {self.n} resolved trades with prob_win "
                    f"(need {MIN_SAMPLES}). Gate uses raw stacked P until then.")
        lines = [
            f"Calibration ACTIVE on {self.n} trades (shrink={self.shrink:.2f})",
            f"  Brier:  raw {self.brier_raw:.4f}  ->  calibrated {self.brier_cal:.4f}"
            f"  ({'better' if self.brier_cal < self.brier_raw else 'no gain'})",
            f"  ECE:    raw {self.ece_raw:.4f}  ->  calibrated {self.ece_cal:.4f}",
            "  Reliability (raw predicted -> actual win rate, in-sample):",
        ]
        for lo, hi, nb, mp, ma in self.bins:
            bar = '#' * int(round(ma * 20))
            lines.append(f"    [{lo:.2f},{hi:.2f})  n={nb:<4} pred={mp:.2f} actual={ma:.2f} {bar}")
        return "\n".join(lines)


def _brier(pred: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean((pred - actual) ** 2)) if len(pred) else 0.0


def _ece(pred: np.ndarray, actual: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error: |mean_pred - mean_actual| weighted by bin mass."""
    if len(pred) == 0:
        return 0.0
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total = 0.0
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (pred >= lo) & (pred < hi) if i < n_bins - 1 else (pred >= lo) & (pred <= hi)
        if not mask.any():
            continue
        total += abs(pred[mask].mean() - actual[mask].mean()) * (mask.sum() / len(pred))
    return float(total)


def _reliability_bins(pred: np.ndarray, actual: np.ndarray,
                      n_bins: int = 10) -> List[Tuple[float, float, int, float, float]]:
    edges = np.linspace(pred.min(), pred.max(), n_bins + 1) if len(pred) else np.linspace(0, 1, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (pred >= lo) & (pred < hi) if i < n_bins - 1 else (pred >= lo) & (pred <= hi)
        if not mask.any():
            continue
        out.append((float(lo), float(hi), int(mask.sum()),
                    float(pred[mask].mean()), float(actual[mask].mean())))
    return out


class ProbabilityCalibrator:
    """
    Maps the gate's raw stacked probability to a calibrated win probability.

    Lifecycle:
        cal = ProbabilityCalibrator()        # auto-loads persisted curve
        cal.fit_from_journal(journal)         # fit + persist (no-op if too few)
        p = cal.calibrate(raw_p)              # identity until active
        cal.maybe_refit(journal)              # cheap; refits only on growth
    """

    def __init__(self,
                 min_samples: int = MIN_SAMPLES,
                 full_trust_n: int = FULL_TRUST_N,
                 refit_every: int = REFIT_EVERY,
                 path: str = CALIB_FILE):
        self.min_samples = min_samples
        self.full_trust_n = full_trust_n
        self.refit_every = refit_every
        self.path = path

        self._x: Optional[np.ndarray] = None   # isotonic knot x (raw probs)
        self._y: Optional[np.ndarray] = None   # isotonic knot y (calibrated)
        self._n_fit = 0                        # resolved trades used in the fit
        self._shrink = 0.0
        self._n_seen = 0                       # journal size at last refit attempt
        self.load()

    @property
    def is_active(self) -> bool:
        return self._x is not None and self._y is not None and len(self._x) >= 2

    # ── prediction ──────────────────────────────────────────────────────────
    def calibrate(self, raw_p: float) -> float:
        """Map a raw stacked probability to the calibrated win probability."""
        if not self.is_active:
            return raw_p
        cal = float(np.interp(raw_p, self._x, self._y))   # np.interp clamps at the ends
        blended = self._shrink * cal + (1.0 - self._shrink) * raw_p
        return float(min(_P_CEIL, max(_P_FLOOR, blended)))

    # ── fitting ───────────────────────────────────────────────────────────────
    def fit(self, samples: Sequence[Tuple[float, bool]]) -> CalibrationReport:
        """
        Fit isotonic regression on (raw_p, won) pairs.

        Only pairs with raw_p > 0 are usable (raw_p == 0 means the gate didn't
        record a probability for that trade). Deactivates and returns an
        inactive report if there isn't enough resolved data.
        """
        xs = np.array([p for p, _ in samples if p and p > 0.0], dtype=float)
        ys = np.array([1.0 if w else 0.0 for p, w in samples if p and p > 0.0], dtype=float)
        n = len(xs)

        if n < self.min_samples or len(np.unique(xs)) < 2:
            self._deactivate()
            self._n_seen = n
            self.save()
            return CalibrationReport(n=n, active=False)

        try:
            from sklearn.isotonic import IsotonicRegression
        except Exception as e:                       # pragma: no cover - env-dependent
            logger.warning("[CALIB] scikit-learn unavailable, calibration disabled: %s", e)
            self._deactivate()
            return CalibrationReport(n=n, active=False)

        # Fit on QUANTILE-BINNED means rather than raw points. Per-point isotonic
        # overfits at the extremes (a run of wins at the top pins the last knot
        # near 1.0); binning averages each region over ~20 trades so the boundary
        # knots are the stable empirical win rate of that bucket, not a spike.
        n_bins = min(15, max(3, n // 20))
        q_edges = np.unique(np.quantile(xs, np.linspace(0.0, 1.0, n_bins + 1)))
        if len(q_edges) >= 3:
            bx, by, bw = [], [], []
            for i in range(len(q_edges) - 1):
                lo, hi = q_edges[i], q_edges[i + 1]
                mask = (xs >= lo) & (xs <= hi) if i == len(q_edges) - 2 else (xs >= lo) & (xs < hi)
                if mask.any():
                    bx.append(float(xs[mask].mean()))
                    by.append(float(ys[mask].mean()))
                    bw.append(int(mask.sum()))
            fit_x, fit_y, fit_w = np.array(bx), np.array(by), np.array(bw)
        else:
            fit_x, fit_y, fit_w = xs, ys, None

        iso = IsotonicRegression(y_min=0.02, y_max=0.98,
                                 increasing=True, out_of_bounds='clip')
        iso.fit(fit_x, fit_y, sample_weight=fit_w)

        # Persist the step function as knots so prediction needs only numpy.
        kx = np.asarray(iso.X_thresholds_, dtype=float)
        ky = np.asarray(iso.y_thresholds_, dtype=float)
        if len(kx) < 2:
            self._deactivate()
            self._n_seen = n
            self.save()
            return CalibrationReport(n=n, active=False)

        self._x, self._y = kx, ky
        self._n_fit = n
        self._shrink = min(1.0, n / float(self.full_trust_n)) if self.full_trust_n > 0 else 1.0
        self._n_seen = n

        # Diagnostics (in-sample): does calibration reduce Brier / ECE?
        cal_pred = np.array([self.calibrate(float(x)) for x in xs])
        report = CalibrationReport(
            n=n, active=True, shrink=self._shrink,
            brier_raw=_brier(xs, ys), brier_cal=_brier(cal_pred, ys),
            ece_raw=_ece(xs, ys),     ece_cal=_ece(cal_pred, ys),
            bins=_reliability_bins(xs, ys),
        )
        self.save()
        logger.info("[CALIB] fit on %d trades, shrink=%.2f, Brier %.4f->%.4f",
                    n, self._shrink, report.brier_raw, report.brier_cal)
        return report

    def fit_from_journal(self, journal) -> CalibrationReport:
        # Exclude records from older probability-model versions — their prob_win
        # is out-of-distribution and would poison the isotonic fit.
        records = getattr(journal, 'records', [])
        samples = [(float(getattr(r, 'prob_win', 0.0) or 0.0), bool(getattr(r, 'won', False)))
                   for r in records
                   if int(getattr(r, 'prob_model_version', 0)) >= MIN_MODEL_VERSION]
        report = self.fit(samples)
        # maybe_refit() gates on TOTAL journal growth, so _n_seen must track the
        # full record count — not the (version-filtered) usable sample count, or a
        # large pool of excluded legacy records would trigger a refit every loop.
        self._n_seen = len(records)
        self.save()
        return report

    def maybe_refit(self, journal) -> bool:
        """Refit only when the journal has grown by >= refit_every since last attempt.
        Returns True if a refit ran. Cheap to call every loop iteration."""
        n_records = len(getattr(journal, 'records', []))
        if n_records - self._n_seen >= self.refit_every:
            self.fit_from_journal(journal)
            return True
        return False

    # ── persistence ───────────────────────────────────────────────────────────
    def _deactivate(self):
        self._x = self._y = None
        self._n_fit = 0
        self._shrink = 0.0

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'w') as f:
                json.dump({
                    'x': self._x.tolist() if self._x is not None else None,
                    'y': self._y.tolist() if self._y is not None else None,
                    'n_fit': self._n_fit,
                    'shrink': self._shrink,
                    'n_seen': self._n_seen,
                }, f, indent=2)
        except Exception as e:                       # don't let calibration break trading
            logger.warning("[CALIB] save failed: %s", e)

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if d.get('x') and d.get('y'):
                self._x = np.asarray(d['x'], dtype=float)
                self._y = np.asarray(d['y'], dtype=float)
            self._n_fit = int(d.get('n_fit', 0))
            self._shrink = float(d.get('shrink', 0.0))
            self._n_seen = int(d.get('n_seen', 0))
        except Exception:
            pass   # first run / no file → stays inactive (identity)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _selftest():
    """Synthetic check: a miscalibrated source should be pulled toward truth,
    monotonicity preserved, and the sub-threshold case stay identity."""
    rng = np.random.default_rng(0)
    # Source claims p in [0.5,0.8] but truly wins at p-0.1 (over-confident).
    raw = rng.uniform(0.50, 0.80, size=400)
    won = rng.uniform(size=400) < np.clip(raw - 0.10, 0, 1)
    cal = ProbabilityCalibrator(path=os.path.join(os.path.dirname(CALIB_FILE), 'calibration_selftest.json'))
    rep = cal.fit(list(zip(raw, won.tolist())))
    print(rep.render())
    assert cal.is_active, "should be active with 400 samples"
    # over-confident input should be pulled DOWN
    assert cal.calibrate(0.75) < 0.75, f"expected pull-down, got {cal.calibrate(0.75)}"
    # monotonic
    grid = np.linspace(0.5, 0.8, 20)
    out = [cal.calibrate(g) for g in grid]
    assert all(b >= a - 1e-9 for a, b in zip(out, out[1:])), "calibration not monotonic"
    # small sample → identity
    small = ProbabilityCalibrator(path=os.path.join(os.path.dirname(CALIB_FILE), 'calibration_selftest2.json'))
    small.fit([(0.6, True), (0.6, False), (0.7, True)])
    assert not small.is_active and small.calibrate(0.7) == 0.7, "small sample must stay identity"
    # cleanup
    for p in (cal.path, small.path):
        try: os.remove(p)
        except OSError: pass
    print("\nselftest OK: pulls over-confidence down, monotonic, identity below min_samples")


def _main():
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if '--selftest' in sys.argv:
        _selftest()
        return
    from .trade_journal import TradeJournal
    journal = TradeJournal()
    cal = ProbabilityCalibrator()
    rep = cal.fit_from_journal(journal)
    print(rep.render())


if __name__ == '__main__':
    _main()
