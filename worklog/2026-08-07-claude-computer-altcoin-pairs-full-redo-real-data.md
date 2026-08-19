---
date: 2026-08-07
agent: claude-computer
branch: pairs-altcoin-arm
pr: 93
lane: brain-risk-observability
files: [RESEARCH_2026-08-04_altcoin_pairs_and_arb.md, backtest_altcoin_pairs_v2_real.py]
---

# Altcoin pairs cointegration: full redo with 100% verified real hourly data — zero survivors

Follow-through on this session's own retraction (PR #93, closed 2026-08-07):
the original altcoin-pairs finding was built on daily-close data forward-
filled to hourly resolution for every coin except BTC/ETH/SOL, which made
the strategy's apparent mean-reversion edge an artifact of the spread only
genuinely updating once a day.

Fetched genuine hourly OHLCV for all remaining coins (16 total) and rebuilt
the discovery script (`backtest_altcoin_pairs_v2_real.py`) with a hard
self-check that rejects any cached file whose bar-to-bar gaps aren't
consistently exactly 3600 seconds — refuses to trust a daily-ffill'd series
again, structurally not just by convention.

Reran the full pipeline (Engle-Granger cointegration + 4-fold walk-forward,
"all folds net positive" bar) across all C(16,2)=120 pairs, 100% verified
real hourly data. **Zero pairs survived** — not a smaller list, none. This
decisively confirms the original finding was the artifact in full, not a
promising idea undermined by a fixable bug. Altcoin pairs cointegration is
now closed as a research line in this repo; do not revisit without a
materially different hypothesis.

**Cross-lane note:** none — worklog/proof_scorecard only, PR already closed,
no production code affected.

**Verification:** research-doc-only change plus one new standalone script;
no test suite impact.
