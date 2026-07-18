# Trend-ensemble live deployment checklist (drafted 2026-07-17, Mac-local)

Goal: take the proven long-only BTC trend strategy from paper to real Kraken
spot money, sized so the drawdown is survivable. **Do not skip step 0.**

## Step 0 — RECONCILE THE BACKTEST DISCREPANCY (blocking)

The queued claim (NEXT_SESSION: 2-of-3 ensemble +141%, maxDD −27%, B&H +33%
"same window") did NOT reproduce on 2026-07-17. On 7 years of OKX daily bars
(2019-06→2026-07, so indicators fully warm), the same 2-of-3 rules
(close>SMA100, close>SMA200, 90d mom>0, next-day execution, Kraken costs)
over the last 5 years:

| | Return | maxDD | Flips |
|---|---|---|---|
| Claimed (prior session) | +141% | −27% | ~44 |
| Reproduced, taker 0.26% | +73% | −55% | 56 |
| Reproduced, maker 0.16% | +83% | −55% | 56 |
| BTC buy & hold, same window | +103% | ~−65% | — |

Yearly (reproduced): 2021H2 −24%, 2022 −27%, 2023 +73%, 2024 +81%,
2025 +14%, 2026H1 −12%. Still crash-protective vs B&H's −65%, but the
"beats B&H and S&P with −27% DD" claim is not confirmed here. The prior
test's B&H baseline (+33%) implies a different window/data source entirely.
**Find the original test script/window (likely PC or VPS), rerun both on the
same data, and only deploy if the reconciled number still justifies it.**
Note the live paper arm `btc_trend_paper.py` uses a DIFFERENT pre-specified
signal (SMA100 AND 20d momo — the tournament winner); decide which one
actually goes live. Deploying the paper-arm signal keeps the forward record
honest; deploying the ensemble restarts the clock.

## Step 1 — Owner decisions (nobody else can make these)

- [ ] Capital: an amount where a −50% drawdown on the BTC sleeve is
      tolerable *for years*. (At −27% claimed vs −55% reproduced, plan for
      −55%.)
- [ ] Which signal (step 0): paper-arm conf_trend_momo vs 2-of-3 ensemble.
- [ ] Merge the pending PRs first (lev-perp-v2-trailed-exits + ~10 older) so
      the deployed tree is the tested tree.

## Step 2 — Kraken account plumbing

- [ ] Kraken API key: **trade-only** permissions (no withdrawal), IP-restrict
      to the VPS IP (178.105.41.226).
- [ ] Keys into VPS `/opt/crypto-bot/.env` (never the repo):
      `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`.
- [ ] Fund the account; leave a fee buffer in USD.

## Step 3 — Execution wrapper (small code task)

- [ ] New `btc_trend_live.py`: same signal path as the chosen paper arm, but
      the order step posts a real Kraken order. **Maker (post-only limit)**
      — 0.16% vs 0.26%, and this strategy is never in a hurry (signal is
      daily; a fill an hour late is noise).
- [ ] Hard safety rails: order size cap = configured sleeve, refuse to open
      if a position already exists (idempotent), master kill switch honored
      (`data/KILL_SWITCH`), Telegram alert on every fill/error.
- [ ] Dry-run mode first: log the exact order it WOULD place for ≥1 signal
      flip before arming.

## Step 4 — Ops

- [ ] Cron/systemd timer after daily close (00:05 UTC), same pattern as the
      paper arms.
- [ ] Weekly Telegram report includes the live sleeve (equity, position,
      last signal) — extend `scripts/weekly_report.py`.
- [ ] Pre-registered abort rule, written BEFORE going live: e.g. "halt and
      review if live slippage+fees per round-trip exceed 0.4%, or if
      drawdown exceeds the backtest maxDD by 10pp."

## Step 5 — Scale rule

Start at 25–50% of intended size for the first 2–3 signal flips (weeks to
months) to verify fills/costs match the model; scale to full only after the
measured cost per round-trip is at or under the modeled 0.16%.

## Combined-book context (2026-07-17 backtest, reproduced numbers)

$1k split 60% ensemble / 30% v2 perps / 10% funding-majors, yearly, net:
2021H2 −$97, 2022 −$102, 2023 +$393, 2024 +$442, 2025 +$120, 2026H1 −$12
→ **+$744 over 5y (~+12%/yr non-compounded)**, with the two down years
capped near −10% of book. The diversification works (v2's best year 2022 was
the ensemble's worst) but the headline rate depends on step 0's resolution —
with the claimed ensemble numbers it would be ~+20%/yr.
