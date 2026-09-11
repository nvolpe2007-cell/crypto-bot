---
date: 2026-09-10
agent: claude-computer
branch: guard/altperp-volume-provenance
pr: pending
lane: directional (src/altperp/ + config.yaml — COMMENTS ONLY, no behaviour change)
files: [src/altperp/runner.py, src/altperp/confluence.py, src/altperp/research_universe.py, config.yaml]
---

# Volume-provenance audit: one real exposure, and it is dormant

CLAUDE.md gained a rule this session (from the SSRN scan): **never compute a signal from
aggregated multi-venue volume**, because unregulated exchanges wash-trade >70% of reported
volume (SSRN 3530220; Bitwise says up to 95%). That rule said "audit any volume-weighted
path." This is the audit, and its result, so nobody has to run it again.

## Method

Mapped every non-Kraken data source by grepping for exchange API hosts, then checked which
of those feed a *volume* statistic into a *decision*.

## Clean — the live paths

- **The live directional loop** (`paper_trading.py`) has a whale-print filter — current
  candle volume > 10× SMA20 — and its OHLCV comes from `ExchangeConnection` →
  **`ccxt.kraken()`**. Kraken is in the REGULATED tier of the wash-trading taxonomy. Fine.
- **Every forward-test runner** (`tsmom_paper`, `conf_paper`, `swing_paper`, `lev_perp_paper`,
  `pairs_paper`, `kelly_trend_paper`, `trend_ensemble_paper`, `tsmom_ls_paper`, `regime_arm`,
  `btc_trend_paper`, `brain_paper`) pulls `api.kraken.com`. Fine.
- **`arbitrage/funding_scanner.py`** does read Binance and Bybit — but only **funding rates**.
  Grep for volume/turnover in that file returns nothing, and the arms restrict themselves by a
  symbol whitelist (`MAJOR_SYMBOLS` / `FUNDING_ARB_KRAKEN_SYMBOLS`), not by reported turnover.
  Fine.

## The exposure — `src/altperp/`, and it is worse than "noisy"

`src/altperp/data.py` pulls Bybit `/v5/market/kline`. That volume flows to
`runner.py` → `is_volume_spike(last, prev_20, 3.0)` → `confluence.py:78`, where it is a
**required conjunct** of the Tier-1 flush-long gate:

```python
tier1_long = oi.get("long_flush") and funding.get("funding_collapsed") and volume_spike
```

Bybit is in the **unregulated** tier. Two details make this more than a data-quality nit:

1. A 3× burst over a 20-bar average is *precisely* the statistic wash trading distorts —
   trade-size clustering and burst patterns are what the detection literature keys on.
2. Wash volume **rises under volatile conditions** (SSRN 4971590). A "post-liquidation flush"
   is a volatile condition. So the contamination is **correlated with the gate firing**, not
   independent of it — the bias points toward false positives in exactly the regime the gate
   exists to detect, rather than adding symmetric noise.

This is methodology lesson 3 from the [[altcoin-pairs-cointegration]] retraction in another
costume: *robustness checks cannot detect a flaw uniformly present in the input.* No amount of
walk-forward on altperp would have surfaced it.

**It is dormant.** `config.yaml altperp.enabled: false`, shelved 2026-06-05 for a different
and independently sufficient reason (unproven directional strategy, shorts Kraken Futures
perps which a US Kraken-spot account cannot execute, and `proof_scorecard.py` never tracked
it). So this is a **latent trap for whoever re-enables it**, not a live bleed. Recorded that
way — no claim that it has cost anything.

## Also flagged — `research_universe.py`

Ranks the universe by Bybit `turnover24h` and takes the top N. That is literally the
"top-N by volume" pattern the new rule names: it selects partly on who fakes the most. It is a
research tool, not a live path, but any universe promoted out of it inherits the flaw.

## What changed

**Comments only. Zero behaviour change**, and the package is disabled anyway. Warnings placed
where someone would actually hit them: at the computation site (`runner.py`), at the decision
site (`confluence.py`), at the universe-selection site (`research_universe.py`), and as a
second documented reason not to flip the switch (`config.yaml`). Each names the fix —
re-source from a regulated venue, or drop `vol_spike` from the tier-1 conjunction.

Tests still collect (3584).
