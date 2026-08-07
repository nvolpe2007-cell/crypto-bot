"""
Meme-coin RISK SCREENER — scoring layer.

This is a RISK FILTER, not a buy-recommendation tool. It does not rank tokens by
upside, it does not emit "buy"/"sell"/"bullish" language, and it never scores a
token as attractive. Its honest job is to explain why almost every new token is
UNTRADEABLE. The only two verdicts are:

    "REJECT" — fails at least one hard gate; do not trade it
    "PASS"   — not obviously untradeable on the data we have.  That is ALL it
               means.  A PASS is the absence of a disqualification, never a
               recommendation, never an expectation of profit.

THE CORE MODEL — round-trip cost in DOLLARS
-------------------------------------------
Every research verdict in this repo lands on the same wall: the binding
constraint is COST, not signal (see the arb verdict, the fee re-pricing study,
the funding-decay null result).  So the headline number here is not a score,
it is a dollar amount: what does it cost to get INTO a position of size S and
back OUT again?

For a constant-product AMM (x*y=k) with total pool liquidity L USD, the
quote-side reserve is ~L/2.  Spending dY of quote:

    tokens_out = X * dY / (Y + dY)          (actual)
    ideal      = X * dY / Y                 (no price impact)
    slippage   = 1 - actual/ideal = dY / (Y + dY)

With Y ~ L/2 and dY = S, one side costs S / (L/2 + S).  The exit leg is
comparable in size, so round-trip slippage ~ 2 * S / (L/2 + S).  Add the swap
fee on each side and a per-chain gas/priority estimate on each side.

`breakeven_move_pct` is the single most honest number this module produces: the
price move required just to get back to FLAT after paying to enter and exit.
It is strictly larger than `total_pct`, because the costs compound (you exit a
position that the entry already shrank).

WHEN THE MODEL DOES NOT APPLY
-----------------------------
DexScreener omits the `liquidity` object entirely for pump.fun pre-bonding-curve
pairs, and the sources layer faithfully coerces that absence to 0.0.  Two things
follow, and both are handled explicitly rather than papered over:

  * `liquidity_usd == 0.0` means liquidity is UNKNOWN, not zero.  Rejecting it
    with "liquidity $0 < $50,000 minimum" is a factually wrong reason.
  * A bonding curve is not an x*y=k pool, so the constant-product math above is
    simply invalid there.  `round_trip_cost` returns `basis="unknown"` with the
    cost fields set to None rather than inventing a slippage figure — the one
    case where a made-up number would be most tempting and least excusable.

Such a record is still a REJECT (unmodellable exit cost is a disqualification),
just for the accurate reason.  Measured base rate: 4 of the 5 newest tokens
sampled were in exactly this state, so it is the common path, not an edge case.

This module does NO network I/O.  All fetching lives in `screener_sources.py`,
which is deliberately NOT imported here — `score()` accepts any object exposing
the attributes in `TokenRecord` (duck typed; missing attributes read as None).

    python -c "import screener_risk as s; print(s.round_trip_cost(50_000, 500))"
"""
from __future__ import annotations

import math
import time
from typing import Any, Protocol, runtime_checkable

# ─── cost model constants ─────────────────────────────────────────────────────

DEFAULT_FEE_RATE = 0.003        # per side. Raydium/Uniswap-v2-class 0.30%.
DEFAULT_TICKET_USD = 500.0      # the position size all costs are quoted against

# pump.fun-class bonding-curve pools take ~1% per side — nearly 4x the standard
# AMM fee, which alone can eat the whole cost budget on a small ticket.
FEE_RATE_BY_DEX = {
    "pumpfun": 0.01,
    "pump.fun": 0.01,
    "pumpswap": 0.0025,
    "raydium": 0.0025,
    "raydiumcpmm": 0.0025,
    "raydiumclmm": 0.0025,
    "meteora": 0.003,
    "orca": 0.003,
    "uniswap": 0.003,
    "pancakeswap": 0.0025,
    "aerodrome": 0.003,
    "sushiswap": 0.003,
}

