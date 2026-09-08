# UltraIndicator architecture — design review

**Date:** 2026-09-03
**Subject:** Owner-supplied adaptive-weighting ensemble spec (StateStore, AdaptiveWeighter,
TradeOutcomeTracker, MetaWatchdog, ShadowSignalTester, UltraIndicator)
**Verdict:** the engineering is sound; **the statistics are not, and they fail in the
direction that manufactures false confidence.** Three measured findings below, then fixes.
**Script:** `scripts/ultra_indicator_review_sim.py`

---

## What is right, and worth keeping

Listing this first because most of the review is critical and the design is not bad.

- **Persistence first.** Correct build order. State that doesn't survive a restart is state
  you can't learn from on a VPS that reboots.
- **Atomic snapshot write** (`tmp_path.replace(path)`) — genuinely correct, and the thing
  most people get wrong. A half-written pickle is worse than no pickle.
- **Recording the full signal fingerprint at entry** (`signal_snapshot`) is the single best
  idea in the spec. It is what makes honest per-signal attribution possible later, and
  almost nothing in this repo currently does it.
- **Shadow testing separated from live weighting.** Exactly the right shape: candidates earn
  their way in rather than being feature-stacked on day one.
- **A meta-layer that watches the strategy itself.** The instinct is right even though the
  implementation (below) doesn't work.
- `confidence_scalar = min(n/100, 1)` is a crude shrinkage term — the right idea, wrong
  magnitude by about four orders of magnitude.

---

## Finding 1 — the promotion gate is a coin flip

`ShadowSignalTester` promotes a candidate when `abs(ic) > 0.03` after 500 observations.

I fed it a **pure noise** signal — `rng.normal(0,1)`, carrying zero information about
forward returns — 4,000 times, using the spec's own parameters (1s poll, 300s horizon,
500 observations):

| | |
|---|---|
| **False-promotion rate** | **52.0%** |
| sd of IC under the null | 0.0459 |
| 0.03 threshold, in sd | **0.65 sd** |
| threshold needed for 5% false promotions | **0.089** |

**A signal that knows nothing gets promoted into the live ensemble on a coin flip.** The
threshold is set below one standard deviation of the null distribution, so it does not
discriminate at all — it merely samples it.

It is worse than 52% in practice, for two reasons the simulation does not model:

1. **No multiple-testing correction.** Every candidate registered is another draw. With `k`
   candidates the probability at least one noise signal is promoted is `1-(1-0.52)^k` — at
   k=5 that is 97.5%. This repo already owns the correct machinery for this
   (`proof_scorecard._family_t_bar`, Šidák) and the shadow tester should use it.
2. **`abs(ic)` promotes anti-predictive signals.** A candidate with IC = −0.4 clears the
   gate. It then reaches `AdaptiveWeighter`, where `raw_weight = max(0.0, new_ewm)` pins it
   at zero weight permanently. So the two components disagree about what promotion means:
   one admits sign-blind, the other is sign-sensitive.

## Finding 2 — the sample counts are inflated ~300x

`AdaptiveWeighter` treats `sample_count >= 100` as full confidence. The main loop calls
`record_trade` on **every tick** a symbol qualifies, at `poll_interval_sec=1`, against a
`horizon_seconds=300` forward return.

Consecutive observations therefore share 299 of their 300 seconds. They are not independent
samples; they are one sample smeared across 300 rows.

| logged observations | ~independent | SE(IC) |
|---|---|---|
| 100 | 0.3 | — |
| 500 | 1.7 | — |
| 5,000 | 16.7 | 0.271 |
| 50,000 | 166.7 | 0.078 |

**At the spec's "full confidence" point of 100 samples, the effective sample size is 0.3.**

For IC = 0.03 to sit two standard errors from zero you need ~4,444 independent
observations = **1.33M logged ticks = 15.4 continuous days**, *per (signal, regime) cell*.
The spec has 7 signals × 4 regimes = **28 cells**, and regimes are not equally frequent — a
regime present 10% of the time needs **~154 days** to fill one cell.

