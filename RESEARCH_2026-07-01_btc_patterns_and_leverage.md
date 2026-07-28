# RESEARCH 2026-07-01 — BTC intraday patterns, leverage math, and what survived

Owner-requested market research session (Claude on the Mac). Data: 410 full days
of BTC-USD hourly candles from Coinbase Exchange (2025-05-15 → 2026-07-02, a −42%
period), plus daily bars derived from them; ETH/SOL untested — BTC only. All
backtests include costs where stated. Scripts were session-scratch (not kept);
methods are described well enough to re-derive. Conclusions here drove the
`lev-perp-risk-controls` branch (see WORKLOG 2026-07-01).

## 1. Intraday patterns that actually repeat

| Pattern | Frequency | Robustness |
|---|---|---|
| US-open volatility spike: avg hourly range ~doubles 13:00–15:00 UTC (0.48% Asia → 1.00% at 14:00) | every day | structural — session hours + US data releases |
| Asia range (00–08 UTC) breaks during EU/US hours | 95% of days (389/410); **31% break BOTH sides** | structural |
| Session drift: 13–18 UTC hours avg negative, 20–22 UTC avg positive, 23 UTC most negative | statistical only (~54% of evenings green) | weak, likely regime-dependent (bear sample) |
| Weekday: Sunday evening strongest (+0.25%), Wednesday only negative | statistical | weakest sample — curiosity only |

## 2. What was backtested and FAILED (after realistic costs)

- **Asia/morning range breakout** (many variants): ~30% win rate, negative EV.
  The 31% both-sides-break rate is why — first break is fake half the time.
- **Failed-break fade of the Asia range** (9 variants: stop width, mid vs
  opposite-side target, time filters, long-only): ALL ≈ breakeven pre-fee,
  clearly negative at 0.10% round-trip. Every variant decayed H1→H2.
- **Evening drift long (20:00→23:00 UTC daily)**: +0.10%/trade pre-fee, 54% WR —
  the only positive intraday signal, but fees consume it. Not deployable taker.
- **Candle-based market making** (bid/ask around prior close, inventory-capped):
  −43% to −190%. Inventory bleeds in trends; not viable without orderbook data
  and hedging. Do not revisit with candles.
- **Fading the US-open move**: 48% WR, negative.

## 3. What SURVIVED

- **Multi-day time-series momentum**, daily bars, 0.10%/flip cost, while
  buy-and-hold lost 44%:
  - TSMOM 7d L/S: +27%, maxDD −46%, Sharpe ~0.58
  - TSMOM 14d L/S: +25%, maxDD −36%, Sharpe ~0.54
  - MA 10/40 cross L/S: +19%; Donchian 20d: +6%
  - Consistent with this repo's own tsmom arms and published crypto-momentum
    literature. Edge ≈ +0.06%/day — real but thin, and it arrives in streaks
    (split-half: one losing half, one winning half).

## 4. The leverage result (the important one)

Same TSMOM-14 signal, compound equity, taker fees 0.05%/side, funding 0.03%/day
charged always, **liquidation checked against actual intraday wicks**:

| Leverage | Final | Max DD |
|---|---|---|
| 1x fixed | +2% | −32% |
| 2x fixed | −14% | −56% |
| 3x fixed | **−41%** | −73% |
| 5x fixed | −85% | −92% |
| 10x fixed | −99% | −99% |
| 20x fixed | liquidated in ~25 days | — |
| **vol-targeted (2%/day ÷ realized20d, any cap 2–5x)** | **+8–9%** | −38% |

Key mechanics, independent of the specific signal:
- Sharpe is IDENTICAL (≈0.25) from 1x to 5x — leverage added zero risk-adjusted
  return; it only amplified **volatility drag** (grows with the square of moves).
  With BTC at ~2%/day vol, drag at 2x (~0.08%/day) already exceeds a
  0.06%/day edge → **~2x is where profit mathematically flips to loss** for any
  edge of this size.
- Vol-targeting chose ~1.1x average and ignored higher caps — BTC's own vol is
  the binding constraint, not the exchange's max leverage.
- Multiple ≥9% adverse intraday wicks in 410 days: at 10x that is
  near-liquidation each time; no stop can fully save a position sized like that.

## 5. Consequences already applied to lev_perp_paper.py

(branch `lev-perp-risk-controls`) — hard 5% stop (the 2026-06-30 SOL flip-close
ran −14.4% into −44% of margin with nothing capping it; the stop caps that class
of loss at ~−15%), vol-targeted effective leverage (3x env value becomes a
ceiling; vol picks ~1.1–1.2x), correlation-capped margin (BTC/ETH/SOL
same-direction ≈ one bet), and a news-halt entry gate (`data/news_halt.json`).

## 6. External tooling (lives OUTSIDE this repo, on the owner's Mac for now)

`~/strat finder/news_tracker/` — stdlib-only news/Twitter watcher (RSS works
keyless today; CryptoPanic + X API slots wired, keys pending). Keyword-scores
headlines; on CRITICAL it writes this repo's `data/news_halt.json`
(`{"until": epoch, "reason": ...}`) when its `HALT_FILE` env points there.
Deploys to the VPS via scp + cron/systemd when the owner is ready.

## 7. Standing guidance for future agents

- Don't re-test intraday taker strategies on candles — the 2026-07-01 sweep
  covered breakouts, fades, session drift, and open-fades; all died to costs.
- Treat any proposal with fixed leverage ≥2x on a ≲0.1%/day edge as
  negative-EV by construction (volatility drag math above), regardless of
  win rate. Vol-targeted sizing is the default shape.
- The volatility structure (dead Asia, 13:00–15:00 UTC spike, daily Asia-range
  break) is reliable and is best used for execution/risk timing, not direction.
