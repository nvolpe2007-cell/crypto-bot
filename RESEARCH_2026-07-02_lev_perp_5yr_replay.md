# RESEARCH 2026-07-02 — 5-year replay of the lev_perp arm family

Companion to `RESEARCH_2026-07-01_btc_patterns_and_leverage.md`. Method: the
ACTUAL production code of `lev_perp_paper.py` (v1, with the risk controls) and
`lev_perp_v2_paper.py` (v2, ATR-trail exits) replayed over Coinbase daily bars
(BTC/ETH/SOL, 2021-06 → 2026-07 — two bulls, the 2022 crash, the 2023–24 chop),
by seeding `last_bar_t` past warm-up and letting `process_symbol` walk history.
Not a reimplementation; whatever the arms do live, this did. News halt off,
same filters/costs/funding as production. Fixed $333 margin per position
(non-compounding, as the arms actually size).

## Headline (5 years, $1k book)

| Arm | Return | maxDD | Trades | WR | Liqs |
|---|---|---|---|---|---|
| v1 fixed +5% TP / 5% SL, vol-tgt 2%/3x | **−0.5%** | −16% | 323 | 51% | 0 |
| v2 ATR(14)×2 chandelier, vol-tgt 2%/3x | **+22.8%** | −24% | 129 | 38% | 0 |
| v1 aggressive (vol-tgt 3%/5x) | −0.8% | −24% | 323 | 51% | 0 |
| v2 aggressive (vol-tgt 3%/5x) | +34.3% | −33% | 129 | 38% | 0 |

PnL by year ($):

| | 2021 | 2022 | 2023 | 2024 | 2025 | 2026H1 |
|---|---|---|---|---|---|---|
| v1 base | −25 | +16 | −126 | −62 | +66 | +126 |
| v2 base | +24 | **+205** | −159 | −156 | +121 | +192 |

## Findings (in strength order)

1. **The fixed take-profit design is a full-cycle wash.** v1 = −0.5% over 5
   years at BOTH risk levels. Its +29.6% in a 21-month (2024-07→2026-07) replay
   of the same code was window selection, and its live forward +23% at n=9 is
   uninformative. Mechanism: 323 trades at 51% WR with wins capped at +5%
   price move — every trend's tail is donated, and the choppy years eat the
   rest. **Do not re-propose fixed-TP exits on this arm family without new
   evidence; expect the forward scorecard to bury v1.**
2. **Trailed exits are the real difference, not a tweak.** Same entries, v2
   +22.8%: a third of the trades, 38% WR, avg win ≈ 2.3–2.8× avg loss. Its
   best year was the 2022 CRASH (+$205, shorts ridden down where v1 kept
   cashing out at +5%); its cost is 2023–24 chop (−$315 over two years).
   Classic trend profile — streaky, two consecutive losing years, ~4%/yr on
   the book at base risk. Real, not rich.
3. **Leverage amplifies, never creates.** v1 at 5x: still zero, deeper DD.
   v2 at 5x: +34% at −33% DD. Combined with the 2026-07-01 findings (fixed
   ≥3x compounding = wipeout on the bear year; FIXED 10x on this replay =
   18 liquidations, v2 book touched −97%), the leverage question is closed:
   vol-targeted sizing, and the cap is a drawdown-tolerance dial, not a
   return dial.
4. **Window-selection is the dominant backtest error we keep catching.**
   21 months said v1 +29.6%; 60 months said v1 −0.5%. Any future arm
   evaluation should replay the longest window the data source allows
   (Coinbase daily goes back ≥5y; Kraken's OHLC API truncates at 720 bars).

## Standing consequences

- The forward 2×2 (v1/v2 × base/aggressive, `deploy/lev_perp_arms_cron.txt`)
  runs as pre-specified; this doc is its prior, the scorecard is the judge.
- Sharpened hypothesis, written BEFORE the forward data: v1 fails its proof
  bar at both risk levels; v2 survives; base-vs-aggressive resolves as a
  drawdown-preference tie on risk-adjusted terms.
- Caveats: daily bars can't resolve intrabar TP-vs-stop ordering (scored
  conservatively, same as production); one asset class, one venue's prices;
  funding modeled as constant drag; SOL history starts 2021-06.
