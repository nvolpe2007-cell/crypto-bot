---
date: 2026-09-10
agent: claude-computer
branch: docs/external-evidence-ssrn
pr: pending
lane: docs (CLAUDE.md only — no src/, no runner, no test touched)
files: [CLAUDE.md]
---

# The published literature independently reached this repo's conclusions

Owner asked for a deep scan of the SSRN working-paper archive across four active topics.
The trading half is the part that belongs in this repo, and it is worth recording because
**two independent paths reached the same verdict** — this corpus measured it the hard way,
and the published record got there from different data with different methods.

## What the literature confirms

| This repo | External |
|---|---|
| `xsec_momentum_verdict` — cross-sectional momentum loses, buys pumped alt tops | Han, Kang & Ryu (SSRN 4675565), realistic assumptions incl. liquidation: **TS momentum strong, XS momentum weak** |
| `tsmom-multiday-majors` — "the only survivor"; majors-only swing universe | Fieberg et al (4601972): CTREND survives costs and **persists for large and liquid coins** |
| `meanrev_dead` | Caporale & Plastun (3113177): overreactions real, **not exploitable** after costs |

Three separate verdicts this repo reached by measurement, confirmed from outside. That is
the strongest evidence the project holds on the momentum question — and a reason not to
re-litigate any of the three.

## What is new, and now in CLAUDE.md

**The turnover screen.** Novy-Marx & Velikov (2535173): anomalies below **~50% one-sided
monthly turnover** keep a significant net spread; few above it do. This is a pre-filter
applicable *before* a backtest is written, and it explains the whole corpus retrospectively
— the scalper's 73.6% fee drag was a turnover problem, not a signal problem.

**The buy/hold spread** (3253359) — strict entry, loose exit — is the most effective simple
cost mitigation. `FUNDING_ARB_EXIT_CONFIRM_HOURS` is already this for the funding arms. The
directional side has no equivalent. Recorded as an open improvement, not a claim that it
works here.

**The proof bar's level.** Harvey, Liu & Zhu (2249314) put the honest floor at **t > 3.0**;
the Šidák family bar here is ~2.68 at k=7. Right family, slightly below. Nothing in this
system clears either, so this changes no verdict — it just means the bar should not be read
as conservative.

## The one thing I did NOT write down as settled

Kim, Tse & Wald (2786955) argue published TSMOM performance is **largely volatility scaling
rather than momentum**. `trend-signal-honest`'s walk-forward *rejected* inverse-vol sizing
on the SMA(100) rule (Sharpe 0.98 → 0.76), which already contradicts this file's general
vol-targeting endorsement. Two credible sources point opposite ways on one lever. It is
recorded in CLAUDE.md as an **open tension with a pre-registerable test**, and deliberately
not appended as a verdict to any ledger entry — ledger rule 4 forbids editing an existing
entry's evidence block, and there is no new measurement here, only an external claim.

## Scope

Docs only. `CLAUDE.md` gains one section; no strategy, gate, sizing or runner behaviour
changes, and no in-flight proof is touched. The full scan — including the SEO, website-trust
and cold-outreach halves, which do not belong in this repo — is filed in the Obsidian vault
as `Notes/SSRN Literature Scan 2026-09-10.md` and linked from `Home.md`.
