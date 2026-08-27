---
date: 2026-08-27
agent: dispatch
branch: dispatch/fix-stale-bot-main-tests
pr: TBD
lane: shared
files: [src/bot.py, tests/test_bot_main.py]
---

# `tests/test_bot_main.py` was pinned to a subsystem `main()` doesn't gather anymore

**Cross-lane note:** `src/bot.py` and `tests/test_bot_main.py` aren't listed under either
lane in WORKLOG.md's lane map — treating as shared/neutral (entry-point + its own test
file, same category as `run_all_bots.py`). Small, mechanical, behavior-preserving diff
(no logic in `src/bot.py` changed, only a comment; the test file is updated to match
already-current code). Flagging here per the coordination rule rather than assuming.

## Why this run touched it

This is a second, incidental PR from this run — my main task (see
`worklog/2026-08-27-dispatch-fix-whale-average-self-inclusion.md`) opened PR #105, whose
CI failed with the same 4 `tests/test_bot_main.py` failures every open dispatch PR
(#100–#104) has independently hit and had to flag as "pre-existing, not mine" in its own
worklog since 2026-08-20. Per the PR-babysitting rule for a PR I opened ("CI red ... when
[the failure is] in code unrelated to the change, port a fix that exists ... and only when
none exists say what is failing and why, with a proposed patch, rather than widening the
PR"), no fix existed anywhere yet, so I produced one as its own dedicated PR (this one) and
ported the identical commit into PR #105 so it goes green immediately without waiting on
this one to merge.

## Root cause

`tests/test_bot_main.py` was added in `b1d1f80` (PR #59, crash-isolation for `main()`'s
three gathered subsystems: bot, dashboard, funding scanner). A later commit, `8104e46`
("Remove duplicate funding-scanner task from src/bot.py main()"), deleted the
`_run_funding_scanner` subsystem from `main()` — its own commit message explains why: it
built a second `FundingScanner` + `FundingArbPaperSim` that bypassed
`FUNDING_ARB_ENABLED`/`FUNDING_ARB_AGGRESSIVE_ENABLED` and raced `paper_trading.py`'s
properly-gated multi-arm wiring on the same `data/state.json` keys — a leftover predating
the 2026-06-15/16/17 multi-arm restore. That commit's own full-suite run showed "2914
passed, 0 failures", so `test_bot_main.py` must not have been rebased against it at the
same time; the merge commit `81cce2d` ("merge PR #60 ... resolved atop #59 supervised
gather") is where the two diverged without anyone catching it — a plain instance of the
`multi_agent_master_races` pattern CLAUDE.md's memory note warns about, just never chased
down. 3 of the 4 tests in the file monkeypatch `bot_mod._run_funding_scanner`, which
doesn't exist, so every one of them (and the 4th indirectly, since all four share
`_patch_common`) has failed with `AttributeError` on every commit since — including the
`8104e46` commit itself, apparently unnoticed downstream.

**Confirmed not a live-path bug either way:** `8104e46`'s own message notes `src/bot.py`'s
`main()` isn't on the production path — `run_all_bots.py` (the VPS systemd entry point)
calls `ScalpingBot.start()` directly and never invokes this `main()`. So neither the
original removal nor this test fix touches anything the VPS actually runs.

## The fix

- `tests/test_bot_main.py`: dropped the 3 `monkeypatch.setattr(bot_mod,
  "_run_funding_scanner", ...)` lines and the now-nonsensical
  `test_funding_scanner_crash_does_not_crash_main` test entirely (there's no such
  subsystem left in `main()` to test), renamed
  `test_clean_exit_of_all_three_returns_normally` → `test_clean_exit_of_all_returns_normally`,
  and rewrote the module docstring to describe the current two-subsystem gather and to
  record why the funding-scanner references existed and were removed (so the next reader
  doesn't have to `git log` it again).
- `src/bot.py`: corrected an in-code comment on `main()` that still claimed a crash would
  "take the live/paper trading loop and funding scanner down with it" — no longer true,
  since the funding scanner isn't gathered here. Added a one-line pointer to `8104e46` for
  context. No logic changed.

## Verification

- `python -m pytest tests/test_bot_main.py -q` → 3 passed (was 1 passed / 3 failed).
- `python -m pytest tests/ -q` → **3581 passed, 0 failed** — first zero-failure full-suite
  run in the worklog history checked this run (every prior entry since PR #100 on
  2026-08-20 recorded exactly these same 4 failures as pre-existing baseline noise).
- The stale "2 known pre-existing fails" note in CLAUDE.md/WORKLOG.md — already flagged as
  outdated (it became 4) by PRs #100/#101 — is now moot; suggest updating that line to "0
  known" once this merges, or removing the caveat entirely.

## Also pushed to PR #105

Same commit ported directly onto `dispatch/fix-whale-average-self-inclusion` (PR #105,
opened earlier this run) so its CI goes green now instead of waiting on this PR to merge
first — it'll no-op cleanly once master carries this fix too. Noted on PR #105 as a
comment per the CI-red protocol.
