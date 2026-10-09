---
date: 2026-09-06
agent: claude-computer
branch: research/trend-signal-risk-reward
pr: pending
lane: directional
files: [scripts/trend_signal_rr_research.py, RESEARCH_2026-09-06_trend_signal_risk_reward.md]
---

# Trend Signal R:R — already 8:1, and a fixed 3R–5R target makes it worse

Owner asked whether the shipped BUY signals have "a good risk to reward — looking for
R 1:3 to 1:5". The rule has no stop and no target, so R had to be imputed first:
entry close minus the lower band as it stood at entry, i.e. the risk actually taken.
Measured on BTC/ETH/SOL (Coinbase) plus SPY/QQQ/GLD (yfinance), 2019→now, real costs.

**Answer: the requested ratio is already exceeded.** Pooled over 100 trades, average
win +11.46R against average loss −1.42R — a payoff ratio of **8.07**. On the three
equities, which had no influence on the rule's design, it lands at 3.2–6.8 payoff on a
60–64% win rate, which is close to exactly the shape asked for.

**Capping at 3R or 5R makes the ratio WORSE, not better.** Payoff falls 8.07 → 2.13 (3R)
/ 2.77 (5R); per-trade expectancy falls +3.35R → +0.59R / +0.84R; win rate rises 37% →
48%. The mechanism is visible in the distribution: **88% of all gross R comes from the 14
trades of 100 that ran past 5R.** A 5R cap sells all fourteen of them at 5R. This is the
**third independent reproduction of the win-rate trap** (tpMult 2026-09-05, the 12/12 vol
overlay 2026-09-06, this) — see memory `win_rate_trap`.

**The result I did not want and did not hide:** the `growth` column (compounded net
return) does NOT fall with the target — 778x / 4081x / 2418x / 2965x / 4467x / 2469x
across 1R..8R. Non-monotonic in the parameter is the signature of noise, and the metric
is bad here anyway (compounds full capital over six assets' overlapping trades, and the
target variants book ~1.5x more trades so they get more compounding events on the same
moves). Per-trade expectancy in R is the metric that answers the question, and it falls
without exception. Stated in the doc rather than dropped, because a reader who reruns the
script will see that column and should know why it is not evidence.

**The finding that actually matters, and it is not about reward:** the imputed R is not a
real risk cap. **45 of 63 losing trades lost MORE than 1R; worst −5.61R.** The band moves
with the SMA and price gaps through it. So any "1:3 R:R" claim about the rule as written
is arithmetic on a denominator that is not enforced. A hard 1R stop measured roughly
NEUTRAL (PF 4.79 vs 4.74 control, but avg R down 3.35 → 2.70 on 23 more trades) — not
harmful the way the target is, and the only variant here worth a forward test rather than
a discard.

**Verification:** take-profit and stop variants fill INTRABAR on touch with no gap-through
and no slippage — deliberately optimistic, so the bias runs in favour of the idea being
tested and the negative result is the strong direction. Stop resolves before target when a
bar touches both (conservative). Every finding cross-checked on all six assets, not just
BTC. Data cached to `data/research_cache_trend_rr.json`.

**Cross-lane note:** none — new script plus a research doc, no existing file touched.

**Limits:** n=100 pooled across six correlated assets in a predominantly bullish window;
BTC and ETH are in-sample for the rule's own design. Descriptive, not proof. The 1R-stop
variant is NOT deployed or wired anywhere — it is a measurement, and putting it live would
change the rule the owner is about to trade real money on mid-flight.