The design as written will converge fast, confidently, and onto noise. Speed of convergence
here is a symptom, not a feature.

## Finding 3 — the watchdog cries wolf a third of the time

`MetaWatchdog` flags `degraded` when the last-30 per-trade Sharpe drops below 0.5× the
baseline Sharpe. On a **stationary** P&L stream with no degradation whatsoever:

| history length | "degraded" fires | "critical" fires |
|---|---|---|
| 100 | **31.6%** | 0.0% |
| 200 | **30.2%** | 0.0% |
| 500 | **34.4%** | 0.0% |

A monitor that alarms on a third of all checks when nothing is wrong will be muted within a
week, and then it is worse than no monitor — because you believe you have one. The cause is
using a 30-sample Sharpe ratio as a point estimate with no interval and no significance
test.

(The `critical` branch never fires at all here, which is the mirror problem: a threshold of
recent-Sharpe < −1.0 on 30 samples is so extreme it will also miss real collapses.)

---

## Finding 4 — the objective function is the one this repo has already disproven

This is the deepest issue and it is not a bug; it is a choice.

```python
correctness = np.sign(signal_value) * np.sign(realized_forward_return)
```

The entire system learns to maximise **directional hit rate**. It is blind to magnitude and
blind to cost. But this repo's own measured record says the binding constraint is neither
prediction nor direction:

- `inverted_signal_test` — flipping the sign of every one of 228 real trades took the loss
  from −$20.14 to −$9.50. **Still red.** If direction were the problem, inverting would have
  fixed it.
- `trade_journal_csv_polluted` — cost measured at **18.6×** the size of the predicted move.
- Zero-cost replay of the same journal **still lost** (−$5.33).
- The BTCUSDT.P backtest run earlier today: Continuation −$3.72/trade over 173 trades,
  Exhaustion −$5.89/trade over 37. Exhaustion had the *lower* hit rate (29.7% vs 33.5%) and
  the *better* profit factor — the two metrics disagree, and hit rate is the one that lies.

A signal that is right 70% of the time on moves smaller than the 0.52% round trip is a
losing signal, and this weighter will rank it top. **Optimising hit rate is optimising the
metric that has already been shown not to be the constraint.**

The fix is a one-line change of objective with large consequences:

```python
# instead of sign agreement:
net = realized_forward_return - round_trip_cost * np.sign(abs(signal_value))
score = np.sign(signal_value) * net        # cost-aware, magnitude-aware
```

This reverses the ranking. Signals predicting *rare large* moves beat signals predicting
*frequent small* ones, which is the opposite of what the current objective rewards, and it
is the direction every surviving result in this repo points.

---

## Code-level defects

Ordered by how badly they bite.

**1. VPIN dampener can invert your signal.** `toxicity_dampener = 1.0 - (vpin_pct * 0.5)`.
If `vpin_toxicity_percentile` arrives on a 0–100 scale rather than 0–1, this goes to −49 and
**multiplies the final score by a large negative number** — a risk control that silently
flips direction in exactly the toxic conditions it exists to protect against. Clamp it:
```python
p = float(np.clip(vpin_toxicity_percentile, 0.0, 1.0))
toxicity_dampener = float(np.clip(1.0 - 0.5 * p, 0.5, 1.0))
```

**2. Restore is inconsistent across the two stores.** Weights come from SQLite,
`ic_history`/`ewm_state` come from pickle. If the pickle is missing or stale but the DB is
not, you restore weights with empty histories — then the first `update()` recomputes
`confidence_scalar` from n=1 and collapses every weight to ~0. Restore both from one source,
or version them together and refuse to load a mismatched pair.

