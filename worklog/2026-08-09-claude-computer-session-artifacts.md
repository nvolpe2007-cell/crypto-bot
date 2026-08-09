---
date: 2026-08-09
agent: claude-computer
branch: docs/session-artifacts-2026-08-09
pr: TBD
lane: brain-risk-observability
files: [BOT_MECHANICS_2026-07-21.md, RESEARCH_2026-07-21_tick_ofi_cvd.md, backtest_lev_perp_last4months.py, fetch_ticks.py]
---

# Commit leftover local research artifacts from this session

Housekeeping: four files were generated during this session's research work
but never made it into a commit (produced between PRs, working tree carried
them across multiple branch switches). Docs/tooling only, no behavior
changes. Committing so they're not sitting local-only.

- `BOT_MECHANICS_2026-07-21.md` — the detailed "how does each arm actually
  think, step by step" reference written when walking through the full
  decision pipeline (entry filters, probability gate, Kelly sizing) for the
  owner. Useful onboarding doc for any future agent that needs the mechanics
  spelled out rather than re-reading every arm's source.
- `RESEARCH_2026-07-21_tick_ofi_cvd.md` — real Kraken tick-data backtest of
  order-flow-imbalance/CVD as a directional signal. Found the signal is
  real (t up to 16 on raw predictiveness) but 2-3x too small to clear even
  maker costs across a full parameter grid — independently confirms the
  shelved directional engine's own live t=-8.82 finding from a completely
  different angle (raw ticks vs. paper-trade history). Also answered the
  owner's "balls indicator" (Bookmap-style order-flow bubbles) question:
  it's a visualization of the same signed-volume data, not a new signal.
- `backtest_lev_perp_last4months.py` — faithful replay harness (real Kraken
  daily data + lev_perp_paper.py's actual production functions, not a
  reimplementation) used to get real recent-performance numbers for PRs
  #95/#88 before merge/close decisions. Kept for reuse -- e.g. re-running
  this same window later to see how the now-deployed 8-coin arm's forecast
  compares to what actually happened.
- `fetch_ticks.py` — Kraken tick-trade fetcher (OAuth-free public trades
  endpoint) that produced the tick data behind the OFI/CVD research above.

**Verification**: docs + one utility script + one already-used research
script; no test suite impact, nothing imports these into production code
paths.
