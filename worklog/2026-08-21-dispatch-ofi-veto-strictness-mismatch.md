---
date: 2026-08-21
agent: dispatch
branch: dispatch/document-ofi-veto-strictness-mismatch
pr: TBD
lane: directional
files: [src/scientific_strategy.py, src/entry_checklist.py, tests/test_entry_checklist.py]
---

# `_ofi_aligned` hard-vetoes on ANY opposing OFI, not just "strongly opposing" as documented — flagged, not fixed

Scheduled "continue the profitability mandate" pass. No live VPS/data access from this
sandbox (`data/` is gitignored, no SSH configured), so option (a) — analyzing live
forward-test results — wasn't possible this run. Baseline confirmed first:
`python -m pytest tests/ -q` → 3577 passed / **4** pre-existing failures (all
`tests/test_bot_main.py::TestMainSubsystemIsolation::*`, `src.bot` missing
`_run_funding_scanner` — not this lane, matches what PR #100 already flagged; the
"2 known pre-existing fails" note in CLAUDE.md/WORKLOG.md is stale, it's 4 now).

Dispatched an Explore audit specifically hunting the same bug *pattern* PR #100
(`dispatch/fix-cvd-trend-sign-semantics`, still open/unmerged) found in
`orderflow_ws.get_cvd_trend()`: a docstring/comment claiming one behavior while the
code does something measurably different. It surfaced one solid hit, confirmed by hand:

**`src/entry_checklist.py:232-244` `_ofi_aligned`** (hard check, wired into both the
long and short checklists at lines ~420/444) vetoes the trade whenever
`ctx.sig.ofi_score < 0` — **any** negative score. But `scientific_strategy.py`'s
`ofi_score` computation (lines ~285-300) has three opposing tiers: magnitude ≥0.25 →
-15, ≥0.15 → -8, and **magnitude <0.15 (noise-level) → -3**. The module docstring this
strategy ships with says entry requires "OFI must not be **strongly** opposing." In
practice a high-confidence setup with `ofi=-0.02` (essentially flat/noise, barely on
the wrong side of zero) gets hard-blocked identically to `ofi=-0.35` (a real, strong
opposing print). The existing test (`test_fails_when_ofi_score_negative`, `ofi=-0.5,
score=-10.0`) only exercised the strong case, so it never caught the mismatch.

**Why I didn't change the gate's behavior:** loosening `_ofi_aligned` to only block on
the strong tiers (e.g. `ofi_score <= -8`) would let more trades through in the weak-
opposition band — exactly the shape of change CLAUDE.md's Core Principle and this run's
own task prompt call out by name: *"do NOT loosen atr_alive or other cost-aware gates
to force more trades, that recreates the historical ~1% win rate bug."* I don't have
forward-test evidence either way for this specific threshold, and a scheduled run with
no human in the loop is the wrong place to make that call unilaterally. So this PR is
documentation + a pinning test only, zero behavior change:

- `scientific_strategy.py` module docstring: corrected the "must not be strongly
  opposing" line to say what the code actually does (blocks on any negative
  `ofi_score`, fail-open only when OFI itself is `None`).
- `scientific_strategy.py` `CONFIDENCE_TIERS` comment block: separately found to be
  stale too — it said "<60: no trade" with a different multiplier set than the actual
  table (which sizes trades down to confidence 38 at 0.2x). Corrected to match the
  table verbatim; this is a plain doc fix, unrelated to the OFI finding.
- `entry_checklist.py::_ofi_aligned`: added an inline comment stating the actual
  behavior and pointing at this worklog entry, so the next person reading the function
  doesn't have to re-derive the divergence.
- `tests/test_entry_checklist.py`: added
  `test_fails_even_on_weak_noise_level_opposition_not_just_strong` — pins current
  behavior (`ofi=-0.02, ofi_score=-3.0` still fails the check) so a future change to
  this threshold is a deliberate, visible diff, not an accidental behavior change.

**Recommendation for the owner / next agent with forward-test access:** if
`data/trade_journal.csv` on the VPS shows the directional side is idle partly because
of this hard OFI veto firing on noise-level opposition, the fix is a one-line threshold
change (`ofi_score <= -8` instead of `< 0`, matching scientific_strategy's own -8/-15
"opposing" tiers) — but it should go out as its own PR with before/after funnel-log
`[FUNNEL]` counts or a forward-test window, not bundled with this doc/test change.

**Verification:** `python -m pytest tests/test_entry_checklist.py
tests/test_scientific_strategy.py -q` → 206 passed. Full suite:
`python -m pytest tests/ -q` → 3578 passed (was 3577), same 4 pre-existing
`test_bot_main.py` failures, unrelated to this change (not in the directional lane).
No `.env`/`config.yaml` touched.