**3. Promoted signals do not survive a restart.** `_promote_signal` appends to
`self.signal_names`, but `signal_names` is never persisted — it is rebuilt from the hardcoded
list in `run_ultra_indicator_loop`. After a reboot the promotion is silently reverted while
its `ic_history` remains in the pickle, leaving an orphan. Persist the live signal set.

**4. SQLite will throw under concurrency.** `threading.Lock` guards writes but **not reads**
(`load_weights`, `get_unresolved_trades`), and the default journal mode is rollback. Add:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
```

**5. `trade_outcomes` has no index and grows without bound.** `get_unresolved_trades` full-
scans every 15s, on a table gaining a row per symbol per qualifying tick. Add
`CREATE INDEX ... ON trade_outcomes(resolved, entry_time)` and prune resolved rows older
than the learning window.

**6. Pickle for durable state.** It is fragile across refactors and is an arbitrary-code-
execution surface. The payloads here are dicts of deques of floats — JSON or a SQLite table
does the job with none of the downside.

---

## The recommended redesign, concretely

1. **Sample non-overlapping.** One observation per symbol per horizon, not per tick. This
   costs you 300× the data volume and buys you data that means something. If you want the
   throughput, use overlapping samples but correct the variance (Newey–West / block
   bootstrap) rather than counting them as independent.
2. **Set the promotion threshold from the null, not from taste.** Compute the sd of IC under
   a shuffled-label null at the actual N_eff, require ~3 sd, and apply a Šidák correction
   across the number of candidates ever registered. Reuse `proof_scorecard._family_t_bar`.
3. **Promote on signed IC, not `abs(ic)`** — or invert the signal on promotion. Pick one; the
   current pair of rules contradict.
4. **Learn on net-of-cost returns.** See Finding 4. Single highest-value change in this list.
5. **Shrink hard toward 1/N.** 28 cells fitted online is a multiple-testing machine.
   `1/N` beating optimised weights is one of the most robust results in portfolio research
   (DeMiguel, Garlappi & Uppal 2009); your weighter must clear that bar, so make `1/N` the
   prior and let evidence move you off it slowly.
6. **Pool regimes by default.** Split a regime out only once *that cell* has the independent
   sample count to justify it (partial pooling), rather than splitting scarce data 4 ways up
   front. Note also that `regime_intraday_arm` found regime labels whipsaw — `PersistentRegime`
   exists in this repo precisely for that.
7. **Replace the watchdog test.** Use a CUSUM or SPRT on cost-adjusted expectancy, tuned to a
   stated false-alarm rate (e.g. one false alarm per 90 days). Any monitor whose false-alarm
   rate you cannot state is not a monitor.
8. **Add a frozen control arm.** Run learned-weights against equal-weights and against
   frozen-initial-weights on the same signals, same costs, judged head-to-head by
   `proof_scorecard`. Without this you can never tell whether the adaptation is adding value
   or just adding variance — and the null hypothesis, given everything else in this repo,
   is that it adds variance.

---

## The precondition nobody can skip

`signal_computer.compute_all()` is described as the stub where "your existing OFI/VPIN/
liquidation/sweep code plugs in". Four of the seven listed signals — `ofi`, `oi_liq_regime`,
`lead_lag`, `absorption` — need order-book and open-interest data that **this system does
not record**. `src/orderflow_indicator.py` (PR #110) supplies the scoring functions;
`RESEARCH_2026-09-03_orderflow_capture_schema.md` specifies the capture layer; neither the
recorder nor the data exists yet.

So the honest sequencing is: **capture → measure → only then adapt.** Building the adaptive
layer first means it will spend its first months learning weights over signals computed from
data that was never persisted, and it will report confident numbers the whole time. That
combination — fast convergence, no ground truth, a broken promotion gate, and a monitor that
fires 30% of the time — is how a system talks itself into a live deployment it has not
earned.

None of this says the architecture is wrong. It says it is aimed at the wrong constraint and
graded on a curve it sets itself. Fix the objective, fix the sampling, calibrate the two
gates, and it becomes a genuinely useful measurement instrument.