# Venues whose pre-graduation pairs are bonding curves, NOT constant-product
# pools. The x*y=k slippage model does not describe them, and DexScreener does
# not report a liquidity object for them at all.
BONDING_CURVE_DEXES = {"pumpfun", "pump.fun", "moonshot", "sunpump", "boop"}

# Gas / priority fee for ONE leg, in USD. Charged twice (entry + exit).
# Ethereum is the outlier that makes small tickets structurally impossible:
# $16 round-trip gas is 3.2% of a $500 ticket before any slippage or fee.
GAS_USD_BY_CHAIN = {
    "solana": 0.05,
    "sui": 0.02,
    "ton": 0.05,
    "polygon": 0.02,
    "base": 0.15,
    "blast": 0.15,
    "arbitrum": 0.10,
    "optimism": 0.10,
    "avalanche": 0.10,
    "bsc": 0.30,
    "tron": 0.30,
    "ethereum": 8.00,
}
DEFAULT_GAS_USD = 0.50          # unknown chain -> assume the expensive case

# Returned by round_trip_cost() when the constant-product model cannot be
# applied. Same key set as a real result so callers render it without branching;
# `basis` is the discriminator and every number is None, never a placeholder.
UNKNOWN_COST: dict = {
    "basis": "unknown",
    "slip_pct": None,
    "fee_pct": None,
    "gas_pct": None,
    "total_pct": None,
    "total_usd": None,
    "breakeven_move_pct": None,
}

# ─── gates ────────────────────────────────────────────────────────────────────
# Hard, non-negotiable disqualifiers. Any single breach is a REJECT. Exposed as
# module-level constants (readable/patchable) and as DEFAULT_GATES (overridable
# per call via score(..., gates={...})).

# Below this you cannot exit a real position: the pool IS the market, and a
# $500 exit into a $20k pool moves the price against you by several percent.
LIQ_MIN_USD = 50_000.0

# The rug/snipe window. Most rug-pulls and sniper dumps resolve inside the first
# two days; a pair that has not survived 48h has not survived anything yet.
AGE_MIN_HOURS = 48.0

# Turning the entire pool over 20x in a day is a wash-trading signature, not
# organic interest. Real markets do not recycle their whole float 20 times.
VOL_LIQ_RATIO_MAX = 20.0

# A huge notional valuation resting on a sliver of real liquidity. The "float"
# is an illusion: the FDV cannot be realised through a pool 1/100th its size.
FDV_LIQ_RATIO_MAX = 100.0

# The repo's cost wall, applied to the CALLER'S ticket size. If getting in and
# out costs more than this, the trade is a losing bet before any thesis.
RT_COST_MAX_PCT = 3.0

# Fewer trades than this and there is nothing to characterise: no distribution,
# no fill evidence, no way to tell a market from a script.
MIN_TXNS_H24 = 50

DEFAULT_GATES: dict[str, float] = {
    "LIQ_MIN_USD": LIQ_MIN_USD,
    "AGE_MIN_HOURS": AGE_MIN_HOURS,
    "VOL_LIQ_RATIO_MAX": VOL_LIQ_RATIO_MAX,
    "FDV_LIQ_RATIO_MAX": FDV_LIQ_RATIO_MAX,
    "RT_COST_MAX_PCT": RT_COST_MAX_PCT,
    "MIN_TXNS_H24": MIN_TXNS_H24,
}

# ─── flag thresholds (non-fatal observations) ─────────────────────────────────

IMBALANCE_RATIO = 3.0           # buys > 3x sells (or the reverse) is lopsided
BIG_MOVE_PCT = 100.0            # already doubled/halved inside 24h

# Stable machine-readable reason kinds, so summarize() can count them without
# parsing English.
REASON_LIQUIDITY = "liquidity"
REASON_LIQ_UNKNOWN = "liquidity_unknown"
REASON_AGE = "age"
REASON_VOL_LIQ = "vol_liq_ratio"
REASON_FDV_LIQ = "fdv_liq_ratio"
REASON_COST = "round_trip_cost"
REASON_TXNS = "txns_h24"
REASON_MISSING = "missing_data"


