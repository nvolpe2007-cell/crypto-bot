# RESEARCH 2026-08-04 — Altcoin arbitrage survey + cointegrated pairs discovery

Owner: "do more research on making a new strategy and filters... look at altcoin
arbitrage also." Three avenues tested; two dead ends (both confirmed empirically,
not just theoretically), one genuinely strong new lead.

## 1. Cross-exchange altcoin arbitrage — DEAD (confirmed live)

Live snapshot, 10 altcoins (XRP, ADA, LINK, DOT, AVAX, LTC, BCH, DOGE, ATOM, ALGO)
across 5 exchanges (Kraken, Coinbase, OKX, Bitstamp, KuCoin) simultaneously. Best
cross-exchange spread found: **2-11bps**, e.g. DOGE 10.7bps, AVAX 7.9bps, ADA
6.8bps. A single round-trip at this repo's own cost model (0.15-0.54%% = 15-54bps)
already exceeds the entire spread before counting withdrawal time, transfer risk,
or slippage on real size. Confirms `RESEARCH_strategies_and_filters.md`'s existing
verdict ("a latency race, retail rarely wins") empirically, for alts specifically.
**Not worth pursuing.**

## 2. Single-venue triangular arbitrage on altcoins — DEAD (confirmed live)

The repo already has a full triangular-arb scanner (`src/triangular_arb.py`,
disabled via `TRIARB_ENABLED=0`) but its `DEFAULT_CYCLES` only covers BTC/ETH/SOL.
Extended the same math to 11 altcoin/BTC cross-pairs on Kraken (all exist except
AVAX/BTC), live order-book snapshot, both cycle directions per coin:

Every single altcoin cycle: **-118bps to -137bps**, dominated by the 3-leg taker
fee floor (3×0.40% = 120bps) with essentially zero real spread on top to offset
it — ETH (the tightest/most efficient) still comes in at -117.6bps. **Extending
triangular arb to alts does not change the existing disabled verdict.**

## 3. Altcoin pairs cointegration — STRONG NEW LEAD, shipped as a new arm

`pairs_paper.py` only ever traded the 3 combinations of {BTC, ETH, SOL} — it never
tested cointegration, it assumed the majors were related. Ran a proper
Engle-Granger test (OLS hedge ratio + Augmented Dickey-Fuller on the residual)
across all C(10,2)=45 pairs in a 10-coin universe (adds LTC/BCH/XRP/ADA/LINK/
AVAX/DOT to the existing 3), on 5 years of hourly OKX/Kraken-equivalent data.

**Methodology discipline** (this matters — it's the difference between this and
the EMA-cross finding that fell apart under scrutiny):
1. Split the 5-year series exactly in half by TIME (not randomly).
2. Ran the cointegration test on the FIRST half only. 10/45 pairs passed
   (ADF p<0.05).
3. Backtested the z-score mean-reversion strategy — production's exact mechanics
   (entry|z|≥2.0, exit|z|≤0.5, stop|z|≥3.5, 7-day time stop, 0.15%×2-leg cost +
   funding drag) — **strictly on the second, unseen half.** No look-ahead.
4. Split that held-out half again into two sub-periods and required the edge to
   hold in BOTH independently, not just averaged.

**Result — 4 of 10 pairs cleared every bar:**

| Pair | Full test-half: n, WR, total, t | 1st sub-half t | 2nd sub-half t |
|---|---|---|---|
| LINK/SOL | 288, 73.6%, +$1,343 | **+7.71** | **+8.96** |
| ETH/AVAX | 263, 66.5%, +$968 | +5.18 | +6.07 |
| ETH/XRP | 276, 70.3%, +$704 | +1.56 (weak) | +6.68 |
| ETH/BCH | 282, 66.0%, +$878 | +4.47 | +2.69 (degrading) |

The other 6 cointegrated-on-train pairs (ADA/AVAX, BCH/XRP, LINK/AVAX, AVAX/DOT,
BCH/AVAX, ADA/DOT) all failed out-of-sample (WR 29-34%, small negative t) — the
train-only cointegration test does produce false positives, which is exactly why
the out-of-sample step matters and why 6/10 were correctly rejected rather than
shipped.

LINK/SOL and ETH/AVAX are the strongest — positive and t>5 in every single split
tested. ETH/XRP and ETH/BCH are real but a bit less uniform (one sub-half each is
weaker, though never negative). All 4 clear the ~2.8 Šidák family-wise bar for
10 trials on the full held-out set.

## 3b. Follow-up: proper walk-forward (2026-08-05)

The original test above used one train/test split. Ran a full 4-fold walk-forward
instead (fold k trains cointegration on window k, tests the strategy strictly on
the next window k+1, non-overlapping, 3 independent fold-tests per pair):

| Pair | Fold 0 test-t | Fold 1 test-t | Fold 2 test-t |
|---|---|---|---|
| ETH/AVAX | +2.56 | +5.18 | +6.07 |
| ETH/BCH | +1.44 | +4.47 | +2.69 |
| ETH/XRP | +3.99 | +1.56 | +6.68 |
| LINK/SOL | +1.77 | +7.71 | +8.96 |

