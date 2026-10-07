---
date: 2026-10-07
agent: dispatch
branch: dispatch/fix-scientific-strategy-trending-up-buy-mirror
pr: pending (opened this run)
lane: directional
files: [src/scientific_strategy.py, tests/test_scientific_strategy.py]
---

# `_has_buy_signal` had no `TRENDING_UP` mirror of the `TRENDING_DOWN` sell-only clause

## What I did this run

1. `git fetch && checkout master && pull` — up to date (last master commit 2026-09-28,
   merge of #146/#145/#147).
2. Read `CLAUDE.md` + `WORKLOG.md` + `python scripts/worklog_index.py`.
3. Checked the open PR backlog (GitHub API): **29 open PRs** (#100-#156) touching the
   directional lane, oldest (#100) 47 days unmerged, none merged since. The most recent
   run (2026-10-06, PR #156) had already escalated this backlog/draft-state problem to the
   owner directly and explicitly noted "every lane bug a status pass can find by reading is
   already filed in the existing 29" — so a 7th status pass finding the same thing would be
   pure noise, not new signal. I did not repeat that escalation.
4. Given (a) is infeasible from this cloud session (no VPS access — `data/` with the live
   trade journal / paper-trading state isn't in the git repo), and (c) would just be status
   pass #7, I went with (b): delegated a focused subagent to read the 7 lane files harder
   than previous passes, explicitly given the full list of ~25 already-filed findings so it
   wouldn't rediscover them, with instructions to look especially hard at `indicators.py` and
   `pairs_strategy.py` (fewest prior findings — possibly under-mined, not actually clean).
5. Baseline `pip install -r requirements.txt` + `pip install fastapi` (missing from a fresh
   container; needed for `test_bot_main.py`/`test_dashboard.py` to even collect) →
   `python -m pytest tests/ -q`: **3634 passed, 0 failed** (matches #156's figure exactly).

## What I found and fixed

**`src/scientific_strategy.py::ScientificStrategy._evaluate`, nested `_has_buy_signal` /
`_has_sell_signal`** (around line 220-234). `_has_sell_signal` has an extra clause:

```python
if regime == 'TRENDING_DOWN' and adx_v > 22: return True
```

forcing a SELL candidate purely from regime+trend-strength, with no OFI/lead-lag/EMA-cross/
RSI support needed. `_has_buy_signal` had no mirror for `TRENDING_UP`. The comment
immediately above both functions states the intent explicitly: *"This ensures we find shorts
in downtrends and longs in uptrends without manually hard-coding regime→direction rules"* —
but the code only hard-coded the downtrend half. Confirmed via `git log -p -S` this has been
present since the file's original commit (PR #54, 2026-07-17); not a regression.

Effect: a strongly trending-down market with ADX>22 always reaches a candidate SELL
evaluation (and the downstream confidence/cost gates). An equally strong trending-up market
only reaches a candidate BUY evaluation if OFI/lead-lag/EMA-cross/RSI happen to independently
agree — otherwise `has_buy=False and has_sell=False` and `_evaluate` returns HOLD immediately
(line ~239), even though the regime-based tiebreaker logic a few lines further down
(`elif regime == 'TRENDING_UP': direction = 'BUY'`) shows the rest of the function already
treats `TRENDING_UP`/`TRENDING_DOWN` symmetrically — it just never gets the chance to run.
A structural long/short asymmetry, not a cost-gate change.

**Fix:** added the mirrored clause to `_has_buy_signal`:
```python
if regime == 'TRENDING_UP' and adx_v > 22: return True
```
This only changes which candidates reach the *existing* downstream `entry_checklist` /
`probability_gate` cost-and-confidence filters — it adds no new trigger that bypasses them,
and doesn't touch `atr_alive` or any other cost-aware gate, so it's in bounds under CLAUDE.md's
"do not loosen gates to force more trades" principle: it corrects which side gets evaluated
at all, not how permissive the evaluation is.

## Verification

- Added two regression tests to `tests/test_scientific_strategy.py`
  (`test_trending_down_high_adx_forces_sell_with_no_other_signal`,
  `test_trending_up_high_adx_forces_buy_with_no_other_signal`) that monkeypatch
  `ta.ema`/`ta.rsi`/`ta.adx` to pin every other signal neutral, isolating this one clause.
- Confirmed the new TRENDING_UP test fails pre-fix (`git stash` the source change only):
  `AssertionError: Signal.HOLD == Signal.BUY`. Passes post-fix.
- `python -m pytest tests/test_scientific_strategy.py -q` → 58 passed.
- `python -m pytest tests/ -q` (full suite) → **3636 passed, 0 failed** (3634 baseline + 2
  new tests, no regressions, no new failures).

## Why this is safe / in scope

Directional lane (`scientific_strategy.py` is explicitly listed). Pure candidate-generation
symmetry fix — doesn't touch `entry_checklist.py`'s `atr_alive`/cost/kill gates, doesn't
change confidence scoring, doesn't change position sizing. Small, isolated, test-covered.

## Backlog note (not re-escalating, just logging for the next run)

29 open directional-lane PRs, oldest 47 days, confirmed unchanged from #156's count as of
this run's start. #156 already escalated this to the owner via direct notification
yesterday; repeating that notification today would be noise since nothing has changed since
then. Not sending another one. Next run: if the backlog is still unchanged, consider skipping
the notification again too — only re-escalate if something about the situation actually
changes (backlog shrinks, grows further, or a new category of problem shows up).
