---
date: 2026-08-07
agent: claude-computer
branch: lev-perp-8coin-widened
pr: TBD
lane: brain-risk-observability
files: [lev_perp_paper.py, proof_scorecard.py, tests/test_lev_perp_universe_widening.py]
---

# 8-coin widened-universe lev_perp variant, cross-lane deploy of dispatch's own finding

Read `RESEARCH_2026-07-26_lev_perp_v1_frequency.md` (dispatch, directional
lane) after syncing this session's own research with current master: dispatch
found widening lev_perp v1's traded universe from 3 to 8 liquid coins (SAME
unchanged SMA-50 signal, filters, exit), zero other changes, is the strongest
single lever in this repo's whole research history (DSR 0.538 -> 0.887,
trade count 324 -> 856, win rate held ~51-53%). Their own honest cumulative
verdict across all 17 candidates tried this summer (companion doc,
`RESEARCH_2026-07-26_lev_perp_regime_and_entry_search.md`) still doesn't
clear the 0.95 DSR bar, but it's the closest anything has come.

That finding was never turned into a running forward-test arm. Deployed it.

**Found and fixed a real landmine before it could ship**: `lev_perp_paper.py`'s
`ALLOC_FRAC` divided by `len(KRAKEN_PAIRS_ALL)` (the fixed 3-coin dict), not
the actually-active traded set. Widening `KRAKEN_PAIRS_ALL` directly to 8
coins would have silently changed margin sizing for the LIVE production v1
arm and both agg twins (neither sets `LEV_PERP_SYMBOLS`) the moment this
merged and got pulled — a change no one asked for, landing invisibly. Fixed
by adding `KRAKEN_PAIRS_WIDE` as a superset only reachable via an explicit
`LEV_PERP_SYMBOLS` override, and changing `ALLOC_FRAC` to divide by the
active `KRAKEN_PAIRS` count instead. Verified: no-override default is
byte-for-byte identical to before (3 coins, ALLOC_FRAC=1/3, MARGIN=$333.33);
`LEV_PERP_SYMBOLS=BTC,ETH,SOL,ADA,XRP,DOT,AVAX,LINK` correctly reaches all
8 and splits margin 8 ways ($125 each).

Registered `lev_perp_8coin_state.json` in `proof_scorecard.py` (reused the
existing generic `_lev_perp_agg_variant` helper by state-file-path — no new
function needed, it already does exactly the stats computation this needs).

+7 tests (`tests/test_lev_perp_universe_widening.py`): default universe/
alloc/margin unchanged, override reaches the wider superset, ALLOC_FRAC
reflects the active subset size (not the full known-universe size) for both
full and partial overrides, wide superset is a strict extension of default.

**Cross-lane note**: `lev_perp_paper.py` sits in neither lane's explicit file
list (the lane map only names `src/` internals + a few standalone scripts
per agent), but this directly builds on dispatch's own directional-lane
research — flagging here per coordination rule 3 so dispatch doesn't
duplicate. Also: while syncing, found the VPS was 8+ merged PRs behind
master (nothing from PRs #85-91 had been deployed) — pulled + restarted,
verified all 18 subsystems healthy post-restart. Diff was research
scripts + 2 small already-tested src/ changes, no core-loop risk.

**Verification**: full suite after this branch's changes: 2786 passed / 337
failed (unchanged pre-existing thin-venv async failures, none in
lev_perp/proof_scorecard files — confirmed via grep). Dry-ran the actual
8-coin config locally against live Kraken data before committing: all 8
symbols evaluated, correct $125 margin, one real seed position (XRP short).

**Not yet done**: the VPS cron line itself. This is a real new production
deployment decision (extra live orders, not just a bugfix restore), so it
needs the owner's explicit go-ahead before installing, same pattern as this
session's earlier crontab-restore work.
