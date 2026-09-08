---
date: 2026-09-07
agent: claude-computer
branch: fix/attribution-ledger-test-pollution
pr: 117
lane: brain/risk/observability
files: [src/session_filter.py, tests/conftest.py, tests/test_session_filter_source.py]
---

# The session gate was reading a constant, and reporting the inverse of the truth

Follow-on from the attribution-ledger fix. I had listed "audit whether other production
paths have the same shape" as not-done; this is that audit, and it found worse.

## It is a bug class, not one bug

Snapshotted all 84 files under `data/`, ran the suite, diffed. **`data/trade_journal.csv`
grows 15,838 bytes per run.** Root cause is a second, independent mechanism:
`src/trade_journal.py` exposes TWO module paths, `JOURNAL_FILE` (.json) and `CSV_FILE`;
`tests/test_trade_journal.py` monkeypatches only the first, so `append_csv()` kept writing
to production.

## What that did to a live gate

`src/session_filter.py` rated trading sessions from that CSV. By now it held **3665 rows,
3290 of them synthetic**, and — the part that matters — **every single row carried
`hour_utc = 12`**, including all 375 non-synthetic ones. So:

- Asia and US were **permanently n=0 → NEUTRAL**. The time-of-day gate was structurally
  incapable of ever measuring a time-of-day edge.
- EU, the one session it could see, was rated **FAVORABLE at 59.9% win**.

The real record lives in `data/trade_journal.json` — 228 trades spanning all 24 hours
(matches memory `directional_cost_bleed_fix`). Its honest verdict:

```
             live gate (CSV)          real record (JSON)
   Asia   n=0     NEUTRAL          n=20   0.0% win   UNFAVORABLE
   EU     n=3665  FAVORABLE 59.9%  n=112  1.8% win   UNFAVORABLE
   US     n=0     NEUTRAL          n=96   0.0% win   UNFAVORABLE
```

**The gate reported the inverse of the truth on the only session it could see**, and
`entry_checklist.py` down-scores on that rating today. CLAUDE.md documents
`SESSION_FILTER_HARD=1` to promote it to a hard veto "only after the by-session verdict
confirms FAVORABLE windows out-earn UNFAVORABLE ones" — a confirmation that could never
arrive, because only one window was ever populated.

## Fix

`session_filter.py` now screens synthetic ids (`id_*`, `BTC_17000*` — exactly what
`proof_scorecard._directional()` already dropped, which the gate never did) and prefers
`trade_journal.json`, falling back to the CSV only if the JSON is absent. It never merges
the two — they overlap, and double-counting would inflate every `n` on this gate.

`tests/conftest.py` redirects both journal paths, and now snapshots **the whole `data/`
directory** at session start and fails the run naming any file the suite modified — so the
next instance of this class is caught rather than discovered months later.

## Verification

- 17 new tests, including the two that pin the actual failure mode: a constant `hour_utc`
  column can only ever populate one session, and enough winning synthetic rows flip a
  losing session from UNFAVORABLE to FAVORABLE.
- **The directory guard was proven to fire**, not assumed: I disabled the CSV redirect,
  reran, and confirmed it named `data\trade_journal.csv` and set a failing exit status;
  then restored and confirmed it goes silent.
- Suite: **3593 passed, 9 failed — all pre-existing.** `data/` delta across a run: 0 bytes.
- All 27 pre-existing session-filter tests still pass unchanged.

## Behaviour change — read this before merging

This flips the live EU rating **FAVORABLE → UNFAVORABLE**. That is the correct direction
(the gate stops nudging toward a session on fabricated evidence) and it only tightens,
never loosens, so it does not violate the core principle. But note what it means: with all
three sessions UNFAVORABLE the gate has **no discriminating power** — it now down-scores
uniformly. Whether it should stay enabled at all is the owner's call; the honest reading is
that the directional record has no edge in any session, which is what every other line of
evidence in this repo already says.

**My own error, stated:** proving the guard fires required letting a run write to the CSV.
That added ~350 synthetic rows (1,054,684 → 1,070,522 bytes). The file is gitignored so I
could not revert it. It is noise on top of 3,290 existing junk rows, and after this fix
nothing live reads that file — but I caused it.

**Not done:** the polluted CSV is not cleaned (owner's data, owner's call; and it is now
unused by the gate). The other 82 files under `data/` are clean as of this run, but only
against *this* suite — a new test could still target one, which is what the guard is for.
