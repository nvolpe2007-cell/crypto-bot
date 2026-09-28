---
date: 2026-09-13
agent: dispatch
branch: dispatch/fix-scientific-strategy-stale-position-size-docs
pr: pending
lane: directional
files: [src/scientific_strategy.py]
---

# scientific_strategy.py's position-size docs described a retired tier scheme

## Backlog check first

`git fetch` + read `WORKLOG.md` + `python scripts/worklog_index.py`: master is unchanged
since #126/#127 (still `dc7b95d`). The directional-lane queue is the same 9 PRs (#100–#105,
#121, #123, #126) — all independently tested, all still waiting on the human owner to merge.
Per #127's own reasoning, re-sending that status (with zero new information) would just be
noise, so this run did not repeat it. Instead: looked for one new, small, safe thing in the
lane that the last ~10 runs hadn't already found.

## What's wrong

`src/scientific_strategy.py` lines 33–39 (a header comment) and 115–121 (the
`compute_position_size` docstring) describe an **older** confidence-tier scheme that no
longer matches the live `CONFIDENCE_TIERS` table three lines below the comment, or the live
`BASE_EQUITY_PCT = 0.06`:

- Stale comment claimed boundaries at 60/80/90/93/97 with multipliers .5/.8/1.0/1.4/1.8.
- Live table (already correctly annotated inline, right next to the stale header) has
  boundaries at 38/45/60/75/85/93/97 with multipliers .2/.3/.5/.7/1.0/1.5/2.0.
- Docstring claimed "$100 equity, 93% confidence → ~$5.60" and "$500 equity → ~$28" — the
  real numbers (confirmed by `tests/test_scientific_strategy.py::test_93_confidence_size`,
  which already asserts `compute_position_size(93.0, 100.0) == 9.0`) are **$9.00** and
  **$45.00**.

Concrete failure mode: anyone — human or another agent — sanity-checking real risk-per-trade
from this docstring before touching `BASE_EQUITY_PCT` or `CONFIDENCE_TIERS` would read "93%
confidence risks ~5.6% of equity" and conclude the bot is more conservative than it actually
is (it risks 9%, and up to 12% at 97-100%, capped at `MAX_EQUITY_PCT=15%`). That's exactly
the kind of risk-exposure misjudgment CLAUDE.md's cost/risk framing exists to prevent.

## Fix

Pure documentation correction — rewrote the header comment to match the tiers already
correctly annotated inline in `CONFIDENCE_TIERS`, and corrected the two docstring examples
to the numbers the code (and its existing test) actually produces. Zero behavior change,
zero gate touched — nothing here loosens `atr_alive` or any cost-aware filter.

## Test plan

- `/tmp/cbvenv` (fresh Python 3.12 venv, since this sandbox's default `python3` is 3.11 and
  `pandas-ta` here requires ≥3.12 — matches one leg of the CI matrix): installed
  `requirements.txt` + `pandas-ta` + `pytest`.
- `python -m pytest tests/ -q` → **3584 passed, 0 failed** — matches #126/#127's documented
  master baseline exactly; no new failures introduced.
- `tests/test_scientific_strategy.py` already covers `_size_multiplier` and
  `compute_position_size` at every tier boundary and confirms the real (now-documented)
  values; no test changes needed, existing coverage was already right — only the prose
  around it was wrong.
