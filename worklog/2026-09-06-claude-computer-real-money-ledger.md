---
date: 2026-09-06
agent: claude-computer
branch: feat/real-money-ledger
pr: pending
lane: brain/risk/observability (cross-lane — see note)
files: [scripts/real_ledger.py, proof_scorecard.py, tests/test_real_ledger.py]
---

# Real-money ledger: judge the owner's manual trades on the same bar as the paper arms

Owner is going to trade the Trend Signal (Honest) rule by hand with real money —
alert fires on the confirmed daily close, they buy; SELL alert fires, they exit.
The rule already carries its own exit (it is long/flat, `pine/trend_signal_honest.pine`),
so nothing needed building there. What did not exist was any way to find out, later,
whether it worked. Trading memory is systematically flattering; this makes it a file.

`scripts/real_ledger.py` records actual fills (`buy` / `sell` / `skip` / `status` /
`report`) into `data/real_money_ledger.json`, and `proof_scorecard._real_money_forward()`
judges them against the unchanged pre-registered bar (n≥30, expectancy>0, clustered t>2),
entry-week clustered like the swing arm since the basket is correlated majors plus ETFs.

## Two things this measures that no paper arm can

**Execution drag.** Every trade stores the signal price (the close that fired the alert)
AND the price actually filled. The backtest charged a flat 0.54% round-trip; `report`
prints what was really paid and flags the gap in dollars. In a smoke test with 24bps of
entry slippage the realised cost came out 1.44% — nearly 3x the assumption. If that is
what live execution looks like, the assumption every arm in this repo shares is wrong,
and this is the only instrument that would ever tell us.

**Discipline.** The 12/12 result says filtering this rule raises win rate and destroys
expectancy, and a human in the loop is a filter. So `skip` records signals not taken, and
`exit_reason != 'signal'` records positions closed on a hunch. `_verdict` checks those
BEFORE the statistics and returns `NOT JUDGED` if either is non-zero — an off-rule record
is a different strategy, and a t-stat computed on it does not mean what it says. Tested
with an overwhelming t=9.0: still `NOT JUDGED`. This is deliberately the one place the
scorecard refuses to produce a number.

## Pre-registration, and why it does not move anyone else's bar

The arm is marked `pre_registered=True` and **excluded from `k`**. Šidák exists to punish
best-of-k cherry-picking; this rule was committed to in advance, before any trade existed,
so there is nothing to correct for — it is judged at the single-arm t>2. Counting it in
`k` would tighten every other arm's family bar merely because the owner started trading,
which is backwards. Memory `proof_bar_family_wise` notes k inflation cuts against the live
arms; this is the same concern from the other direction.

Net is recomputed from the recorded fills on every read, so a mistyped price or fee is
fixed by editing the JSON — a stale stored `net_usd` is ignored (tested).

**Verification:** 20 new tests pass. Full suite: 3592 passed, **9 failed — all
pre-existing** (test_bot_main 4, test_dashboard 3, test_exchange 1, test_notifications 1),
none in files touched here; matches the count in memory `ci_branch_protection`. Smoke-
tested the CLI round-trip against a scratch data dir. `proof_scorecard.py` runs clean.

**Cross-lane note:** `proof_scorecard.py` is a brain/risk/observability file and this
session's other work is directional. The change is additive — one new arm function, one
`k` computation, one early return in `_verdict` gated on keys only this arm sets. No
existing arm's numbers or verdicts move.

**Pre-existing, not fixed (unrelated):** `python proof_scorecard.py` crashes on a Windows
cp1252 console on the `▌` glyph in `main()`. Reproduced on master before these changes.
Workaround `PYTHONIOENCODING=utf-8`; left alone as another lane's file and not this PR's
subject.

**Not done:** no alert→ledger automation — entries are typed by hand, on purpose, since
the fills come from a broker UI the bot cannot see. Not wired into `weekly_report.py`
(it calls `build_arms()`, so the arm appears there automatically once trades exist, but
the discipline counters are not surfaced in that report's §7 yet).
