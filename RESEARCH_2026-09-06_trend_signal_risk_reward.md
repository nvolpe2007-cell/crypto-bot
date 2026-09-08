# Risk:reward of the Trend Signal (Honest) rule — and what a fixed 3R–5R target does to it

**Date:** 2026-09-06 · **Tool:** `scripts/trend_signal_rr_research.py` ·
**Data:** Coinbase daily BTC/ETH/SOL + yfinance SPY/QQQ/GLD, 2019-01-01 → 2026-09-06.
**Costs:** 0.54% round-trip crypto, 0.10% equities, charged on every entry and exit.

## Question

The owner asked whether the shipped BUY signals have "a good risk to reward — I'm
looking for R 1:3 to 1:5."

## The definition problem, and how R was imputed

The rule is long/flat on close vs SMA(100) with a 2% band. It has **no stop and no
target**, so R is not defined by construction. The imputed definition used here:

    R = entry_close − SMA100_at_entry × (1 − 0.02)

i.e. the distance down to where the SELL would have fired had price turned around
immediately — the risk actually being taken at entry. **This is an imputed R, not one
the rule enforces**, and §4 below measures how badly that matters.

## 1. The payoff ratio the rule already delivers

| asset | n | win% | avg R | median R | avg win R | avg loss R | payoff | PF |
|---|---|---|---|---|---|---|---|---|
| BTC | 18 | 44% | +4.29 | −0.51 | +11.44 | −1.43 | 8.01 | 6.41 |
| ETH | 22 | 32% | +7.69 | −1.04 | +28.24 | −1.90 | 14.88 | 6.94 |
| SOL | 31 | 13% | +0.54 | −1.27 | +13.06 | −1.31 | 9.95 | 1.47 |
| SPY | 10 | 60% | +1.74 | +1.33 | +3.67 | −1.16 | 3.16 | 4.74 |
| QQQ | 11 | 64% | +2.06 | +1.43 | +3.77 | −0.92 | 4.08 | 7.14 |
| GLD | 8 | 62% | +3.91 | +0.97 | +6.86 | −1.01 | 6.79 | 11.32 |
| **POOL** | **100** | **37%** | **+3.35** | **−0.88** | **+11.46** | **−1.42** | **8.07** | **4.74** |

The requested 1:3–1:5 is **already exceeded, by a lot**: average win 11.46R against an
average loss of 1.42R is a payoff ratio of **8:1**. On the three equities — which had
zero influence on the rule's design — it lands at 3.2–6.8 payoff with a 60–64% win rate,
which is close to exactly the shape the owner asked for.

Where that comes from:

- trades reaching ≥3R: **23/100**; ≥5R: **14/100**; ≥10R: **10/100**
- **88% of all gross R is produced by the 14 trades that ran past 5R**

## 2. Fixed take-profit at kR — the direct test

Exits intrabar the moment the high touches entry + k·R. **Optimistic on purpose** (no
gap-through, no slippage on the target), so the bias runs in favour of the thing tested.

| variant | n | win% | avg R | payoff | PF | growth |
|---|---|---|---|---|---|---|
| rule (control) | 100 | 37% | **+3.35** | **8.07** | **4.74** | 3408x |
| + take-profit 1R | 261 | 64% | +0.22 | 0.89 | 1.59 | 778x |
| + take-profit 2R | 189 | 54% | +0.45 | 1.61 | 1.89 | 4081x |
| + take-profit 3R | 163 | 48% | +0.59 | 2.13 | 2.01 | 2418x |
| + take-profit 4R | 146 | 43% | +0.68 | 2.66 | 2.02 | 2965x |
| + take-profit 5R | 137 | 45% | +0.84 | 2.77 | 2.22 | 4467x |
| + take-profit 8R | 122 | 41% | +1.11 | 3.59 | 2.49 | 2469x |

Per-trade expectancy collapses from **+3.35R to +0.59R (3R target) / +0.84R (5R target)**,
and the payoff ratio the owner was trying to *achieve* falls from 8.07 to 2.13 / 2.77 —
capping at 3R **produces a worse realised R:R than not capping at all**, because the
average loss is unchanged while the average win is truncated. Win rate rises 37% → 48%.

**Third independent reproduction of the win-rate trap** (after tpMult 2026-09-05 and the
12/12 vol-regime overlay 2026-09-06): win rate up, trade count up (100 → 163), expectancy
destroyed.

### The one column that does not fall, and why it is not evidence

`growth` (compounded net return over the pooled trade sequence) does not decline
monotonically: 778x → 4081x → 2418x → 2965x → 4467x → 2469x. **Non-monotonic in the
parameter is the signature of noise, not effect.** It is also a poor metric here — it
compounds full capital across six assets' overlapping trades, and the target variants
book ~1.5x more trades, so they get more compounding events on the same underlying moves.
Per-trade expectancy in R is the metric that answers the question asked; it falls without
exception.

## 3. Adding the stop that normally accompanies a target

| variant | n | win% | avg R | payoff | PF | growth |
|---|---|---|---|---|---|---|
| rule (control) | 100 | 37% | +3.35 | 8.07 | 4.74 | 3408x |
| + stop 1R only | 123 | 30% | +2.70 | 11.14 | 4.79 | 4156x |
| + TP 3R & stop 1R | 183 | 43% | +0.50 | 2.57 | 1.95 | 2487x |
| + TP 5R & stop 1R | 161 | 37% | +0.71 | 3.67 | 2.18 | 5524x |

**A stop alone is roughly neutral** — PF 4.79 vs 4.74, payoff 11.14 vs 8.07, but avg R
*down* 3.35 → 2.70 on 23 more trades. Different, not better; the differences are well
inside what 100 correlated trades can produce by chance. It is not killed the way the
target is, and it is the only one of these ideas worth a forward test.

## 4. The imputed R is NOT a real risk cap — the finding that actually matters

Of 63 losing trades:

- **45 of 63 lost MORE than 1R.** Median loss **−1.32R**, worst **−5.61R**.
- 18 of 63 lost less than 1R (the band rose to meet price before the exit fired).

The band moves with the SMA and price gaps through it, so **the rule does not cap risk at
1R, or at anything**. Any "1:3 R:R" claim about it as currently written is arithmetic on a
denominator that isn't enforced. If a bounded R is genuinely wanted, that comes from
adding a stop (§3), not from adding a target.

## Verdict

1. The rule already delivers **8:1** average win/loss — better than the 1:3–1:5 asked for.
2. Capping gains at 3R or 5R **lowers** the realised payoff ratio to 2.1–2.8 and cuts
   per-trade expectancy by ~4-6x. Do not add a take-profit.
3. The genuine gap is the downside: losses are not capped at 1R and reached −5.6R. A hard
   1R stop measured approximately neutral, not harmful — the one variant here that could
   be forward-tested rather than discarded.
4. n=100 pooled across six correlated assets over one predominantly bullish window; BTC
   and ETH are in-sample for the rule's design. Treat every number above as descriptive,
   not as proof.
