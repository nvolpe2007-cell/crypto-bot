---
date: 2026-09-08
agent: claude-computer
branch: feat/gex-dealer-exposure-walls
pr: 112
lane: research/new-data
files: [deploy/gex_cron.txt, deploy/setup_vps.sh]
---

# The one pipeline that could escape the edge-search boundary had logged a single row

`exhaustive_search_320_zero` records the hard boundary of this repo's research:
**"no edge in daily OHLC; need new DATA or shorting, not more backtests."** The Deribit
options pipeline built 2026-09-05 is the only thing in the repo that answers that — it is
new data, not another price backtest.

It had **one row** in `data/gex_log.csv`, timestamped `2026-09-05T20:08` — the moment it
was built. It ran once, by hand, and stopped.

**Cause:** every other forward arm has a canonical cron in `deploy/` — `swing_cron.txt`,
`tsmom_cron.txt`, `lev_perp_arms_cron.txt`, `trend_ensemble_cron.txt`, `flash_arb`,
`stablecoin_arb`, `dex_arb`, `pattern_flow`, `trade_close_notifier`. GEX had none, and
`setup_vps.sh` therefore had nothing to install. The research shipped; the collection did
not. PR #112 has sat open and MERGEABLE since 2026-09-05.

**Verified the pipeline still works before wiring it up** — ran `gex_paper.py` live against
Deribit's public API: BTC spot 78,441.80, dealer-long-gamma regime, zero-gamma flip at
69,395 (+11.5%), ceiling 81,000 / floor 78,500, ATM IV 36.6% vs realized 35.2%. Two calls,
no key, sane output, second row logged. `tests/test_gex_calculator.py` 9/9 pass.

**Added** `deploy/gex_cron.txt` (hourly at :05) and the matching `install_cron` line in
`setup_vps.sh`. Hourly rather than daily because the hypothesis is about where price sits
RELATIVE to the walls, so the snapshot is only useful paired with subsequent price —
hourly lets a later study measure 1h/4h/24h forward returns conditioned on dist-to-flip
and dist-to-wall. Daily would cap the sample at ~30/month and make the intraday question
untestable. Cost is 2 unauthenticated calls/hour.

**Pre-registered the question in the cron file itself**, before any data exists, so it
cannot be rationalised later: below the flip point (dealer-short-gamma) forward realized
vol should be reliably HIGHER than above it, and price should mean-revert near walls.
**Kill criteria stated up front** — no significant regime difference after ≥200 paired
observations, or a wall-reversion effect smaller than the 0.54% round-trip cost. A
"promising but underpowered" reading at 200 observations is a NULL, not a reason to keep
looking. ETH is written but commented OUT: one symbol first, because a second correlated
symbol adds observations without adding independent evidence and inflates k against every
other arm on the scorecard.

**The assumption restated in the cron file, because it can invalidate everything:** net GEX
per strike assumes call OI = dealer LONG gamma and put OI = dealer SHORT gamma. That is the
standard convention and it is **unverifiable from public data** — Deribit publishes open
interest, not dealer inventory. If it is wrong, the sign of every regime label flips. This
data can at best show the PROXY correlates with something; it cannot prove the mechanism.

**Verification:** `bash -n deploy/setup_vps.sh` clean; `install_cron` is idempotent
(greps for `gex_paper.py` before adding). GEX tests 9/9. Full suite **3581 passed, 9 failed
— all pre-existing**. `GEX_SYMBOLS` confirmed supported by `gex_paper.py:62`, so the
commented ETH line is valid rather than aspirational.

**Cross-lane note:** none — a new deploy file plus one line in `setup_vps.sh`. No strategy,
gate or decision-pipeline file touched. Still logging-only, per the owner's explicit
2026-09-05 scope decision.

**Not done:** PR #112 is still unmerged, so nothing is collecting until the owner merges and
re-runs `setup_vps.sh` on the VPS. Until then this remains one row plus the one I added.
No analysis of the data is possible or attempted — that needs months, which is the point.
