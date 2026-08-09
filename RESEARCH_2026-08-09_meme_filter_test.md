# Pre-registration — do the meme filters have any selective power?

**Registered:** 2026-08-09, before any arm reached n=1.
**Status:** OPEN. Collecting.
**Instrument:** `scripts/meme_paper.py` (three-arm paper book over `data/meme_cohort/`)
**Read-out due:** on the first date all three arms clear n=30; earliest realistic ~2026-08-23.

This document exists so the decision rule is fixed in advance. Every candidate
edge this repo has killed was killed by a rule written *before* the data came
in; the ones that looked alive longest were the ones where the rule moved.

---

## The question

Not "is meme trading profitable" — that is not answerable at this sample size and
is not what the filters claim. The narrow, answerable question is:

> Do the `screener_risk` gates, and the `meme_radar` delta triggers on top of
> them, select pools that behave differently from the ones they reject?

A filter can be profitable and useless (it picked what the whole market did), or
unprofitable and useful (it avoided disasters in a falling market). Selective
power and profitability are different claims and are tested separately below.

## Arms

| Arm | Definition |
|---|---|
| `pass` | cleared the risk gates **and** the delta triggers fired |
| `gates` | cleared the risk gates, delta did **not** fire |
| `control` | **failed** the risk gates; deterministic 10% sample of rejects |

Control sampling is keyed on a SHA-256 of the pool address with a fixed seed, so
the same cohort always produces the same control arm. It cannot be re-rolled to
taste, and it does not depend on iteration order or on which tick first saw the
pool.

Control rate is set independently of the pass rate on purpose. Tying it to pass
count would starve the control arm exactly when the filter is most selective —
i.e. when the comparison matters most.

## Mechanics

- $100 notional per position, uniform. No sizing logic — sizing is a separate
  question and mixing it in would confound this one.
- Entry at the observed cohort price; round-trip cost from `screener_risk`
  charged across both legs.
- Primary exit: fixed 24h hold. MFE/MAE recorded so take-profit and stop rules
  can be evaluated later **without re-running**, which keeps exit choice from
  becoming a free parameter fitted after the fact.
- A pool going `gone` or `drained` closes at **-100%**. Never dropped.
- Positions with no modellable exit are flagged `tradeable=False`. The control
  arm is reported twice — excluding them, and scoring them -100%. The second is
  the honest number: you could not have sold those.

## Primary metric

**Median net return per arm**, net of round-trip cost.

Median, not mean: meme returns are fat-tailed to the point where the mean is a
statement about one or two outliers. Both are reported; the decision rule uses
the median.

## Decision rule — fixed now

Read out only when **all three arms have n>=30 closed, scored positions.**

1. **`pass` vs `gates`** — the clean comparison. Both arms cleared identical
   gates, so only the delta trigger differs, and there is no age confound.
   - delta adds nothing unless `pass` median exceeds `gates` median by **>5
     percentage points**. Below that, the delta logic is removed from the radar.
2. **`gates` vs `control`** — tests the gates as a whole, age included.
   - gates select nothing unless `gates` median exceeds `control` median
     (untradeable scored -100%) by **>10 percentage points**.
3. **Profitability is a separate, stricter bar.** Even if the filter is
   selective, the radar stays off as a trading tool unless the `pass` arm's
   **total P&L in dollars** is positive at n>=30. A selective filter over a
   negative-expectancy population is a better way to lose money, not an edge.

If (1) fails, the delta triggers come out. If (2) fails, the whole radar is
demoted to a risk filter and the alerting is switched off — the treatment the
arb arm got. If (3) fails but (1) and (2) pass, the filters are kept as a
research instrument and nothing is traded.

## Confounds, stated in advance

- **Age.** A 48h minimum is one of the gates, so the control arm skews newborn by
  construction. `gates vs control` therefore tests the gate stack *as a whole*,
  not liquidity or cost in isolation. `pass vs gates` is the unconfounded one.
  Median entry age per arm is printed with every report.
- **Universe.** The cohort is Solana pools surfaced by GeckoTerminal's
  `new_pools`. Findings do not generalise to other chains or to the DexScreener
  boosted feed the live radar polls.
- **Coverage.** If the cohort's overflow rate rises above a few percent, the
  sample under-represents launch-rate spikes and the result is provisional.
- **Survivorship.** Handled by construction — deaths close at -100% and pools are
  never removed from the registry.
- **No contract-level data.** LP lock, mint/freeze authority, holder
  concentration and deployer history are all invisible on the free tier. A null
  result here is a null result about *price, liquidity and flow filters only*,
  and says nothing about whether contract-level filters would work.

## What would make this test invalid

- Changing gate thresholds, delta thresholds, hold period, or the control rate
  mid-run. Any such change ends this registration and starts a new one.
- Reading out before all arms clear n=30.
- Reporting the mean when the median disagrees with it.
- Quietly excluding untradeable control positions from the headline.

---

## Amendment 1 — 2026-08-09, same day, before any arm reached n=1

**Change:** the live radar's delta trigger was switched from trade-count
acceleration (v1) to unique-buyer growth (v2). Every paper position now records
`delta_v1_fired` and `delta_v2_fired` as flags.

**Why this does not invalidate the registration.** Arm assignment is unchanged —
`pass` is still "cleared the gates AND **v1** fired". The three pre-registered
comparisons therefore run on exactly the population they were registered
against. v2 is recorded as metadata, not as a competing arm, so it does not
change which pools land where and does not alter any threshold, the hold period,
or the control rate.

**Why v2 was adopted in the live radar without waiting for the read-out.** v1
measures trade counts, which one wallet can inflate arbitrarily for the price of
gas. This is not hypothetical: on the cohort, the p90 pool ran 5.7 trades per
unique wallet and 3.5% ran above 10x. A constructed wash pattern — trades
accelerating while distinct buyers stay flat — fires v1 and is correctly refused
by v2. That is a defect fix, not an edge claim. **v2 being harder to fake is not
evidence it selects better, and that claim is what the test below settles.**

**Added comparison, declared now:**

4. **v2 vs v1, within gate-passing positions only.** Among positions in the
   `pass` + `gates` arms, compare median net return for `delta_v2_fired=True`
   against `delta_v1_fired=True`. Requires n>=30 in each group.
   - v2 is kept in the radar if its median net is **>= v1's minus 5 pts**
     (i.e. it is not materially worse). The bar is deliberately asymmetric: v2's
     justification is manipulation-resistance, so it earns its place by not
     being worse, and only claims superiority if it beats v1 by >5 pts.
   - If v2 is materially worse (>5 pts below v1), the radar reverts to v1 and the
     wash-resistance argument is recorded as a loss — resistance to a
     manipulation that turns out not to matter is not worth a worse signal.

**Cost of the change:** v2 needs unique-wallet counts, which DexScreener does not
publish. The radar now enriches gate-passing candidates only (typically ~2 per
tick) from GeckoTerminal, whose pool address is the same as DexScreener's
`pairAddress` — verified 2026-08-09. Roughly one extra API call per tick. When
enrichment fails, v2 does not fire; it never silently falls back to v1.

---

## Result

*(to be completed at read-out — leave blank until then)*
