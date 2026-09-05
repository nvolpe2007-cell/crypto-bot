"""
Calibrated statistics for online/adaptive signal weighting.

Written against RESEARCH_2026-09-03_ultra_indicator_review.md, which measured three
failures in the UltraIndicator spec that all share one cause: gates whose false-alarm
rate was chosen by taste rather than derived from a null distribution.

  * a promotion gate at |IC| > 0.03 promoted PURE NOISE 52% of the time
  * "full confidence" at 100 logged samples was ~0.3 independent observations
  * a degradation monitor fired on ~31% of checks against a stationary stream

Every function here takes the opposite approach: state the false-alarm rate you are
willing to accept, and derive the threshold from it.

Pure functions, no state, no I/O, not wired to anything.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "effective_sample_size",
    "ic_null_sd",
    "ic_promotion_threshold",
    "cost_adjusted_score",
    "shrink_to_equal_weights",
    "cusum_change_detector",
    "toxicity_dampener",
]


# ── sample independence ───────────────────────────────────────────────────────

def effective_sample_size(n_logged: int, horizon_seconds: float,
                          poll_seconds: float) -> float:
    """
    Independent-observation count for OVERLAPPING forward-return windows.

    Logging every `poll_seconds` against a `horizon_seconds` forward return means
    consecutive rows share (horizon - poll) of their window. They are one
    observation smeared across many rows, not many observations.

    n_eff = n_logged / (horizon / poll)

    At the reviewed spec's numbers (1s poll, 300s horizon) this is a 300x
    inflation: 100 logged rows are ~0.33 independent observations.
    """
    if n_logged <= 0:
        return 0.0
    if poll_seconds <= 0 or horizon_seconds <= 0:
        raise ValueError("horizon_seconds and poll_seconds must be positive")
    overlap = max(horizon_seconds / poll_seconds, 1.0)
    return float(n_logged) / overlap


def ic_null_sd(n_eff: float) -> float:
    """
    Standard deviation of a sample correlation under the null of zero true
    correlation: 1/sqrt(n_eff - 3) (Fisher). Returns inf below 4 effective
    observations, because with that little data no threshold is meaningful and
    returning a finite number would invite treating one as if it were.
    """
    if n_eff < 4:
        return float("inf")
    return 1.0 / math.sqrt(n_eff - 3.0)


def ic_promotion_threshold(n_eff: float, n_candidates: int = 1,
                           target_fwer: float = 0.05) -> float:
    """
    |IC| a candidate must exceed to be promoted, for a stated FAMILY-WISE false
    promotion rate across `n_candidates` candidates ever tested.

    Šidák: per-candidate alpha = 1 - (1 - fwer)^(1/k). The correction matters more
    than it looks — every registered candidate is another draw at the null, and the
    reviewed spec applied none, so testing 5 noise candidates promoted at least one
    with probability ~97.5%.

    Returns inf when n_eff is too small to support any threshold, which is the
    honest answer for "you do not have enough data to promote anything yet".
    """
    if n_candidates < 1:
        raise ValueError("n_candidates must be >= 1")
    if not 0.0 < target_fwer < 1.0:
        raise ValueError("target_fwer must be in (0, 1)")

    sd = ic_null_sd(n_eff)
    if not math.isfinite(sd):
        return float("inf")

    alpha = 1.0 - (1.0 - target_fwer) ** (1.0 / n_candidates)
    # two-sided critical z for the per-candidate alpha
    z = _norm_ppf(1.0 - alpha / 2.0)
    return z * sd


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, ~1e-9 abs).
    Local so this module stays dependency-free beyond numpy."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── objective ─────────────────────────────────────────────────────────────────

def cost_adjusted_score(signal_value: float, forward_return: float,
                        round_trip_cost: float) -> float:
    """
    Per-observation learning target, replacing sign(signal) * sign(return).

    Returns the NET return the signal's direction would have captured, after
    paying the round trip:

        score = sign(signal) * forward_return - round_trip_cost

    Why this and not hit rate: hit rate is blind to magnitude, so a signal right
    70% of the time on moves smaller than the cost ranks top while losing money.
    This repo has measured that directly — inverting the sign of 228 real trades
    moved the loss from -$20.14 to -$9.50 but stayed red, and cost came in at
    18.6x the size of the predicted move. Direction was never the constraint.

    A zero signal expresses no view, takes no trade, and therefore pays no cost:
    it scores exactly 0 rather than -cost.
    """
    if signal_value == 0 or not np.isfinite(signal_value):
        return 0.0
    if not np.isfinite(forward_return):
        return 0.0
    return float(np.sign(signal_value) * forward_return - abs(round_trip_cost))


# ── weighting ─────────────────────────────────────────────────────────────────

def shrink_to_equal_weights(raw_weights: Mapping[str, float], n_eff: float,
                            prior_strength: float = 50.0) -> Dict[str, float]:
    """
    Shrink estimated weights toward 1/N, with the pull decreasing as evidence
    accumulates:

        lambda = prior_strength / (prior_strength + n_eff)
        w      = lambda * (1/N) + (1 - lambda) * w_hat

    1/N is the prior because equal weighting beating optimised weighting
    out-of-sample is one of the most reproducible findings in portfolio research
    (DeMiguel, Garlappi & Uppal 2009). An adaptive weighter has to BEAT 1/N to be
    worth its variance, so 1/N is where it should start and what it should have to
    argue its way off.

    `prior_strength` is in units of effective observations: at n_eff equal to it,
    the result sits halfway between the estimate and 1/N.
    """
    if not raw_weights:
        return {}
    n = len(raw_weights)
    equal = 1.0 / n
    n_eff = max(float(n_eff), 0.0)
    lam = prior_strength / (prior_strength + n_eff) if prior_strength > 0 else 0.0

    shrunk = {k: lam * equal + (1.0 - lam) * float(v) for k, v in raw_weights.items()}
    shrunk = {k: max(v, 0.0) for k, v in shrunk.items()}
    total = sum(shrunk.values())
    if total <= 0:
        return {k: equal for k in raw_weights}
    return {k: v / total for k, v in shrunk.items()}


# ── monitoring ────────────────────────────────────────────────────────────────

def cusum_change_detector(values: Sequence[float], target: float,
                          slack_sd: float = 0.5,
                          threshold_sd: float = 8.0,
                          scale: Optional[float] = None) -> Tuple[bool, float, int]:
    """
    One-sided CUSUM for a downward shift in the mean of `values` below `target`.

    Returns (fired, max_statistic, index_first_fired). `index_first_fired` is -1
    when it never fires.

    Chosen over "recent Sharpe < 0.5 x baseline Sharpe" because that test fired on
    ~31% of checks against a stationary stream. CUSUM accumulates evidence instead
    of comparing two noisy point estimates.

    THE DEFAULT IS MEASURED, NOT ASSUMED. Textbook CUSUM tables suggest h=5 for
    k=0.5; that gives an ARL0 near 465, so on a 500-observation stream it false-
    alarms about 42% of the time — reproducing the very failure this replaces.
    Measured over 600 stationary streams of 500 observations (slack_sd=0.5):

        threshold_sd :   5      6      7      8      9     10
        false alarms : 42.5%  18.8%   7.0%   1.7%   1.2%   0.0%

    Default 8.0 gives ~1.7% per 500 observations while still detecting a genuine
    -2 sd shift 100% of the time in the same experiment. Recalibrate against your
    own stationary history if your stream length differs materially — the
    false-alarm rate depends on how many observations you run it over, and a
    monitor whose false-alarm rate you cannot state is not a monitor.

    `slack_sd` is the drift you are willing to ignore, in sd units; only shortfalls
    beyond it accumulate.
    """
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if v.size < 2:
        return False, 0.0, -1

    sd = float(scale) if scale is not None else float(v.std(ddof=1))
    if not np.isfinite(sd) or sd <= 0:
        return False, 0.0, -1

    k = slack_sd * sd
    h = threshold_sd * sd

    s = 0.0
    peak = 0.0
    first = -1
    for i, x in enumerate(v):
        s = max(0.0, s + (target - x) - k)
        peak = max(peak, s)
        if s > h and first < 0:
            first = i
    return first >= 0, float(peak), first


# ── risk scaling ──────────────────────────────────────────────────────────────

def toxicity_dampener(vpin_percentile: float, max_reduction: float = 0.5) -> float:
    """
    Size multiplier in [1 - max_reduction, 1] from a VPIN percentile.

    The reviewed spec used `1.0 - vpin_pct * 0.5` unclamped. Fed a 0-100 percentile
    instead of 0-1 that returns -49, which does not reduce size — it multiplies the
    signal by a large negative number and INVERTS the trade, in exactly the toxic
    conditions the control exists to protect against. A risk control must not be
    able to become a direction flip, so this clamps both the input and the output.
    """
    p = float(np.clip(vpin_percentile if vpin_percentile <= 1.0
                      else vpin_percentile / 100.0, 0.0, 1.0))
    m = float(np.clip(max_reduction, 0.0, 1.0))
    return float(np.clip(1.0 - m * p, 1.0 - m, 1.0))
