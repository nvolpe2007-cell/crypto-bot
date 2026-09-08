---
date: 2026-09-06
agent: claude-computer
branch: feat/trend-signal-honest-indicator
pr: 113
lane: directional
files: [pine/trend_signal_honest.pine, RESEARCH_2026-09-06_volatility_signal_search.md]
---

# Volatility signal search — three designs tested, two killed, one premise falsified

Owner asked for a simpler indicator built on the GEX/volatility idea, shipped to
TradingView, that beats the current win rate with buy signals ahead of big moves.
Pine has no options-chain access, so the question became whether the *mechanism*
behind dealer-exposure walls — volatility suppression then release — is visible in
price alone. It is not, in any of the three shapes tested.

Killed: (1) fading vol bands, PF 0.42-0.66, shorted a 20x bull market; (2)
compression -> expansion breakout, PF 0.24-0.46 and worse than its own no-filter
control, which prompted testing the premise directly — **after vol compression the
5-day absolute move is SMALLER, not bigger (t=-2.46, p=0.014)**, no effect at
10d/20d, so the "coiled spring" idea is falsified at these horizons and the whole
family dies, not just the implementation; (3) vol regime as an overlay on the trend
rule (the only use the 2026-06-08 verdict allowed) — worse in **12 of 12 cells**
across BTC and ETH.

The 12/12 result is the useful part. Every overlay raised the win rate and destroyed
the profit factor; ETH SMA(100) went from 26.7% win / +441% to 45.8% win / -59%.
That independently reproduces the 2026-09-05 tpMult finding in a different shape —
an asymmetric take-profit there, a regime filter here. Named in the research doc as
a standing failure mode: **any change that raises win rate while increasing trade
count should be assumed to be destroying expectancy until proven otherwise.**

Shipped the plain rule that actually measured profitable — close vs SMA(100), 2%
hysteresis band, nothing else. BTC PF 8.08 / +2841% / maxDD -44.1%; ETH PF 8.21 /
+1576% / -68.3%; same params both assets, both split-halves positive, both beating
buy-and-hold with smaller drawdown. The script header spends as much space on the
limits as the results: neither clears t>2 (1.44 / 1.08), the median trade LOSES on
both assets, and one trade is 59% / 81% of gross profit. A 4% band scored higher and
was deliberately not selected, since choosing it post-hoc is the exact overfit being
argued against.

**Verification:** all numbers reproduced from live-fetched Coinbase daily data; the
shipped rule re-verified *including* the hysteresis band (the first backtest omitted
it, so the header would have described a different rule than the script implements —
caught before shipping). Premise test run independently of any trading rule. Every
finding cross-checked on ETH, not just BTC.

**Cross-lane note:** none — new pine file plus a research doc, no existing strategy
or decision-pipeline file touched. Note `pine/` also carries uncommitted work from
another session's branch (`research/promote-vault-hypotheses`); this PR adds only
its own file, so the two merge cleanly.

**Not done:** Pine syntax not compiler-verified (can't compile Pine locally) — needs
a paste into TradingView to confirm. No walk-forward or OOS split on the shipped
rule; n=19-23 is small and the window contains one dominant bull market.
