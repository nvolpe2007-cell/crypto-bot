---
date: 2026-08-24
agent: dispatch
branch: dispatch/fix-dual-direction-probe-noop
pr: TBD
lane: directional
files: [src/paper_trading.py, tests/test_paper_trading.py]
---

# Fix: dual-direction probe's flip/reject verdict was silently discarded

No VPS/data access from this cloud session (`data/` is gitignored, lives only on the
Hetzner box), so task (a) — analyzing recent forward-test results — wasn't possible this
run. Also checked the two still-open, still-draft dispatch PRs (#100 `fix-cvd-trend-sign-
semantics`, #101 `document-ofi-veto-strictness-mismatch`) so as not to duplicate their
findings; both remain unmerged and out of scope for this run since the owner hasn't acted
on them yet. Confirmed their flagged baseline drift still holds: `python -m pytest tests/ -q`
→ 3577 passed / 4 failed on a clean master (before my change), all in
`tests/test_bot_main.py::TestMainSubsystemIsolation::*` against `src/bot.py`
(`_run_funding_scanner` missing) — not my lane, not touched, not re-logging since #100/#101
already flagged it.

Fell back to (b): had a general-purpose subagent audit the directional lane
(`paper_trading.py`, `scientific_strategy.py`, `entry_checklist.py`, `live_trading.py`,
`pairs_strategy.py`, `orderflow_ws.py`, `indicators.py`) for the same "docstring says X,
code does Y" bug pattern #100/#101 already found instances of, excluding the two already-
fixed spots. Verified its top finding myself before acting on it.

## The bug

`run_paper_trading_session`'s "dual-direction probe" (`src/paper_trading.py`, around line
1930) re-evaluates the opposite trade direction through the probability gate and is
supposed to do one of three things: flip the signal if the opposite direction clearly wins,
reject the whole bar if both directions clear the gate within a margin ("noisy/contradictory
tape"), or leave it unchanged. It applied the flip/reject verdict like this:

```python
sig.is_buy  = (not _orig_buy)
sig.is_sell = _orig_buy
...
sig.is_buy = False
sig.is_sell = False
```

`ScientificSignal.is_buy`/`is_sell` (`src/scientific_strategy.py`) are `@property` getters
with no setter — they're derived from `.signal` and `.size_mult`. Assigning to them raises
`AttributeError: property 'is_buy' of 'ScientificSignal' object has no setter`, every single
time either branch fires. That exception was caught by the surrounding broad
`except Exception as _e: logger.debug(...)`, so it never surfaced as an error — meanwhile
the `[DUAL-FLIP]`/`[DUAL-REJECT]` info-level log lines and (for reject) the
`funnel.bump('skip:dual_noisy')` counter fire *before* the failed assignment, so the logs
and funnel stats claimed the verdict was applied when the original, unmodified signal
actually went on to the entry checks unchanged.

Net effect: the "reject contradictory tape" case — arguably the more important of the two,
since it's meant to veto a bar the probability gate itself is unsure about — never actually
vetoed anything. The "flip to the direction the gate scored higher" case never flipped
anything either; the bot would trade the (gate-scored-worse) original direction while
logging that it had switched to the better one. `MicrostructureSignal` inherits the same
read-only properties, so this isn't limited to the scientific-strategy fallback path.

This is a **gate that reads as active in the logs but is a no-op** — squarely the "weaker
than documented" category CLAUDE.md's Core Principle warns about, not a case of me loosening
anything: fixing it makes an already-intended-to-exist gate actually run. Blast radius today
is bounded by `DIRECTIONAL_ENABLED` defaulting to `'0'` (directional entries are shelved
per CLAUDE.md), but `DUAL_DIRECTION_ENABLED` itself defaults to `'1'`, so the bug fires
immediately and silently the moment anyone flips `DIRECTIONAL_ENABLED=1` for testing.

## The fix

Extracted the decision + mutation into a new pure helper, `_resolve_dual_direction(sig,
orig_buy, orig_p, opp_p, orig_rejected, opp_rejected, dual_margin) -> str` (returns `'flip'`
/ `'reject'` / `'unchanged'`), which mutates `sig.signal` (a real, settable dataclass field)
instead of the read-only properties — `sig.signal = Signal.SELL/BUY` for a flip, `Signal.HOLD`
for a reject. `is_buy`/`is_sell` then read correctly off the new `.signal` value automatically.
The call site now calls the helper and branches on its return value for logging/funnel
bookkeeping, with identical log text/funnel-bump behavior to before — only the (broken)
mutation logic changed. No thresholds, margins, or gate strictness were touched.

## Tests

New `TestResolveDualDirection` class in `tests/test_paper_trading.py` (6 tests): flip
buy→sell, flip sell→buy, reject on contradictory-within-margin, unchanged when original
clearly leads, unchanged when the "winning" opposite direction was itself rejected by the
gate, and an explicit regression guard that pins `sig.is_buy = False` as still raising
`AttributeError` directly (proving the fix goes through `.signal`, not the properties).

`python -m pytest tests/test_paper_trading.py -q` → 87 passed (was 81).
`python -m pytest tests/ -q` → 3583 passed / 4 failed — same 4 pre-existing `test_bot_main.py`
failures as the clean-master baseline above, none new.
