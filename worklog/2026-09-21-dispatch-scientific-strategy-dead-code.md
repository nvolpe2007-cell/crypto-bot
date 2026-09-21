---
date: 2026-09-21
agent: dispatch
branch: dispatch/scientific-strategy-dead-code
pr: (opened this run)
lane: directional
files: [src/scientific_strategy.py]
---

# `ScientificStrategy._evaluate`: two dead-code spots, no behaviour change

Continuing the autonomous profitability mandate this run. No VPS/data access from this
cloud session (no `data/` directory, no SSH), so option (a) — analyzing live forward-test
results — wasn't possible; went with option (b), a small in-lane cleanup.

**Context:** the directional lane (`paper_trading.py`, `scientific_strategy.py`,
`entry_checklist.py`, `live_trading.py`, `pairs_strategy.py`, `orderflow_ws.py`,
`indicators.py`) is heavily mined — 25 open PRs at time of writing, most already covering
the obvious bugs in these files (see `python scripts/worklog_index.py` for the list).
Delegated an Explore pass over the six non-`entry_checklist.py` lane files, explicitly
fed the list of already-open-PR findings to avoid duplicating them. It also flagged that
`OrderFlowWS` (`orderflow_ws.py`) is still not wired into either trading loop — true, but
already known and already documented in CLAUDE.md ("Maker-only microstructure" section,
"NEXT STAGE — needs on-VPS WS testing") and in WORKLOG.md's 2026-06-29 entry, so not a new
finding and not actionable from this sandbox anyway. The two genuinely new things it
surfaced, both in `ScientificStrategy._evaluate` (used by `live_trading.py`):

1. **Line 352 (pre-fix): `annual = funding_rate * 3 * 365 * 100` was computed and never
   read.** `funding_score` is assigned entirely from fixed absolute-rate thresholds
   (`-0.001`, `0.0005`, `0.001`), never from `annual`. Removed the dead line — pure
   dead-code deletion, no scoring behaviour changed. (Deliberately did *not* rewire
   `funding_score` to scale continuously off `annual` — that would change live confidence
   scores/position sizing on a path this run has no data to validate; out of scope for a
   safe change.)
2. **Line 267 (pre-fix): `elif regime in ('TRENDING_DOWN', 'CRASH'):` — the `'CRASH'` arm
   was unreachable.** This branch only runs when both `has_buy` and `has_sell` are `True`.
   But the hard block just above (lines 247–251) always clears `has_buy` to `False` (or
   returns HOLD outright) whenever `regime == 'CRASH'` — so by the time this tie-break
   runs, `regime == 'CRASH'` can never coexist with `has_buy == True`. Narrowed the
   condition to `regime == 'TRENDING_DOWN'` and left a comment explaining why `'CRASH'`
   can't reach here, so a future pass doesn't have to re-derive it.

Both are no-op removals/renames — same set of `(regime, has_buy, has_sell)` inputs produce
the same `direction`/`funding_score` as before. Chose this over the actual live-loop
wiring gap on `OrderFlowWS` because that requires on-VPS WS testing per CLAUDE.md, which
this cloud session cannot do, and because CLAUDE.md's Core principle path (signal quality
tightening) needs data this session doesn't have access to validate against.

**Verification:** `python -m pytest tests/test_scientific_strategy.py -q` → 56 passed.
Full suite: `python -m pytest tests/ -q` → 3608 passed, 0 failed (ran under a fresh
python3.12 venv with `pandas-ta` installed from PyPI, since this sandbox's default
python is 3.11 and has no pandas-ta build available — same effective test set the repo's
CI matrix runs).
