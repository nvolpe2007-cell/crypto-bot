---
date: 2026-10-05
agent: dispatch
branch: dispatch/live-trading-missing-atr-alive-gate
pr: 155
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# live_trading.py's real-money entry path never ran the atr_alive cost floor

Found a genuinely new bug in the directional lane, distinct from the ~23 small bugs
already sitting in the open dispatch PR backlog (#100-#154): `src/live_trading.py` — the
path that places real orders on Kraken — never enforced the `atr_alive` cost-aware
volatility floor that `paper_trading.py` applies to every entry via
`entry_checklist.py`.

`atr_alive` exists to refuse setups whose ATR can't clear the ~0.3% round-trip cost
(CLAUDE.md's "Core principle" — these are structurally negative-EV, they can only bleed
out via fees). The live entry gate only checked `confidence >= LIVE_MIN_CONFIDENCE` (70)
plus kill switch / daily-loss halt / sentiment — nothing about whether the market even
had enough volatility to clear costs. A 70+ confidence signal could fire with real money
in the exact dead-volatility conditions paper already refuses.

**Fix:** added `_atr_alive(sig)` in `live_trading.py`, an exact mirror of
`entry_checklist._atr_alive`'s threshold (`_ATR_ALIVE_FLOOR`) and fail-open semantics
(fails open when ATR/price is missing, same as paper — so no existing legitimate trade
pattern is blocked). Added one veto check in the LONG ENTRY block. No SHORT entry path
exists yet in `live_trading.py` (shorts aren't wired — see open PR #103), so only LONG
needed it.

This is strictly a missing-veto fix — it can only reduce trade frequency/risk on the live
path, never increase it, so it doesn't run afoul of "don't loosen gates to force more
trades."

**Verification:** added `TestAtrAlive` (5 cases: above/below/at floor, missing-ATR and
missing-close fail-open) to `tests/test_live_trading.py`. `python -m pytest tests/ -q` →
3639 passed, 0 failed (baseline before this change: 3634 passed, 0 failed — note this is
cleaner than the "2 known pre-existing fails" CLAUDE.md mentions; both appear to already
be fixed upstream). Checked all open `dispatch/*` branches for overlap on this file/check
— none found.

**Backlog note (not new — flagging once more since it's now material to this lane's
output):** as of this run there are still 26 open PRs on this repo, the oldest
(`dispatch/fix-cvd-trend-sign-semantics`, #100) open 47 days with zero review activity.
Every prior dispatch run since 2026-09-08 has independently re-confirmed the backlog is
unreviewed, not conflicting or broken (see worklog 2026-09-22, 2026-09-25, 2026-10-02,
2026-10-03 entries) — this is the fifth+ time it's been noted in-repo with no change in
outcome. Not re-litigating root cause again here; just noting PR #155 adds to that same
queue, and the dispatch mandate's value is capped by review throughput, not by the
backlog's lack of vocabulary to describe itself.
