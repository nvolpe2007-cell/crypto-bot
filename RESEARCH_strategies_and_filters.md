# Crypto Trading with AI Bots: What Actually Makes Money

> Research compiled 2026-05-23 from academic microstructure papers, quant trading
> sources, and post-mortems on bot failures. Organized: hard truths → what makes
> money → filters → AI/ML reality → risk → execution → mapping to *this* bot.
> Sources listed at the bottom.

---

## 1. The reality check (read this first)

The numbers are brutal and consistent: **~52% of automated accounts blow up within
3 months, ~73% within 6 months.** The "AI bot that prints money" is overwhelmingly
marketing fiction. Failures cluster into five repeatable causes:

- **Overfitting** — tuned so tightly to history it mistakes noise for signal, then dies live.
- **Backtest illusion** — backtests assume perfect fills at exact historical prices. Live,
  you trade on delayed data with execution lag; a 2-second gap flips profit to loss.
- **Fees + slippage** — dozens of trades/day bleeds out. "2% daily gain" becomes a loss after costs.
- **No regime adaptation** — a trend strategy run in chop (or vice versa) loses by construction.
- **No risk management** — one un-stopped position wipes the account.

Honest framing: **edge in crypto is small, fragile, and decays as it gets crowded.**
Consistent money comes from *structural* edges (you're paid to provide something —
liquidity, a hedge, a basis) far more reliably than from *predictive* edges (guessing
direction better than the market). Keep that hierarchy in mind throughout.

---

## 2. The strategies that actually make money consistently

Ranked roughly by how *durable* and evidence-backed the edge is.

### A. Funding-rate / basis arbitrage (delta-neutral) — most reliable
Buy spot, short equal-notional perpetual future → market-neutral, collect the funding
longs pay shorts. Closest thing to "steady yield" in crypto.
- **Realistic returns:** 12–25% annualized, **Sharpe 3–6**, max drawdown typically <5%
  when run properly. Funding averages ~0.05% per 8h (~22%/yr) in bull markets.
- **The catch:** easiest trade retail can do → crowded. Of observations with a ≥20bp
  arbitrage spread, **only ~40% were profitable after transaction costs.** Pros have
  moved to *cross-exchange* funding arb (short the rich perp on one venue, long the
  cheap one on another) — now the largest professional strategy.
- **Relevance:** we already have `funding_rate_arb.py` / `funding_arb_paper.py`. This is
  our strongest structural edge — prioritize over directional scalping.

### B. Market making / order-book microstructure — strong but operationally hard
Quote both sides, earn spread + maker rebates. The alpha that keeps it from losing is
**order-flow imbalance (OFI)**.
- 2026 academic study across 5 coins: **OFI is the single strongest predictor of
  short-horizon (3-second) mid-price moves**, monotone-but-concave (extreme imbalance
  has diminishing predictive power). Spread and VWAP-to-mid deviation rank next.
- Real backtest (hftbacktest) skewing quotes by standardized OFI: **Sharpe 5–10 on BTC**
  — but return-per-trade fell from 0.0139% (2023) to 0.0086% (2025). **Maker rebates and
  fee tier are the difference between profit and loss.**
- **Danger:** OFI-based MMs got *wiped out* in the Oct 10 2025 flash crash (adverse
  selection). When everyone runs the same imbalance signal it creates self-reinforcing
  feedback loops. Spread-widening + inventory limits during volatility are mandatory.

### C. Statistical arbitrage / pairs trading — solid, capacity-limited
Two *cointegrated* coins, trade the spread on deviation, profit on reversion.
- **Method:** test pairs with Engle-Granger or Johansen; run Augmented Dickey-Fuller on
  the residual spread (need p < 0.05 = stationary). Enter ±2σ, exit at mean.
- **Edge source:** mean reversion of the *spread*, not either asset → market-neutral, more
  robust than single-asset mean reversion.
- **Risk:** relationship breaks ("cointegration breakdown") in regime shifts → hard stop
  on spread divergence + periodic re-testing of the pair.

### D. Cross-exchange & triangular arbitrage — real but a latency race
Same asset, different price across venues; or stablecoin triangular loops
(`stablecoin_arb.py`). Edge is real but small and **competes on infrastructure**
(colocation, low-latency). Retail rarely wins the pure-latency version; the *funding*
cross-exchange version (B) is more accessible.

### E. Trend / momentum — the only *directional* strategy with durable evidence
Crypto trends persist more than equities (retail-driven, reflexive). Time-series momentum
(ride established trends, cut losers fast) has the best long-run directional track record.
**But** it only works *in trending regimes* — which is why filters matter more than the
signal. Our EMA 9/21 + RSI + ADX stack is a momentum system; profitability lives or dies
on the regime filter.