@runtime_checkable
class TokenRecord(Protocol):
    """Shape produced by screener_sources.py. Structural only — nothing is
    imported from that module, and every field is read defensively."""
    symbol: str
    name: str
    chain: str
    dex: str
    token_address: str
    pair_address: str
    url: str
    liquidity_usd: float
    volume_h24_usd: float
    fdv_usd: float | None
    market_cap_usd: float | None
    pair_created_ms: int | None
    age_hours: float | None
    price_usd: float | None
    price_change_h24: float | None
    buys_h24: int
    sells_h24: int
    pair_count: int
    boosted: bool


# ─── small safe helpers ───────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    """Coerce to a finite float, or None. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _int(value: Any, default: int = 0) -> int:
    n = _num(value)
    return default if n is None else int(n)


def _get(record: Any, field: str) -> Any:
    """Read an attribute off a record without ever raising. Also accepts a
    plain dict, so callers can score raw rows in a pinch."""
    if isinstance(record, dict):
        return record.get(field)
    return getattr(record, field, None)


def _usd(value: float) -> str:
    """$12,400 / $77.23 / $0.0500 — dollars lead, because that is how this repo
    thinks. Whole amounts drop the cents; sub-dollar amounts keep 4 places."""
    a = abs(value)
    if a >= 1 and float(value).is_integer():
        return f"${value:,.0f}"
    if a >= 1:
        return f"${value:,.2f}"
    if a == 0:
        return "$0"
    return f"${value:.4f}"


def fee_rate_for_dex(dex: Any) -> float:
    """Per-side swap fee for a dex id, defaulting to the Raydium-class 0.30%."""
    if not isinstance(dex, str):
        return DEFAULT_FEE_RATE
    key = dex.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    return FEE_RATE_BY_DEX.get(key, DEFAULT_FEE_RATE)


def gas_usd_for_chain(chain: Any) -> float:
    """Per-leg gas/priority estimate for a chain, defaulting to the expensive
    case because an unknown chain is not a cheap chain."""
    if not isinstance(chain, str):
        return DEFAULT_GAS_USD
    key = chain.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    return GAS_USD_BY_CHAIN.get(key, DEFAULT_GAS_USD)


