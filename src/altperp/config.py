"""
All tunable parameters for the alt-perp confluence strategy.

Tune here without touching strategy logic. Values are the spec defaults; the
`validate()` function sanity-checks them at startup.
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────────
# PAPER_TRADING: when True, orders are simulated (logged, never sent to an
# exchange) and PnL is tracked in the database. There is currently NO Bybit
# execution client, so this MUST stay True until one is built + Bybit keys added.
PAPER_TRADING = os.getenv("ALTPERP_PAPER", "1") == "1"

# Keep the conservative funding-arb "majors" arm running alongside this strategy
# (user decision). The runner only owns the alt-perp strategy; the funding arm
# continues to run from the existing paper_trading session.
KEEP_FUNDING_MAJORS_ARM = True

# ── Markets ──────────────────────────────────────────────────────────────────
# Signals are sourced from Bybit (deep OI/funding = the crowd we fade) for ALL
# target coins. Execution is on Kraken Futures. Research finding: only SOL has
# tradeable Kraken liquidity (~$18M/day); AVAX (~$0.9M/day) and ARB (~$0.16M/day)
# are too thin — live orders would slip more than the edge. So AVAX/ARB run in
# PAPER/signal-only mode regardless of the global PAPER_TRADING flag.
TARGET_COINS = ["SOLUSDT", "AVAXUSDT", "ARBUSDT"]   # monitored (Bybit signals)
LIVE_EXECUTION_COINS = ["SOLUSDT"]                  # liquid enough to execute live on Kraken
SPOT_SUFFIX = "USDT"                                # spot symbol == perp symbol on Bybit
BTC_REF_SYMBOL = "BTCUSDT"                          # flush-long BTC-trend filter

# Bybit signal symbol → Kraken Futures execution symbol (PF_ multi-collateral perps)
KRAKEN_SYMBOL_MAP = {
    "SOLUSDT": "PF_SOLUSD",
    "AVAXUSDT": "PF_AVAXUSD",
    "ARBUSDT": "PF_ARBUSD",
}

# ── Trend / regime filter (MANDATORY per research — BIS crypto-carry) ─────────
# The #1 failure mode is fading shorts into a sustained uptrend (funding stays
# extreme for weeks while price rips). Block shorts in a confirmed uptrend, and
# block flush-longs in a confirmed downtrend. Trend measured on 4h closes.
TREND_FILTER_ENABLED = True
TREND_EMA_PERIOD = 50          # 4h EMA
TREND_SLOPE_LOOKBACK = 6       # bars to measure EMA slope (6×4h = 24h)
TREND_EXT_PCT = 0.03           # price >3% above rising EMA = strong uptrend (block short)

# ── Tier 1 thresholds (gate signals — both required) ─────────────────────────
FUNDING_THRESHOLD_SHORT = 0.0005       # >= 0.05% per 8h funding → short setup eligible
FUNDING_THRESHOLD_LONG  = -0.0003      # <= -0.03% per 8h → long/squeeze setup eligible
FUNDING_SPIKE_MULTIPLIER = 2.0         # current rate must be >= 2x the 48h rolling avg
FUNDING_HISTORY_LEN = 6                # rolling readings kept per coin (6 × 8h = 48h)

OI_SPIKE_THRESHOLD_4HR = 0.25          # OI +25% over last 4h candle → short setup
OI_SPIKE_THRESHOLD_8HR = 0.35          # OI +35% over last 8h → short setup
OI_FLUSH_THRESHOLD     = 0.20          # OI -20% over 1–2 candles → flush-long setup
OI_INTERVAL = "4h"                     # Bybit open-interest intervalTime

# ── Tier 2 thresholds (direction confirmation — boosts size, not required) ───
LIQ_PROXIMITY_PCT = 0.02               # price within 2% of a large book cluster
LIQ_CLUSTER_DEPTH = 200                # orderbook depth levels to scan
LIQ_CLUSTER_SIZE_MULT = 4.0            # a "wall" = level size >= this × median level
                                       # (median is robust to a single dominant wall,
                                       #  unlike mean+stdev which the wall inflates)
VOLUME_SPIKE_MULTIPLIER = 3.0          # flush long: last candle vol >= 3x 20-period avg
CVD_WINDOW_MINUTES = 240               # CVD cumulated over trailing 4h of 1m intervals

# ── Position sizing ──────────────────────────────────────────────────────────
BASE_RISK_PCT = 0.01                   # risk 1% of equity per trade
BASE_LEVERAGE = 3                      # default leverage
MAX_LEVERAGE = 5                       # absolute cap — never exceed
# Boost capped at 1.5x (was 2.0): research shows this fade is negatively skewed —
# being biggest on a crowded short right before a squeeze is the wrong moment.
TIER2_CVD_SIZE_BOOST = 1.25            # 1.25x when Tier1 + CVD confirmed
MAX_SIZE_BOOST = 1.5                   # 1.5x when Tier1 + CVD + liq proximity (hard cap)

# ── Execution costs (paper sim + future live) ────────────────────────────────
KRAKEN_TAKER_FEE = 0.0005              # 0.05% per side (Kraken Futures base taker)
PAPER_SLIPPAGE_PCT = 0.0005            # conservative 0.05%/fill against us (SOL on Kraken is thin)

# ── Exits — SHORT (Setup A) ──────────────────────────────────────────────────
# R:R reworked from the spec defaults: the original 1.2% stop sat right under the
# 1.5% TP1 (~1.25:1) and got wicked out by squeeze noise on volatile alt fades.
# Wider stop (2.0%) gives the reversion room; TP2/TP3 pushed out so winners clearly
# beat the stop. Blended win ≈ +2.9% vs −2.0% stop ≈ 1.45:1, and the post-TP1 trail
# locks ~0.5% on the remaining 60%. Risk-based sizing keeps the 1% risk constant.
SHORT_TP1_PCT = 0.015                  # TP1 at -1.5% → close 40% (quick de-risk, ~0.75R)
SHORT_TP2_PCT = 0.030                  # TP2 at -3.0% → close 35% (1.5R)
SHORT_TP3_PCT = 0.050                  # TP3 at -5.0% → close remaining 25% (2.5R)
SHORT_TP1_CLOSE_PCT = 0.40
SHORT_TP2_CLOSE_PCT = 0.35
SHORT_TP3_CLOSE_PCT = 0.25
SHORT_STOP_PCT = 0.020                 # hard stop at +2.0% (was 1.2% — too tight)
SHORT_TRAIL_PCT = 0.010                # after TP1, trail 1.0% above price (locks ~0.5%)
FUNDING_EXIT_THRESHOLD = 0.0002        # exit short if funding drops below 0.02% (invalidated)
TIME_STOP_HOURS = 8                    # close if open >8h with <0.5% profit
TIME_STOP_MIN_PROFIT_PCT = 0.005

# ── Exits — LONG (Setup B, flush) ────────────────────────────────────────────
# Same fix as the short: a flush can overshoot, so 1.5% was too tight a stop.
# Widened to 2.0% with TP nudged to 2.5% to keep R:R ≈ 1.25:1 on the 70% leg.
LONG_TP_PCT = 0.025                    # close 70% at +2.5%
LONG_TP_CLOSE_PCT = 0.70
LONG_TRAIL_PCT = 0.015                 # trail remaining 30% by 1.5%
LONG_STOP_PCT = 0.020                  # hard stop at -2.0% (was 1.5% — too tight)
LONG_OI_REVERSAL_PCT = 0.10            # exit flush-long if OI rebuilds +10% (new longs piling in)

# ── Risk controls ────────────────────────────────────────────────────────────
DAILY_DRAWDOWN_HALT_PCT = 0.05         # halt 24h if equity -5% in a day
MAX_DRAWDOWN_HALT_PCT = 0.10           # halt + alert if equity -10% from ATH
MAX_CONCURRENT_POSITIONS = 2           # across all coins
MAX_POSITIONS_PER_COIN = 1
MAX_POSITION_AGE_MINS = 30             # don't open new if an existing pos is 30+ min underwater
USE_ISOLATED_MARGIN = True             # CRITICAL for live — never cross margin

# ── Funding reset timing (UTC) ───────────────────────────────────────────────
FUNDING_RESET_HOURS_UTC = (0, 8, 16)
PRE_FUNDING_WINDOW_MINS = 90           # best short-entry window before a reset
POST_FUNDING_BLOCK_MINS = 30           # don't open shorts within 30m after a reset

# ── Entry execution ──────────────────────────────────────────────────────────
SHORT_LIMIT_PREMIUM_PCT = 0.0005       # place short limit 0.05% above ask
ORDER_FILL_TIMEOUT_SECS = 120          # cancel + re-evaluate if unfilled in 2 min

# ── Loop / data ──────────────────────────────────────────────────────────────
SIGNAL_LOOP_INTERVAL_SECS = 300        # evaluate every 5 minutes per coin
RECENT_TRADE_LIMIT = 1000              # Bybit recent-trade max
BYBIT_RATE_LIMIT_PER_SEC = 8           # stay under the 10 req/s market-data cap
PAPER_STARTING_EQUITY = float(os.getenv("ALTPERP_EQUITY", "1000"))

# ── Storage ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv(
    "ALTPERP_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "trades.db"),
)


def validate() -> list:
    """Return a list of config problems (empty == OK). Called at startup."""
    problems = []
    if BASE_LEVERAGE > MAX_LEVERAGE:
        problems.append(f"BASE_LEVERAGE {BASE_LEVERAGE} > MAX_LEVERAGE {MAX_LEVERAGE}")
    if MAX_SIZE_BOOST < TIER2_CVD_SIZE_BOOST:
        problems.append("MAX_SIZE_BOOST < TIER2_CVD_SIZE_BOOST")
    if not (SHORT_TP1_PCT < SHORT_TP2_PCT < SHORT_TP3_PCT):
        problems.append("SHORT TP levels must be increasing")
    if abs(SHORT_TP1_CLOSE_PCT + SHORT_TP2_CLOSE_PCT + SHORT_TP3_CLOSE_PCT - 1.0) > 1e-9:
        problems.append("SHORT TP close fractions must sum to 1.0")
    if SHORT_STOP_PCT <= 0 or LONG_STOP_PCT <= 0:
        problems.append("stops must be positive")
    if not TARGET_COINS:
        problems.append("TARGET_COINS is empty")
    return problems