---

## 3. The filters — what separates winners from losers

**Filters don't generate signal — they gate it.** They isolate the conditions where the
edge is real and forbid trading otherwise. *"Filters transform a generalized strategy into
a specialized, high-conviction system."* The five that matter most:

1. **Regime filter (the #1 filter)** — classify trending vs ranging vs volatile/crash, then
   route: momentum in trends, mean-reversion in ranges, flat/reduced size in volatile.
   Tools: HMM or rule-based ADX thresholds. A momentum strategy with no regime filter is a
   coin flip. — *We have this (`RegimeDetector`).*
2. **Volatility filter (ATR)** — skip trades when ATR > threshold; shrink size as vol rises.
   Two uses: a gate ("don't trade when ATR > X") and a sizer (position ∝ 1/volatility).
3. **Time-of-day / session filter** — liquidity, participation, news flow are cyclical and
   predictable. Win rates measurably drop in certain windows (thin Asian session, 12–16 UTC
   US-open chop). Log per-hour win rate, disable worst windows. — *BUILT (2026-06-09):
   `src/session_filter.py` rates Asia/EU/US sessions from the bot's OWN realised record
   (Wilson-LB win-rate + expectancy, min-sample guarded, fail-open). Wired as a soft check
   `_session_favorable` in `entry_checklist.py` and as a measure-first gate in
   `swing_paper.py`; `SESSION_FILTER_HARD=1` promotes it to a hard veto.*
4. **Liquidity / spread filter** — don't trade when spread is wide or depth thin; poor fills
   erase edge. Gate on `spread < X bps` and `top-of-book depth > Y`. — *BUILT: `_spread_normal`
   + `SpreadTracker` in `entry_checklist.py` (rejects spread > `SPREAD_MAX_MULT`× rolling
   median).*
5. **Order-flow / book-imbalance gate** — block entries when OFI is strongly against
   direction. Highest-information short-horizon filter per the research. — *We have this
   (`order_flow.py`, ±0.35) plus CVD-divergence + book-imbalance gates.*

Our stack (regime → strategy → OFI → lead-lag → sentiment → higher-timeframe → K-NN
learner) implements **all 5** as of 2026-06-09: the time-of-day/session gate
(`session_filter.py`) and the spread/liquidity gate (`_spread_normal`) — the two former
gaps — are now built, joining the regime, volatility (`atr_alive`), and order-flow
(`_ofi_aligned` / `_vpin_safe`) filters.

---

## 4. Where AI/ML actually helps (and where it's hype)

**What works:**
- **Hybrid LSTM + XGBoost** — LSTM for temporal patterns, XGBoost for nonlinear relations
  with auxiliary features (sentiment, macro). Consistently beats single models.
- **GRU for minute-level forecasting** — beats ARIMA/Random Forest on short-horizon error.
- **Tree models on microstructure features** — the OFI/spread/VWAP work used gradient-boosted
  trees + SHAP. **Feature engineering beats model complexity** — edge is in the features.
- **BERT/GPT news sentiment** — measurable price impact within 1 hour of publication.

**Mostly hype:**
- **"142% annual" RL agents** — same survey shows another RL agent at Sharpe 1.23 vs 1.46
  buy-and-hold (worse). RL's three killers in crypto: backtest overfitting, non-stationarity
  (market changes faster than the agent learns), data quality. Research frontier, not a
  reliable retail money-maker. Mitigations: rolling retraining on sliding windows,
  near-stationary regime segmentation, transaction-cost-aware rewards.
- **Big LLMs in the trade loop** — burning $10/day of API calls to make $2 profit. Don't put
  an expensive model in a 5-minute polling loop.

Our K-NN loss-pattern learner is the *right* use of ML for a retail bot: lightweight,
interpretable, used as a *confidence gate* (raise threshold when setup resembles past
losses) rather than a black-box predictor.

---

## 5. Risk management & position sizing — the real edge multiplier

- **Fractional Kelly, not full Kelly.** Full Kelly assumes you *know* win probability — you
  don't. Pros run **10–25% of full Kelly** (Quarter-Kelly common default).
- **Kelly caveat that bites everyone:** with <50 trades, win-rate estimate can be off by 10+
  points; Kelly ignores fees/slippage. Don't trust it until you have a large, stable sample.
- **Volatility targeting** — size positions so the *portfolio* holds constant volatility:
  scale down when vol rises, up when it falls. Makes drawdowns controllable.
- **Loss-streak de-risking** — our existing rule (2 losses → 0.75x, 4 → 0.50x) is a sound,
  simple form of this.

---

## 6. The hidden killer: execution & fees

- **Maker vs taker is decisive.** MM profitability *depended on rebates*. Use post-only /
  limit orders; earn the maker side; climb fee tiers.
- **Slippage compounds.** Move from naive market orders to limit/post-only and child-order
  schedulers (TWAP/VWAP/implementation-shortfall) for anything sized.
- **Trade less.** Every trade pays spread + fee. High frequency only works if return-per-trade
  clears costs (we saw that margin shrink to 0.0086%/trade on BTC MM). Fewer high-conviction
  trades beat many marginal ones.
- **Derivatives dominate.** Q1 2026 volume was 91% derivatives, 9% spot — funding/basis
  literacy isn't optional.

---

## 7. Concrete takeaways for *this* bot

We're already top-decile for retail bot design (regime detection, OFI gate, lead-lag,
learner-as-confidence-gate, Kelly sizing, loss-streak de-risk). Highest-value additions:

1. ~~**Add a time-of-day/session filter**~~ — DONE (2026-06-09). `src/session_filter.py`
   rates Asia/EU/US sessions from the realised journal + swing ledger; soft check in the
   entry checklist + measure-first gate in `swing_paper.py`. The proof scorecard now breaks
   P&L down "by session verdict" so the edge can be confirmed before `SESSION_FILTER_HARD=1`.
2. ~~**Add an explicit spread/liquidity gate**~~ — DONE. `_spread_normal` + `SpreadTracker`
   in `entry_checklist.py` veto entries when the spread blows out past its rolling median.
3. **Lean into funding-rate arb** — most structural (durable) edge, best Sharpe profile.
   Directional scalping is the harder, more crowded game.
4. **Verify maker-side execution** — confirm paper fills assume realistic (taker/maker) fees +
   slippage, or live results will disappoint vs paper.
5. **Don't trust adaptive sizing/learner until ~50+ trades** — small samples make Kelly and
   K-NN estimates wildly noisy.
6. **Harden against flash crashes** — Oct 2025 wiped OFI-based MMs. Make sure CRASH regime
   forces flat or tiny size (we have `regime_scale_volatile`; consider a harder crash cutoff).

---

## Sources

- [Explainable Patterns in Cryptocurrency Microstructure (arXiv 2602.00776)](https://arxiv.org/abs/2602.00776) — OFI strongest short-horizon predictor
- [Market Making with Alpha – Order Book Imbalance (hftbacktest)](https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html) — quote-skewing backtest, Sharpe 5–10, rebate dependence
- [Order flow and cryptocurrency returns (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S1386418126000029)
- [Exploring Risk and Return of Funding Rate Arbitrage CEX/DEX (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2096720925000818) — ~40% of top spreads profitable after costs
- [Funding Rate Arbitrage Complete Guide 2025 (CoinCryptoRank)](https://coincryptorank.com/blog/funding-rate-arbitrage) — 12–25% returns, Sharpe 3–6
- [The Two-Tiered Structure of Crypto Funding Rate Markets (MDPI)](https://www.mdpi.com/2227-7390/14/2/346)
- [Pairs Trading Statistical Arbitrage on Digital Assets (Medium/Digital Alpha)](https://medium.com/digital-alpha-research/using-a-pairs-trading-statistical-arbitrage-approach-on-digital-assets-e29b10c6c651)
- [Using Strategy Filters: Time of Day & Volatility (QuantStrategy.io)](https://quantstrategy.io/blog/using-strategy-filters-time-of-day-volatility-to-enhance/)
- [Kelly Criterion for Crypto Position Sizing (Altrady)](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing)
- [Deep RL for Crypto Trading: Backtest Overfitting (arXiv 2209.05559)](https://arxiv.org/pdf/2209.05559)
- [Why Most Trading Bots Lose Money (ForTraders)](https://www.fortraders.com/blog/trading-bots-lose-money)
- [What Actually Works / What Doesn't (Pump Parade)](https://pumpparade.medium.com/ai-trading-bots-lost-441k-in-one-error-heres-what-actually-works-and-what-doesn-t-4f04f890c189)
- [High-Frequency Crypto Forecasting: Comparative ML Study (MDPI)](https://www.mdpi.com/2078-2489/16/4/300)
- [Crypto Price Prediction using LSTM+XGBoost (arXiv 2506.22055)](https://arxiv.org/pdf/2506.22055)
