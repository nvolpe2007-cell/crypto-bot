---
date: 2026-09-08
agent: claude-computer
branch: research/perfect-indicator-search
pr: 116
lane: directional
files: [pine/trend_signal_stocks.pine, tests/test_equity_indicator_lab.py]
---

# The panel I wrote was overclaiming, and I only found it by watching it run

Loaded "Trend Signal (Stocks)" on a live 10-minute SPY chart to confirm it computes, and
read the panel it draws:

```
max DD (rule)        -12.5%      return (rule)        1.02x
max DD (buy & hold)   -9.7%      return (buy & hold)  1.21x
what this is for   ->  "less pain, less money"
```

The rule was worse on **both** axes, and my panel still claimed less pain. The verdict cell
had only two branches — beat-B&H-on-return, else assert "less pain" — so the case where it
loses on return AND drawdown was unrepresentable. **A panel that cannot report the bad case
is advertising, not measurement**, and I wrote it.

**Fixed:** four quadrants instead of two (`better on both (rare)` / `more money, more pain`
/ `less pain, less money` / `WORSE ON BOTH here`), coloured red when drawdown is worse.

**Also added while there:** a `timeframe` row. Every number in that script's header is
DAILY-bar evidence; on a 10-minute chart it is an untested rule wearing a tested rule's
name. The panel now says `NOT DAILY - untested` in red. That is the row that would most
often stop someone trusting a number they shouldn't.

Table grew 6 rows -> 7. An off-by-one there silently drops the last row, which is exactly
the timeframe warning, so there is a test asserting the declared row count covers the
highest index actually written.

**Verification:** 3 new tests (18 total in the file, all pass). Pushed to TradingView and
**confirmed rendering live** — the panel now reads `WORSE ON BOTH here` and
`NOT DAILY - untested`, both red, on the same SPY chart that exposed the bug. Saved as
script version 3.

**Cross-lane note:** none — the pine file and test file this branch owns.

**The generalisable bit:** the local tests all passed before this fix and would have kept
passing. It took looking at the thing running on a real chart to see it. Compilation and
unit tests verify the code does what it says; they cannot tell you the thing it says is a
claim you have not earned.
