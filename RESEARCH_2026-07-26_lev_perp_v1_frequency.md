# RESEARCH 2026-07-26 — lev_perp v1: why it looks decent short-term, and getting more trades at the same win rate

Owner question, following the live proof-scorecard check that found lev_perp
v1 (fixed take-profit, 3x) as the only arm showing a positive live number
(+$124, 73% win, n=11) but not proven (t=0.57, n<30). Two parts: (1) why does
it look decent right now, (2) can more trades be taken without giving up win
rate. Script: `scripts/lev_perp_v1_universe_research.py` (kept for reuse).

## Part 1 — why it looks decent short-term

Pulled the actual closed-trade record (`data/lev_perp_state.json` on the VPS,
11 trades since `started_at` 2026-06-15):

- All 11 trades are **SHORT**. Entries span 2026-06-14 -> 2026-06-25, exits
  2026-06-18 -> 2026-07-04. This is one clean, monotonic BTC/ETH/SOL downtrend:
  8 straight `take_profit` hits (every one capped at exactly +5% price move),
  then the trend reversed and produced 3 losses.
- The first loss (SOL, entered 06-25, exited 07-01, `reason: flip`,
  **-14.44%** price move -> -44.25% of margin) is not a new bug — it is the
  *exact* historical incident already documented in `lev_perp_paper.py`'s own
  docstring ("the SOL flip-close of 2026-06-30 ran a -14.4% move... because
  nothing capped it") that motivated adding the hard stop-loss on 2026-07-01.
  The two losses after it (ETH, BTC) are both `stop_loss` at exactly -5.0% —
  the fix working correctly.
- **Zero new entries since 2026-07-04** (three weeks idle as of this writing)
  confirms the live constraint for part 2: SMA-50 trend changes across only 3
  symbols are rare events.

**Conclusion: this is the well-known streaky trend-following pattern already
on record (`RESEARCH_2026-07-02`), not new edge.** One favorable window
produced a high win rate by chance of timing (catching a full clean trend
start-to-finish); the 5-year replay already found this same v1 config nets
essentially breakeven (-0.5%) across a full cycle. 73% WR at n=11 is not
informative about the model's real long-run rate.

## Part 2 — more trades, same win rate: tested and confirmed

Reused `lev_perp_paper.py` **completely unmodified** (`process_symbol`,
`_check_exit`, `_entry_filter`, `_open`, `_close` — the exact SMA-50 direction
signal, RSI/trend-age/volume/ADX quality filters, fixed TP/SL, vol-targeted
leverage, and correlation cap), only rescaling `MARGIN` for a fair n-symbol
split. Ran it over the SAME ~5-year Coinbase daily cache from the 2026-07-26
lev_perp entry/universe search (PR #85), first on the production 3-coin
universe (BTC/ETH/SOL) as a validation check, then on the already-vetted
8-coin liquid universe (+ADA, XRP, DOT, AVAX, LINK) that PR tested for
*different* entry signals (MA10/40-cross, confluence) but never for v1's own
actual SMA-50 mechanism.

| Universe | Trades | Win rate | Expectancy/trade | t_clustered | DSR |
|---|---|---|---|---|---|
| 3-coin (production, as-is) | 324 | 50.6% | +$0.058 | 0.10 | 0.538 |
| **8-coin (widened)** | **856** | **52.8%** | **+$0.161** | **1.21** | **0.887** |

The 3-coin row reproduces `RESEARCH_2026-07-02`'s already-published 5-year
result almost exactly (324 vs. 323 trades, 50.6% vs. 51% WR) — confirms this
harness faithfully replicates production, not a reimplementation.

**Widening the universe 3 -> 8 coins, with zero other changes, multiplies
trade count 2.6x while HOLDING win rate (52.8% vs 50.6%, if anything
slightly better) and nearly TRIPLING expectancy per trade.** DSR rises from
0.54 to 0.887 — the closest anything in this repo's research has come to the
0.95 bar with a single, non-invasive change. Per-symbol, the result is
broad-based, not one lucky coin: 5 of 8 symbols net positive (DOT +$83, ETH
+$48, AVAX +$47, ADA +$19, LINK +$15), 3 negative (XRP -$33, BTC -$30, SOL
-$11), win rates 46-60% across all eight — no symbol is a catastrophic drag,
though DOT alone accounts for ~60% of the total net (worth knowing, not
disqualifying).

**Still does not clear the bar** (t_clustered 1.21 vs required 2.00; DSR 0.887
vs required 0.95) — this is evidence FOR the universe-widening lever, not
proof it's ready to fund. Judged here as k=1 (one new candidate vs. the
existing production config, no sweep).

## Answer to "how do we get more trades at the same win rate"

**Widen v1's own traded universe from 3 to 8 liquid majors — this is now
validated with real numbers, not just inferred from the other entry-signal's
result in PR #85.** It is the correct lever specifically *because* the
existing entry filters (RSI<45, trend-age>=8d, volume>1.2x, ADX-dead-zone
skip — each independently pattern-tested and documented in
`lev_perp_paper.py`'s own docstring) are quality gates that should NOT be
loosened to force more trades; more independent symbols under the SAME gates
is the frequency lever that doesn't cost quality. What does NOT work: don't
expect the live 73% win rate to persist — the real long-run rate this
mechanism produces is ~51-53%, with expectancy coming from the 8-coin
diversification effect, not from any single trade being unusually reliable.

## Do not re-propose without new evidence

- Treating the current live 73%/n=11 window as informative about v1's real
  edge — the 5-year full-history number (this doc and `RESEARCH_2026-07-02`)
  is ~51% WR, breakeven-to-modestly-positive.
- Loosening the RSI/trend-age/volume/ADX entry filters to generate more
  trades — they are validated quality gates, not overhead; the win-rate-safe
  lever is universe breadth, already tested here.

## Standing follow-on (not chased this session)

DSR 0.887 at 8 coins is close enough to the 0.95 bar that it's worth
checking, next time this comes up, whether combining this session's universe
widening WITH PR #85's better entry signal (MA10/40-cross, which already beat
raw SMA-50 on Sharpe in the 3-coin round) clears the bar outright — that
specific combination (MA10/40-cross entry x 8-coin universe x v1's own
fixed-TP exit, as opposed to v2's chandelier) has not been tested yet.