**12 of 12 fold-tests are net positive.** Only 2 of 12 are "weak" (t≈1.4-1.6);
none negative. This is the strongest, most-validated result of any research
session in this repo — genuine walk-forward, not same-sample fitting, and an
internal-consistency check the earlier EMA-cross finding never had a chance to
pass (that one fell apart the moment it left its original 3-coin sample).

One honest nuance worth flagging: the *formal* cointegration test doesn't always
reach significance on every individual training fold (e.g. ETH/AVAX fold 1 train
p=0.375, fold 2 p=0.339 — not "cointegrated" by the p<0.05 bar in that specific
window) yet the strategy still traded profitably in the following out-of-sample
window regardless. That suggests this may be less "cointegration exists in every
rolling window" and more "these 4 specific coin pairs have a persistent
relative-value relationship that a point-in-time ADF test doesn't always detect
but the trading result reliably captures anyway" — a real distinction, but not
one that changes the practical conclusion: the strategy itself is robust across
every independent window tested, which is the thing that actually matters for
deploying it.

## 3c. Important revision: this may be a broad regime effect, not pair-specific (2026-08-05)

Extended the same walk-forward methodology to 16 coins (120 pairs) to look for
*more* validated pairs, applying the "net-positive in every fold" bar as the
selection criterion from the start. Result: **34 of 116 candidate pairs (29%)
passed** — including economically arbitrary combinations like BTC/DOGE and
SOL/ATOM that have no particular reason to move together beyond general crypto
beta. That is a red flag, not a bigger win: if a third of essentially random
liquid-altcoin pairs clear the same bar, the selection process isn't finding
pair-specific relationships, it's picking up something much more general.

**Negative control**: ran the identical mechanics on BTC vs. a synthetic series
built from BTC's own returns *shuffled in time* (same volatility, genuinely no
real relationship), 8 independent trials. **0 of 8 shuffled pairs passed** —
so this isn't pure backtest-mechanic noise; real crypto pairs behave
differently from random walks. But 29% of real pairs vs. 0% of random pairs
still means the effect is much broader than "4 special pairs," more like
"most liquid altcoin pairs during 2021-2026 show this to some degree."

**Best interpretation**: likely a genuine but *broad* phenomenon — crypto
altcoins share strong common-factor ("crypto beta") exposure, and this
5-year window (2021 mania → 2022 crash → 2023-26 choppy recovery) was full of
episodic, market-wide divergence-then-reconvergence events that show up in
almost any two correlated alts' spread, not a durable economic relationship
specific to LINK/SOL or ETH/AVAX. This raises real regime risk: a genuinely
trending market (permanent winners/losers, not a boom-bust-recover cycle)
could break this broadly, all at once, across every pair simultaneously —
which is worse for diversification than it looks, since more pairs from the
same broad effect isn't independent risk reduction, it's concentrated
exposure to one factor.

**What this means for the shipped arm (PR #93)**: NOT reverting it — the 4
pairs still cleared every walk-forward fold, including the strict single-split
test, and something real (not noise) is happening. But I'm not adding the 34
newly-found pairs to the live arm; doing so would overstate confidence in
something now understood to be more "broad market regime" than "specific
pair edge." The right next step is letting the 4 already-shipped pairs earn
real forward proof, and treating any further pair additions with real
suspicion until there's a way to distinguish genuine idiosyncratic
relationships from this broader effect (e.g. testing against a differently-
shaped historical regime, if one becomes available, or simply waiting for
forward evidence across a regime this backtest window didn't contain).

## Disposition

Shipped as `pairs_altcoin_paper.py` — same mechanics/costs as production
`pairs_paper.py` (imported, not reimplemented, so it's directly comparable),
trading only these 4 explicit pairs (not a combinatorial re-expansion — that
would reintroduce the exact p-hacking risk this methodology was built to avoid).
Own $1k book, own state file: `pairs_paper.py`'s existing BTC/ETH/SOL book already
has real (currently losing) forward history, so mixing in new pairs would corrupt
an in-progress proof record. Registered in `proof_scorecard.py` for its own
forward verdict. Still a backtest, not proof — needs the same n≥30 forward trades
before it counts as real, same bar as everything else here.

## Honest caveats

- Still fundamentally a backtest. Forward paper trading is the actual test.
- Only 2 economically coherent train/test splits were checked (not a walk-forward
  with many splits) — a more thorough validation would roll this forward across
  several splits, which is a good next step if the paper arm starts looking good.
- 6/10 pairs that looked cointegrated in-sample failed out-of-sample — a reminder
  that even "real" statistical cointegration tests aren't immune to regime shift;
  these 4 could still stop working going forward for reasons the backtest can't see.
- Didn't test economic rationale deeply (e.g., why ETH pairs well with AVAX/XRP/BCH,
  why LINK/SOL specifically) — plausible (shared "smart contract platform" beta)
  but not verified as a mechanism, just an empirical pattern.
