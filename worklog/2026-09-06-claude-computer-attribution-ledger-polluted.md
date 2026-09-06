---
date: 2026-09-06
agent: claude-computer
branch: fix/attribution-ledger-test-pollution
pr: pending
lane: brain/risk/observability
files: [tests/conftest.py, tests/test_attribution_isolation.py, scripts/attribution_forensics.py]
---

# `pytest` was writing to the production P&L ledger — and it manufactured a 100%-win-rate arm

Chasing the owner's "100% win rate" goal into the one place it can honestly live —
structural, non-directional strategies — I went to read the realised record in
`data/attribution.db`. The first query returned an arm with a **100% win rate, zero fees,
and +$942**. That is not a result, it is a symptom, and the trail led to a live bug.

## Root cause

`src/attribution.record()` writes through a **process-wide singleton** whose `db_path`
defaults to `data/attribution.db` — the live ledger behind the dashboard and the daily
Telegram scorecard. `regime_arm.py` and `arbitrage/funding_arb_paper.py` call `record()`
directly, so **merely exercising those code paths in a unit test appended rows to
production.** `tests/test_attribution.py` was careful and used `tmp_path`; nothing else
knew it had to be.

Confirmed by measurement, not inference: `python -m pytest tests/ -q` took the table from
**257 → 263 rows**, and bisection pinned it to `test_funding_arb_paper.py` (+3) and
`test_regime_arm.py` (+3).

## What it had done to the numbers

```
as stored                : n=269   net=$+653.61     <- what the dashboard reports
  synthetic "test" rows  : n=98    net=$+980.00
  duplicate extra copies : n=169   net=$-324.53
REAL distinct record     : n=2     net=$-1.85

  arm                     as stored          |  cleaned
  funding_aggr        159   0%   -$328.96    |   1   0%   -$2.07
  regime_intraday     110 100%   +$982.58    |   1 100%   +$0.21
```

98 rows carried `reason='test'` with a **hardcoded `net_pnl` of 10.0**, accumulating on
every test run since 2026-06-13. Separately, one funding_aggr fill was written **159
times** — identical symbol, size, fee and P&L, differing only in timestamp — turning a
~$2 cost into $329. The ledger reported **+$653.61**; the real distinct record is **2 fills
and −$1.85**.

Same class of bug as memory `trade_journal_csv_polluted`, different store. Worth assuming
it is a pattern in this repo rather than two accidents.

## Fix

`tests/conftest.py` claims the attribution singleton **before any test imports a module
that records**, pointing it at a throwaway temp file (the singleton is "first call wins",
so claiming it early is sufficient and changes zero production behaviour). Verified: the
suite now leaves the row count at 269 → 269.

`tests/test_attribution_isolation.py` guards it four ways — the singleton is not the prod
path, it is not anywhere under `data/`, a real `record()` call leaves the prod file's mtime
AND size untouched, and (the complement, so a silently broken `record()` can't pass by
writing nothing) the probe row is actually present in the temp ledger.

`scripts/attribution_forensics.py` reports stored-vs-real totals per arm and, with
`--apply`, removes test rows and collapses exact duplicates after writing a timestamped
`.bak`. **Run read-only only — the existing 267 junk rows are NOT deleted.** That is the
owner's P&L data; deleting it is their call, and the one-line command is in the PR.

**Verification:** full suite **3591 passed, 9 failed — all pre-existing** (test_bot_main 4,
test_dashboard 3, test_exchange 1, test_notifications 1). Row count unchanged across runs.

**Cross-lane note:** `tests/conftest.py` is shared infrastructure and the attribution
module is brain/risk/observability, while this session's other work is directional. The
conftest change is additive and touches nothing else; no production module was modified.

**Not done:** did not purge the junk rows (owner's decision). Did not audit whether other
singletons in this repo have the same "defaults to a production path" shape — `funding
history`, `data/trade_journal.*` and the paper-arm state files are the obvious candidates
and are worth the same check.
