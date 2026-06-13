# Edge Search — 2026-06-13 (systematic, honest)

Session goal: find any improvement that lets the bot **trade and win**, not just
sit idle. Method: measure first, test every executable lever, judge by the proof
bar, refuse to overfit. All tools below are committed and re-runnable.

## The hard constraints (these force the conclusion)
1. **US Kraken-spot account** → long-only majors. No shorting, no Binance/Bybit,
   no cheap perps. Rules out market-neutral, pairs, cross-sectional shorts.
2. **~0.5% round-trip cost** → only worth trading where the move ≫ 0.5%.
3. **Current regime** = extreme fear / ranging (F&G 12), no sustained trend.

## What was tested, and the verdict

| Lever | Tool | Verdict |
|---|---|---|
| Directional intraday (1m) | `trade_forensics.py`, `lessons.py` | **DEAD** — net −$20 = −$5 move + −$15 fees; flip still loses −$9.49; even at ZERO cost the gross is negative; cost = 18.6× a median move; 98% of losses cost-dominated. No filter/threshold flips it. |
| Funding arb (cheaper venue) | `funding_cost_sweep.py` | **DEAD this regime** — majors funding carry NEGATIVE even at zero cost; median 14 sign-flips/symbol (untimeable). Lower (Bybit) cost can't rescue what isn't there. |
| Trend-following (tsmom) | `trend_research.py` | **FRAGILE candidate** — 7/12 (coin,lookback) cells beat buy-&-hold; works BTC/ETH, fails SOL. Live SMA200 is the *worst* lookback (N=50/100 better). Needs a bull; correctly in cash now. |
| Vol-targeting overlay | `vol_target_research.py` | Portfolio-level: **robust** (3/4 lookbacks Sharpe↑, all 4 maxDD↓ ~7-10pp). Per-symbol (the *implementable* version): **fails** (1/4). So not shipped. |
| Mean-reversion / swing 4h | (memory `swing_one_year_backtest_negative`) | **DEAD** — 1-yr backtest −$0.47/trade, t=−0.98. |
| Vol-regime timing | `volregime_research.py` | **NO edge** — 1/6 cells beat B&H; the *inverse* scores better (signal = noise). |

## The move/cost wall, quantified (`timeframe_edge.py`, live data)
median move ÷ ~0.5% cost: **1m 0.1× (unwinnable), 15m 0.6×, 1h 1.9×, 4h 2.8×,
1d 8.2× (winnable)**. You cannot beat a move smaller than the toll at ANY win
rate — win rate is a vanity metric; **expectancy** wins, and only where move ≫ cost.

## Conclusion (forced by the constraints, not defeatism)
The only non-dead executable approach is **trend-following on majors (long in
uptrends, cash otherwise)**. It is correctly idle in cash in this bear/chop —
that IS the strategy working (capital preservation). It will trade, and can win,
when a sustained uptrend appears; the proof bar (forward, n≥30, family-wise t)
judges it then. Nothing else in the executable space showed a robust edge.

**What would change the picture:** (a) a sustained bull → trend trades & the
forward test accumulates; (b) account access to shorts / a cheap-fee venue →
re-opens market-neutral & funding arb (re-run `funding_cost_sweep.py`); (c) a
funding regime flip to persistent positive majors funding.

**Do NOT:** loosen the cost gates to force trades (re-buys the proven bleed),
reactively tune filters per-loss (overfit), or cherry-pick a backtest lookback
(killed the swing edge once already). Improvements must be theory-first + robust
across params + judged forward.

## Infrastructure shipped this session (so any future edge is caught honestly)
- `src/attribution.py` — cross-arm SQLite P&L ledger + daily Telegram scorecard.
- `src/kill_switch.py` + per-arm funding loss caps — protection.
- 6 re-runnable research tools in `scripts/` (the table above).
