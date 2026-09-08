"""
Empirical check of the UltraIndicator design, using the spec's own numbers.

Q1. ShadowSignalTester promotes on |IC| > 0.03 after 500 observations.
    If the candidate is PURE NOISE, how often does it get promoted?

Q2. AdaptiveWeighter treats sample_count >= 100 as full confidence
    (confidence_scalar = min(n/100, 1)). How independent are 100 samples
    logged every tick against a 300s forward horizon?

Q3. MetaWatchdog flags "degraded" when recent-30 Sharpe < 0.5 * baseline Sharpe.
    On a STATIONARY pnl stream (no real degradation), how often does it fire?
"""
import numpy as np

rng = np.random.default_rng(20260903)

POLL_SEC = 1        # main loop poll_interval_sec
HORIZON = 300       # TradeOutcomeTracker horizon_seconds
MIN_OBS = 500       # ShadowSignalTester min_observations
IC_THRESH = 0.03    # promotion_ic_threshold


def overlapping_forward_returns(n_obs, horizon_steps, sigma=0.0004):
    """Price path sampled every POLL_SEC; forward return over `horizon_steps`."""
    steps = rng.normal(0, sigma, n_obs + horizon_steps)
    logp = np.cumsum(steps)
    fwd = logp[horizon_steps:horizon_steps + n_obs] - logp[:n_obs]
    return fwd


print("=" * 72)
print("Q1  Pure-noise signal through the promotion gate (|IC|>0.03, n=500)")
print("=" * 72)

TRIALS = 4000
promoted = 0
ics = []
for _ in range(TRIALS):
    fwd = overlapping_forward_returns(MIN_OBS, HORIZON)
    sig = rng.normal(0, 1, MIN_OBS)          # candidate carries ZERO information
    ic = np.corrcoef(sig, fwd)[0, 1]
    ics.append(ic)
    if abs(ic) > IC_THRESH:
        promoted += 1

ics = np.array(ics)
print(f"trials                    : {TRIALS}")
print(f"promotion rate (noise)    : {promoted / TRIALS:6.1%}   <-- false-promotion rate")
print(f"observed sd of IC         : {ics.std():.4f}")
print(f"threshold in sd units     : {IC_THRESH / ics.std():.2f} sd")
print(f"|IC| 95th pct under null  : {np.percentile(np.abs(ics), 95):.4f}")
print(f"threshold that WOULD give 5% false promotions: {np.percentile(np.abs(ics), 95):.3f}")

print()
print("=" * 72)
print("Q2  Effective sample size with overlapping windows")
print("=" * 72)

overlap = HORIZON // POLL_SEC
for n_logged in (100, 500, 5000, 50000):
    n_eff = n_logged / overlap
    se = 1 / np.sqrt(max(n_eff - 3, 0.01))
    print(f"logged={n_logged:>6}  independent~{n_eff:8.1f}  SE(IC)~{se:6.3f}")

need_eff = (1 / 0.015) ** 2          # SE=0.015 so that IC=0.03 is ~2 SE
need_logged = need_eff * overlap
print(f"\nfor IC=0.03 to be ~2 SE you need ~{need_eff:,.0f} INDEPENDENT obs")
print(f"  = {need_logged:,.0f} logged ticks = {need_logged * POLL_SEC / 86400:,.1f} days")
print(f"  per (signal, regime) cell; spec has 7 signals x 4 regimes = 28 cells")
print(f"  a regime present 10% of the time needs ~{need_logged * POLL_SEC / 86400 / 0.10:,.0f} days")

print()
print("=" * 72)
print("Q3  Watchdog false-alarm rate on a STATIONARY pnl stream")
print("=" * 72)

BASELINE_WINDOW = 30
THRESH = 0.5
for total in (100, 200, 500):
    fires = 0
    crits = 0
    T = 3000
    for _ in range(T):
        pnl = rng.normal(0.02, 1.0, total)   # stationary, mildly positive
        recent = pnl[-BASELINE_WINDOW:]
        older = pnl[:-BASELINE_WINDOW]
        rs = recent.mean() / (recent.std() + 1e-9)
        os_ = older.mean() / (older.std() + 1e-9)
        if os_ > 0 and rs < os_ * THRESH:
            fires += 1
        if rs < -1.0:
            crits += 1
    print(f"history={total:>4}  'degraded' fires {fires/T:6.1%} of checks   "
          f"'critical' fires {crits/T:6.1%}")
print("\n(no real degradation exists in any of these streams)")
