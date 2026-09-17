---
date: 2026-09-08
agent: claude-computer
branch: fix/ci-orphaned-bot-main-tests
pr: pending
lane: brain/risk/observability
files: [tests/test_bot_main.py]
---

# CI has been red since 2026-08-09, blocking every open PR — root cause and fix

Owner asked to merge the crypto indicator (PR #113). It was `mergeable` with no conflicts
but `mergeStateStatus: BLOCKED` — required checks failing on all three Python versions.
Not caused by that PR: **master's CI has been red for a month**, so none of the six open
PRs could merge. Fixing this is the prerequisite, not a detour.

**Root cause.** `tests/test_bot_main.py` patches `src.bot._run_funding_scanner`, which
commit **8104e46 — "Remove duplicate funding-scanner task from src/bot.py main()"** —
deliberately deleted. The scanner had been running *twice*; the copy that survives is in
`src/paper_trading.py`, still wrapped in `supervised()`. `main()` now gathers two
subsystems, not three, so all four tests died with
`AttributeError: module 'src.bot' has no attribute '_run_funding_scanner'`.

CI reported exactly **4 failed, 3577 passed** — every failure was this one stale patch.

**Fix.** Three of the four tests only needed the dead patch line removed; their subject
(dashboard and bot crash isolation) is unchanged and still real. The fourth was *entirely*
about the scanner in `main()`, so it is replaced by one asserting `src.bot` has **not**
regrown a scanner — if it ever does, the double-scan bug 8104e46 fixed is back.

**The part I did not want to just delete.** That fourth test guarded a real guarantee: a
funding-scanner crash must not kill the process. The guarantee moved with the code; it did
not disappear. Deleting the test would have silently dropped the coverage, so
`TestFundingScannerSupervisionMoved` now checks the surviving scanner in
`paper_trading.py` is still under `supervised()`, that all four funding arms are (they
share one failure domain — one bare launch propagates to the others), and that the scanner
is launched **exactly once**, which is the specific bug 8104e46 fixed.

Those three are **source-level wiring checks, not behavioural ones** — exercising them for
real means standing up the whole paper-trading loop. The class docstring says so plainly,
because they prove supervision is still *wired*, not that it *works*, and wiring is the
thing that silently regresses.

**Verification:** `tests/test_bot_main.py` 4 → 7 tests, all pass. Full local suite **3579
passed, 5 failed** — the 5 are Windows-only (test_dashboard 3, test_exchange 1,
test_notifications 1) and CI's Linux runners never reported them; CI's only failures were
the 4 fixed here.

**Cross-lane note:** `tests/test_bot_main.py` covers `src/bot.py`, a brain/observability
file, while this session's other work is directional. Test-only change; no production
module touched.

**Not done:** the 5 Windows-only local failures are untouched — they do not block CI and
each looks like a separate platform issue (path/encoding), worth its own pass rather than
being bundled into an unblock-the-gate PR.