def _norm(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def is_bonding_curve(record: Any) -> bool:
    """True when the pair trades on a bonding curve rather than an x*y=k pool,
    so the constant-product cost model does not apply."""
    dex = _norm(_get(record, "dex"))
    return dex in {_norm(d) for d in BONDING_CURVE_DEXES}


def liquidity_is_known(record: Any) -> bool:
    """False when `liquidity_usd` is missing or <= 0.

    DexScreener omits the liquidity object for pump.fun pre-bonding-curve pairs
    and the sources layer coerces that to 0.0, so 0.0 means UNKNOWN, not empty.
    Either way there is no number to model an exit with, which is why both
    collapse into one predicate.
    """
    liq = _num(_get(record, "liquidity_usd"))
    return liq is not None and liq > 0


# ─── the cost model ───────────────────────────────────────────────────────────


def round_trip_cost(
    liquidity_usd: float,
    ticket_usd: float,
    *,
    fee_rate: float = DEFAULT_FEE_RATE,
    gas_usd: float = 0.05,
) -> dict:
    """Cost of entering AND exiting a `ticket_usd` position in a pool holding
    `liquidity_usd` of total liquidity.

    Returns percentages of the ticket (round-trip, both legs included):

        basis              "cpmm" when the numbers below are real,
                           "unknown" when they could not be computed
        slip_pct           2 * 100 * S / (L/2 + S)     price impact
        fee_pct            2 * 100 * fee_rate          swap fee
        gas_pct            2 * 100 * gas_usd / S       gas / priority, both legs
        total_pct          the three summed
        total_usd          total_pct as dollars off the ticket
        breakeven_move_pct price move needed just to get back to flat

    If liquidity is missing, zero, or non-positive the constant-product model
    has nothing to work with -- and for a pump.fun bonding curve it would be the
    wrong model anyway -- so every cost field comes back None with
    basis="unknown". The key set is identical either way, so callers can render
    the dict without branching. A fabricated slippage number is worse than no
    number, because it looks like an answer.

    `breakeven_move_pct` compounds the two legs -- you exit a position the entry
    already shrank -- so it is always strictly greater than `total_pct`:

        (1 - c)^2 * (1 + m) = 1   =>   m = 1/(1 - c)^2 - 1

    where c is the ONE-SIDE cost fraction. If one side already costs 100% or
    more (a dust pool), breakeven is infinite: no move gets you back.

    Never raises.
    """
    ticket = _num(ticket_usd)
    liq = _num(liquidity_usd)

    # No modellable pool (missing / zero / negative liquidity) or no position:
    # report that we cannot price it rather than dividing by zero or inventing
    # a figure.
    if liq is None or liq <= 0 or ticket is None or ticket <= 0:
        return UNKNOWN_COST.copy()

    fee = _num(fee_rate)
    fee = DEFAULT_FEE_RATE if fee is None else max(fee, 0.0)
    gas = _num(gas_usd)
    gas = 0.0 if gas is None else max(gas, 0.0)

    quote_reserve = liq / 2.0
    denom = quote_reserve + ticket
    slip_side = 1.0 if denom <= 0 else ticket / denom
    gas_side = gas / ticket
    cost_side = slip_side + fee + gas_side

    slip_pct = 200.0 * slip_side
    fee_pct = 200.0 * fee
    gas_pct = 200.0 * gas_side
    total_pct = slip_pct + fee_pct + gas_pct

    if cost_side >= 1.0:
        breakeven = math.inf
    else:
        breakeven = (1.0 / ((1.0 - cost_side) ** 2) - 1.0) * 100.0

    return {
        "basis": "cpmm",
        "slip_pct": slip_pct,
        "fee_pct": fee_pct,
        "gas_pct": gas_pct,
        "total_pct": total_pct,
        "total_usd": total_pct / 100.0 * ticket,
        "breakeven_move_pct": breakeven,
    }


def _effective_age_hours(record: Any, now_ms: float | None = None) -> float | None:
    """age_hours if present, else derived from pair_created_ms, else None."""
    age = _num(_get(record, "age_hours"))
    if age is not None:
        return age
    created = _num(_get(record, "pair_created_ms"))
    if created is None or created <= 0:
        return None
    now = _num(now_ms)
    if now is None:
        now = time.time() * 1000.0
    return (now - created) / 3_600_000.0


def _notional_usd(record: Any) -> tuple[float | None, str]:
    """FDV if we have it, else market cap as a fallback -- both are 'what the
    market says this is worth', and either one dwarfing liquidity is the same
    illusion. Returns (value, label)."""
    fdv = _num(_get(record, "fdv_usd"))
    if fdv is not None and fdv > 0:
        return fdv, "FDV"
    mcap = _num(_get(record, "market_cap_usd"))
    if mcap is not None and mcap > 0:
        return mcap, "market cap"
    return None, "FDV"


# ─── scoring ──────────────────────────────────────────────────────────────────


def score(
    record: Any,
    ticket_usd: float = DEFAULT_TICKET_USD,
    *,
    fee_rate: float | None = None,
    gates: dict | None = None,
    now_ms: float | None = None,
) -> dict:
    """Screen one token record for tradeability.

    Returns:
        {"verdict": "PASS"|"REJECT",
         "reasons": [str],        # gate failures, each with the actual number
         "reason_kinds": [str],   # machine-readable counterparts, for summarize
         "flags": [str],          # non-fatal observations, never disqualifying
         "cost": {...},           # round_trip_cost() output for this ticket
         "metrics": {...}}

    PASS means "nothing here disqualifies it", nothing more. Missing or zero
    data is treated as a REJECT reason, never as an exception: a token we cannot
    characterise is a token we cannot trade.

    When liquidity is unreported (pump.fun-class bonding curves), `cost` comes
    back with basis="unknown" and no numbers, the verdict is REJECT for that
    accurate reason, and the RT_COST gate is skipped rather than failed on a
    figure the model could not legitimately produce.
    """
    g = dict(DEFAULT_GATES)
    if gates:
        g.update({k: v for k, v in gates.items() if _num(v) is not None})

    liq_min = float(g["LIQ_MIN_USD"])
    age_min = float(g["AGE_MIN_HOURS"])
    vol_liq_max = float(g["VOL_LIQ_RATIO_MAX"])
    fdv_liq_max = float(g["FDV_LIQ_RATIO_MAX"])
    cost_max = float(g["RT_COST_MAX_PCT"])
    txns_min = float(g["MIN_TXNS_H24"])

    ticket = _num(ticket_usd)
    ticket = DEFAULT_TICKET_USD if ticket is None or ticket <= 0 else ticket

    liq = _num(_get(record, "liquidity_usd"))
    vol = _num(_get(record, "volume_h24_usd"))
    age = _effective_age_hours(record, now_ms)
    notional, notional_label = _notional_usd(record)
    buys = _int(_get(record, "buys_h24"))
    sells = _int(_get(record, "sells_h24"))
    txns = max(buys, 0) + max(sells, 0)
    pair_count = _int(_get(record, "pair_count"))
    change = _num(_get(record, "price_change_h24"))

    rate = _num(fee_rate)
    if rate is None:
        rate = fee_rate_for_dex(_get(record, "dex"))
    gas = gas_usd_for_chain(_get(record, "chain"))

    cost = round_trip_cost(liq if liq is not None else 0.0, ticket,
                           fee_rate=rate, gas_usd=gas)
    cost_known = cost.get("basis") == "cpmm"

    reasons: list[str] = []
    kinds: list[str] = []

    def reject(kind: str, text: str) -> None:
        reasons.append(text)
        kinds.append(kind)

    # 1. Liquidity — below the floor you cannot exit a real position.
    #    Missing/zero is a DIFFERENT failure from "too small": DexScreener omits
    #    the liquidity object for pump.fun pre-bonding-curve pairs, and the
    #    sources layer coerces that absence to 0.0. Saying "$0 < $50,000" there
    #    would be a wrong reason for a right verdict.
    if liq is None or liq <= 0:
        if is_bonding_curve(record):
            reject(REASON_LIQ_UNKNOWN,
                   "liquidity not reported (pump.fun-class bonding curve, not "
                   "a constant-product pool) - exit cost cannot be modelled")
        else:
            reject(REASON_LIQ_UNKNOWN,
                   "liquidity not reported or zero - exit cost cannot be "
                   "modelled, and an empty pool has no exit at any size")
    elif liq < liq_min:
        reject(REASON_LIQUIDITY,
               f"liquidity {_usd(liq)} < {_usd(liq_min)} minimum")

    # 2. Age — the rug/snipe window.
    if age is None:
        reject(REASON_AGE,
               f"age unknown: cannot confirm it cleared the {age_min:.0f}h "
               "rug/snipe window")
    elif age < age_min:
        reject(REASON_AGE,
               f"age {age:.1f}h < {age_min:.0f}h minimum (rug/snipe window)")

    # 3. Volume / liquidity — wash-trading signature.
    vol_liq = None
    if liq is not None and liq > 0 and vol is not None:
        vol_liq = vol / liq
        if vol_liq > vol_liq_max:
            reject(REASON_VOL_LIQ,
                   f"24h volume {_usd(vol)} is {vol_liq:.1f}x liquidity "
                   f"{_usd(liq)} > {vol_liq_max:.1f}x maximum "
                   "(wash-trading signature)")
    elif vol is None:
        reject(REASON_MISSING,
               "24h volume unknown: cannot test for wash trading")

    # 4. Notional / liquidity — the float is an illusion.
    fdv_liq = None
    if notional is None:
        reject(REASON_MISSING,
               "FDV and market cap both unknown: cannot test valuation "
               "against real liquidity")
    elif liq is not None and liq > 0:
        fdv_liq = notional / liq
        if fdv_liq > fdv_liq_max:
            reject(REASON_FDV_LIQ,
                   f"{notional_label} {_usd(notional)} is {fdv_liq:.1f}x "
                   f"liquidity {_usd(liq)} > {fdv_liq_max:.1f}x maximum "
                   "(the float is an illusion)")

    # 5. Round-trip cost — the repo's cost wall, in dollars first.
    #    Skipped entirely when the pool is not modellable: the liquidity gate
    #    above already rejected it, and failing it a second time on a fabricated
    #    percentage would be inventing evidence.
    limit_usd = cost_max / 100.0 * ticket
    if cost_known and cost["total_pct"] > cost_max:
        be = cost["breakeven_move_pct"]
        be_txt = "infinite" if math.isinf(be) else f"{be:.2f}%"
        reject(REASON_COST,
               f"round-trip cost {_usd(cost['total_usd'])} "
               f"({cost['total_pct']:.2f}%) on a {_usd(ticket)} ticket > "
               f"{_usd(limit_usd)} ({cost_max:.1f}%) limit; needs a "
               f"{be_txt} move just to break even")

    # 6. Trade count — nothing to characterise.
    if txns < txns_min:
        reject(REASON_TXNS,
               f"{txns} trades in 24h < {txns_min:.0f} minimum "
               f"({buys} buys / {sells} sells)")

    # ── flags: observations, not disqualifications ────────────────────────────
    flags: list[str] = []

    if is_bonding_curve(record):
        flags.append("pre-bonding-curve launch on "
                     f"{_get(record, 'dex') or 'a bonding-curve venue'}: not a "
                     "constant-product pool, standard AMM cost math does not "
                     "apply")

    if not cost_known:
        flags.append("round-trip cost not modellable: no usable liquidity "
                     "figure, so no cost number is reported")

    if pair_count == 1:
        flags.append("single pool: one LP pull ends the market")

    if bool(_get(record, "boosted")):
        flags.append("paid promotion: someone paid for this placement")

    if buys > 0 or sells > 0:
        if sells == 0:
            flags.append(f"one-sided flow: {buys} buys and 0 sells in 24h")
        elif buys == 0:
            flags.append(f"one-sided flow: {sells} sells and 0 buys in 24h")
        elif buys > IMBALANCE_RATIO * sells:
            flags.append(f"lopsided flow: {buys} buys vs {sells} sells in 24h "
                         f"({buys / sells:.1f}x)")
        elif sells > IMBALANCE_RATIO * buys:
            flags.append(f"lopsided flow: {sells} sells vs {buys} buys in 24h "
                         f"({sells / buys:.1f}x)")

    if change is not None and abs(change) > BIG_MOVE_PCT:
        flags.append(f"already moved >{BIG_MOVE_PCT:.0f}% in 24h "
                     f"({change:+.1f}%)")

    missing = [f for f in ("fdv_usd", "age_hours")
               if _num(_get(record, f)) is None]
    if missing:
        flags.append("incomplete data, treat with suspicion: "
                     + ", ".join(missing) + " missing")

    return {
        "verdict": "REJECT" if reasons else "PASS",
        "reasons": reasons,
        "reason_kinds": kinds,
        "flags": flags,
        "cost": cost,
        "metrics": {
            "vol_liq_ratio": vol_liq,
            "fdv_liq_ratio": fdv_liq,
            "txns_h24": txns,
            "cost_basis": cost.get("basis"),
            "age_hours": age,
            "liquidity_usd": liq,
            "ticket_usd": ticket,
            "fee_rate": rate,
            "gas_usd_per_leg": gas,
        },
        "symbol": _get(record, "symbol"),
        "chain": _get(record, "chain"),
    }


def summarize(scored: list[dict]) -> dict:
    """Roll up a batch of score() results.

    top_reasons is ordered most-common first and is the useful output of the
    whole tool: it says WHICH structural problem killed the batch.
    """
    rows = [r for r in (scored or []) if isinstance(r, dict)]
    total = len(rows)
    passed = sum(1 for r in rows if r.get("verdict") == "PASS")
    rejected = sum(1 for r in rows if r.get("verdict") == "REJECT")

    counts: dict[str, int] = {}
    for r in rows:
        kinds = r.get("reason_kinds")
        if not isinstance(kinds, list):
            # Fall back to counting distinct reason strings if a caller built
            # the dict by hand without the machine-readable kinds.
            kinds = r.get("reasons") if isinstance(r.get("reasons"), list) else []
        for kind in dict.fromkeys(kinds):   # de-dupe within one record
            counts[str(kind)] = counts.get(str(kind), 0) + 1

    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"total": total, "passed": passed, "rejected": rejected,
            "top_reasons": top}
