"""Meme-coin risk screener — scoring layer (scripts/screener_risk.py).

The module is a REJECT-machine: these tests pin the cost arithmetic, prove every
gate rejects when breached and stays quiet when satisfied, and prove that no
combination of missing/zero fields can raise instead of rejecting.
"""
import math
import os
import sys
from dataclasses import dataclass

import pytest

# scripts/ is not a package, so put it on the path directly. Nothing is imported
# from screener_sources here — score() is duck typed against the fake below.
_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import screener_risk as sr  # noqa: E402


@dataclass
class FakeRecord:
    """Stand-in for the screener_sources record. Defaults describe a token that
    clears every gate, so each test perturbs exactly one field."""
    symbol: str = "HEALTHY"
    name: str = "Healthy Token"
    chain: str = "solana"
    dex: str = "raydium"
    token_address: str = "Tok111"
    pair_address: str = "Pair111"
    url: str = "https://example.invalid/pair"
    liquidity_usd: float = 500_000.0
    volume_h24_usd: float = 1_000_000.0      # 2.0x liquidity
    fdv_usd: float | None = 5_000_000.0      # 10x liquidity
    market_cap_usd: float | None = 5_000_000.0
    pair_created_ms: int | None = None
    age_hours: float | None = 720.0          # 30 days
    price_usd: float | None = 0.005
    price_change_h24: float | None = 12.0
    buys_h24: int = 900
    sells_h24: int = 800
    pair_count: int = 3
    boosted: bool = False


def _kinds(result):
    return set(result["reason_kinds"])


# ─── the cost model ───────────────────────────────────────────────────────────


class TestRoundTripCost:
    def test_slippage_matches_hand_computation(self):
        # S=$500 into L=$50,000: quote reserve ~ $25,000, so one side is
        # 500 / 25,500 = 1.9608%, round trip ~ 3.9216%.
        c = sr.round_trip_cost(50_000.0, 500.0, fee_rate=0.0, gas_usd=0.0)
        assert c["slip_pct"] == pytest.approx(2 * 100 * 500 / 25_500)
        assert c["slip_pct"] == pytest.approx(3.921568, abs=1e-5)
        assert c["total_pct"] == pytest.approx(c["slip_pct"])

    def test_fee_and_gas_are_charged_on_both_legs(self):
        c = sr.round_trip_cost(1_000_000.0, 500.0, fee_rate=0.003, gas_usd=0.05)
        assert c["fee_pct"] == pytest.approx(0.6)              # 2 * 0.30%
        assert c["gas_pct"] == pytest.approx(2 * 100 * 0.05 / 500)
        assert c["total_pct"] == pytest.approx(
            c["slip_pct"] + c["fee_pct"] + c["gas_pct"])

    def test_total_usd_is_total_pct_of_the_ticket(self):
        c = sr.round_trip_cost(50_000.0, 500.0)
        assert c["total_usd"] == pytest.approx(c["total_pct"] / 100.0 * 500.0)

    def test_cost_rises_as_liquidity_falls(self):
        costs = [sr.round_trip_cost(liq, 500.0)["total_pct"]
                 for liq in (5_000_000.0, 500_000.0, 50_000.0, 5_000.0)]
        assert costs == sorted(costs)
        assert all(a < b for a, b in zip(costs, costs[1:]))

    def test_cost_rises_with_ticket_size(self):
        small = sr.round_trip_cost(100_000.0, 100.0)["slip_pct"]
        big = sr.round_trip_cost(100_000.0, 5_000.0)["slip_pct"]
        assert big > small

    def test_breakeven_exceeds_total_cost_because_legs_compound(self):
        c = sr.round_trip_cost(200_000.0, 500.0)
        assert c["breakeven_move_pct"] > c["total_pct"]
        # exact relation implemented: (1-c_side)^2 * (1+m) = 1
        side = c["total_pct"] / 200.0
        assert c["breakeven_move_pct"] == pytest.approx(
            (1.0 / (1.0 - side) ** 2 - 1.0) * 100.0)

    def test_breakeven_infinite_when_one_side_costs_everything(self):
        # a ticket far larger than the pool: slippage alone exceeds 100% a side
        c = sr.round_trip_cost(10.0, 10_000.0)
        assert math.isinf(c["breakeven_move_pct"])

    def test_zero_liquidity_does_not_divide_by_zero(self):
        c = sr.round_trip_cost(0.0, 500.0)
        assert c["basis"] == "unknown"
        assert c["total_pct"] is None
        assert c["breakeven_move_pct"] is None

    def test_zero_ticket_does_not_divide_by_zero(self):
        c = sr.round_trip_cost(50_000.0, 0.0)
        assert c["basis"] == "unknown"
        assert all(c[k] is None for k in
                   ("slip_pct", "fee_pct", "gas_pct", "total_pct", "total_usd"))

    def test_none_inputs_never_raise(self):
        assert sr.round_trip_cost(None, None)["basis"] == "unknown"
        assert sr.round_trip_cost(None, 500.0)["basis"] == "unknown"
        assert sr.round_trip_cost(50_000.0, None)["basis"] == "unknown"

    def test_unknown_and_known_results_share_a_key_set(self):
        known = sr.round_trip_cost(50_000.0, 500.0)
        unknown = sr.round_trip_cost(0.0, 500.0)
        assert known.keys() == unknown.keys()
        assert known["basis"] == "cpmm"

    def test_ethereum_gas_dominates_a_small_ticket(self):
        gas = sr.gas_usd_for_chain("ethereum")
        c = sr.round_trip_cost(1_000_000.0, 100.0, gas_usd=gas)
        assert c["gas_pct"] == pytest.approx(16.0)   # 2 * $8 on a $100 ticket
        assert c["total_pct"] > 16.0

    def test_dex_and_chain_lookups_have_safe_defaults(self):
        assert sr.fee_rate_for_dex("pumpfun") == 0.01
        assert sr.fee_rate_for_dex("Raydium") == 0.0025
        assert sr.fee_rate_for_dex(None) == sr.DEFAULT_FEE_RATE
        assert sr.fee_rate_for_dex("never-heard-of-it") == sr.DEFAULT_FEE_RATE
        assert sr.gas_usd_for_chain("Solana") == 0.05
        assert sr.gas_usd_for_chain(None) == sr.DEFAULT_GAS_USD


# ─── the healthy baseline ─────────────────────────────────────────────────────


class TestHealthyRecord:
    def test_fully_healthy_record_passes(self):
        out = sr.score(FakeRecord())
        assert out["verdict"] == "PASS", out["reasons"]
        assert out["reasons"] == []
        assert out["reason_kinds"] == []

    def test_pass_still_reports_the_cost_in_dollars(self):
        out = sr.score(FakeRecord(), 500.0)
        assert out["cost"]["basis"] == "cpmm"
        assert out["cost"]["total_usd"] > 0
        assert out["cost"]["breakeven_move_pct"] > out["cost"]["total_pct"]

    def test_metrics_are_populated(self):
        out = sr.score(FakeRecord())
        m = out["metrics"]
        assert m["vol_liq_ratio"] == pytest.approx(2.0)
        assert m["fdv_liq_ratio"] == pytest.approx(10.0)
        assert m["txns_h24"] == 1700

    def test_verdict_vocabulary_is_only_pass_or_reject(self):
        for rec in (FakeRecord(), FakeRecord(liquidity_usd=1_000.0)):
            assert sr.score(rec)["verdict"] in ("PASS", "REJECT")


# ─── one test per gate ────────────────────────────────────────────────────────


class TestLiquidityGate:
    def test_below_minimum_rejects(self):
        out = sr.score(FakeRecord(liquidity_usd=12_400.0, volume_h24_usd=50_000.0,
                                  fdv_usd=500_000.0))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_LIQUIDITY in _kinds(out)
        assert any("$12,400" in r and "$50,000" in r for r in out["reasons"])

    def test_at_minimum_passes_the_liquidity_gate(self):
        out = sr.score(FakeRecord(liquidity_usd=sr.LIQ_MIN_USD,
                                  volume_h24_usd=100_000.0,
                                  fdv_usd=1_000_000.0),
                       ticket_usd=100.0)
        assert sr.REASON_LIQUIDITY not in _kinds(out)

    def test_gate_override_is_honoured(self):
        rec = FakeRecord(liquidity_usd=20_000.0, volume_h24_usd=40_000.0,
                         fdv_usd=200_000.0)
        assert sr.REASON_LIQUIDITY in _kinds(sr.score(rec, 100.0))
        loose = sr.score(rec, 100.0, gates={"LIQ_MIN_USD": 10_000.0})
        assert sr.REASON_LIQUIDITY not in _kinds(loose)


class TestAgeGate:
    def test_young_pair_rejects(self):
        out = sr.score(FakeRecord(age_hours=6.0))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_AGE in _kinds(out)
        assert any("6.0h" in r and "48h" in r for r in out["reasons"])

    def test_old_pair_passes_the_age_gate(self):
        assert sr.REASON_AGE not in _kinds(sr.score(FakeRecord(age_hours=49.0)))

    def test_age_derived_from_pair_created_ms_when_absent(self):
        now_ms = 1_000_000_000_000
        rec = FakeRecord(age_hours=None,
                         pair_created_ms=now_ms - 10 * 3_600_000)
        out = sr.score(rec, now_ms=now_ms)
        assert out["metrics"]["age_hours"] == pytest.approx(10.0)
        assert sr.REASON_AGE in _kinds(out)

    def test_unknown_age_rejects(self):
        out = sr.score(FakeRecord(age_hours=None, pair_created_ms=None))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_AGE in _kinds(out)


class TestVolumeLiquidityGate:
    def test_wash_trading_ratio_rejects(self):
        # 41x turnover of the whole pool in a day
        out = sr.score(FakeRecord(liquidity_usd=100_000.0,
                                  volume_h24_usd=4_100_000.0,
                                  fdv_usd=1_000_000.0))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_VOL_LIQ in _kinds(out)
        assert any("41.0x" in r and "20.0x" in r for r in out["reasons"])

    def test_organic_ratio_passes_the_gate(self):
        out = sr.score(FakeRecord(liquidity_usd=100_000.0,
                                  volume_h24_usd=500_000.0,
                                  fdv_usd=1_000_000.0))
        assert sr.REASON_VOL_LIQ not in _kinds(out)


class TestFdvLiquidityGate:
    def test_illusory_float_rejects(self):
        out = sr.score(FakeRecord(liquidity_usd=60_000.0,
                                  volume_h24_usd=120_000.0,
                                  fdv_usd=9_000_000.0,
                                  market_cap_usd=9_000_000.0))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_FDV_LIQ in _kinds(out)
        assert any("150.0x" in r and "100.0x" in r for r in out["reasons"])

    def test_sane_ratio_passes_the_gate(self):
        assert sr.REASON_FDV_LIQ not in _kinds(sr.score(FakeRecord()))

    def test_market_cap_used_when_fdv_missing(self):
        rec = FakeRecord(fdv_usd=None, market_cap_usd=100_000_000.0)
        out = sr.score(rec)
        assert out["metrics"]["fdv_liq_ratio"] == pytest.approx(200.0)
        assert sr.REASON_FDV_LIQ in _kinds(out)

    def test_both_notionals_missing_rejects_as_missing_data(self):
        out = sr.score(FakeRecord(fdv_usd=None, market_cap_usd=None))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_MISSING in _kinds(out)


class TestRoundTripCostGate:
    def test_thin_pool_breaches_the_cost_wall(self):
        # $500 into a $60k pool: ~1.65% a side of slippage alone -> >3% RT
        out = sr.score(FakeRecord(liquidity_usd=60_000.0,
                                  volume_h24_usd=120_000.0,
                                  fdv_usd=600_000.0), ticket_usd=500.0)
        assert out["verdict"] == "REJECT"
        assert sr.REASON_COST in _kinds(out)
        assert any("break even" in r for r in out["reasons"])

    def test_same_pool_passes_with_a_smaller_ticket(self):
        rec = FakeRecord(liquidity_usd=60_000.0, volume_h24_usd=120_000.0,
                         fdv_usd=600_000.0)
        out = sr.score(rec, ticket_usd=50.0)
        assert sr.REASON_COST not in _kinds(out)
        assert out["verdict"] == "PASS", out["reasons"]

    def test_cost_reason_leads_with_dollars(self):
        out = sr.score(FakeRecord(liquidity_usd=60_000.0,
                                  volume_h24_usd=120_000.0,
                                  fdv_usd=600_000.0), ticket_usd=500.0)
        reason = next(r for k, r in zip(out["reason_kinds"], out["reasons"])
                      if k == sr.REASON_COST)
        assert reason.index("$") < reason.index("%")


class TestTxnsGate:
    def test_too_few_trades_rejects(self):
        out = sr.score(FakeRecord(buys_h24=12, sells_h24=12))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_TXNS in _kinds(out)
        assert any("24 trades" in r and "50" in r for r in out["reasons"])

    def test_enough_trades_passes_the_gate(self):
        out = sr.score(FakeRecord(buys_h24=30, sells_h24=25))
        assert sr.REASON_TXNS not in _kinds(out)


# ─── pump.fun / unreported liquidity ──────────────────────────────────────────


class TestUnreportedLiquidity:
    """DexScreener omits the liquidity object for pump.fun pre-bonding-curve
    pairs and the sources layer coerces that to 0.0 — the common path, not an
    edge case (4 of 5 newest tokens sampled)."""

    def _rec(self):
        return FakeRecord(symbol="FRESH", dex="pumpfun", liquidity_usd=0.0,
                          volume_h24_usd=27_475.0, fdv_usd=13_706.0,
                          market_cap_usd=13_706.0, age_hours=2.0,
                          pair_count=1, buys_h24=310, sells_h24=250)

    def test_rejects_without_raising(self):
        out = sr.score(self._rec())
        assert out["verdict"] == "REJECT"

    def test_reports_no_numeric_round_trip_cost(self):
        out = sr.score(self._rec())
        cost = out["cost"]
        assert cost["basis"] == "unknown"
        for key in ("slip_pct", "fee_pct", "gas_pct", "total_pct",
                    "total_usd", "breakeven_move_pct"):
            assert cost[key] is None, key
        assert out["metrics"]["cost_basis"] == "unknown"

    def test_reason_is_accurate_not_a_false_liquidity_comparison(self):
        out = sr.score(self._rec())
        assert sr.REASON_LIQ_UNKNOWN in _kinds(out)
        assert sr.REASON_LIQUIDITY not in _kinds(out)
        assert sr.REASON_COST not in _kinds(out)      # never failed on a fake number
        reason = next(r for k, r in zip(out["reason_kinds"], out["reasons"])
                      if k == sr.REASON_LIQ_UNKNOWN)
        assert "not reported" in reason
        assert "bonding curve" in reason
        assert "$0" not in reason                     # no "$0 < $50,000" nonsense

    def test_bonding_curve_is_flagged(self):
        flags = sr.score(self._rec())["flags"]
        assert any("bonding-curve" in f for f in flags)
        assert any("not modellable" in f for f in flags)

    def test_helpers_agree(self):
        rec = self._rec()
        assert sr.liquidity_is_known(rec) is False
        assert sr.is_bonding_curve(rec) is True
        assert sr.liquidity_is_known(FakeRecord()) is True
        assert sr.is_bonding_curve(FakeRecord()) is False

    def test_zero_liquidity_on_a_normal_amm_rejects_too(self):
        out = sr.score(FakeRecord(liquidity_usd=0.0, dex="raydium"))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_LIQ_UNKNOWN in _kinds(out)
        assert out["cost"]["basis"] == "unknown"


# ─── flags ────────────────────────────────────────────────────────────────────


class TestFlags:
    def test_single_pool_flagged(self):
        assert any("single pool" in f
                   for f in sr.score(FakeRecord(pair_count=1))["flags"])

    def test_multiple_pools_not_flagged(self):
        assert not any("single pool" in f
                       for f in sr.score(FakeRecord(pair_count=4))["flags"])

    def test_boosted_flagged(self):
        assert any("paid promotion" in f
                   for f in sr.score(FakeRecord(boosted=True))["flags"])

    def test_buy_sell_imbalance_flagged_neutrally(self):
        flags = sr.score(FakeRecord(buys_h24=900, sells_h24=100))["flags"]
        lopsided = [f for f in flags if "lopsided flow" in f]
        assert lopsided and "900 buys vs 100 sells" in lopsided[0]
        # a risk filter never characterises flow as good or bad
        blob = " ".join(flags).lower()
        assert not any(w in blob for w in ("bullish", "bearish", "buy signal"))

    def test_balanced_flow_not_flagged(self):
        assert not any("lopsided" in f
                       for f in sr.score(FakeRecord())["flags"])

    def test_big_move_flagged(self):
        assert any("already moved" in f
                   for f in sr.score(FakeRecord(price_change_h24=340.0))["flags"])
        assert any("already moved" in f
                   for f in sr.score(FakeRecord(price_change_h24=-180.0))["flags"])

    def test_incomplete_data_flagged(self):
        flags = sr.score(FakeRecord(fdv_usd=None, age_hours=None))["flags"]
        assert any("incomplete data" in f for f in flags)

    def test_flags_do_not_by_themselves_reject(self):
        out = sr.score(FakeRecord(pair_count=1, boosted=True,
                                  price_change_h24=250.0, buys_h24=1600,
                                  sells_h24=100))
        assert out["verdict"] == "PASS", out["reasons"]
        assert len(out["flags"]) >= 4


# ─── missing / degenerate data never raises ───────────────────────────────────


class TestMissingDataIsRejectNotCrash:
    def test_all_none_record_rejects(self):
        rec = FakeRecord(liquidity_usd=None, volume_h24_usd=None, fdv_usd=None,
                         market_cap_usd=None, pair_created_ms=None,
                         age_hours=None, price_usd=None, price_change_h24=None,
                         buys_h24=None, sells_h24=None, pair_count=None,
                         boosted=None)
        out = sr.score(rec)
        assert out["verdict"] == "REJECT"
        assert len(out["reasons"]) >= 4
        assert out["cost"]["basis"] == "unknown"

    def test_all_zero_record_rejects(self):
        rec = FakeRecord(liquidity_usd=0.0, volume_h24_usd=0.0, fdv_usd=0.0,
                         market_cap_usd=0.0, age_hours=0.0, price_usd=0.0,
                         price_change_h24=0.0, buys_h24=0, sells_h24=0,
                         pair_count=0)
        out = sr.score(rec)
        assert out["verdict"] == "REJECT"
        assert sr.REASON_LIQ_UNKNOWN in _kinds(out)
        assert sr.REASON_TXNS in _kinds(out)

    def test_bare_object_without_any_attributes_rejects(self):
        class Empty:
            pass
        out = sr.score(Empty())
        assert out["verdict"] == "REJECT"

    def test_plain_dict_record_is_accepted(self):
        out = sr.score({"liquidity_usd": 0.0, "dex": "pumpfun"})
        assert out["verdict"] == "REJECT"

    def test_garbage_field_types_do_not_raise(self):
        rec = FakeRecord(liquidity_usd="not-a-number", volume_h24_usd="",
                         fdv_usd=float("nan"), market_cap_usd=float("inf"),
                         age_hours="soon", buys_h24="many", sells_h24=None)
        out = sr.score(rec)
        assert out["verdict"] == "REJECT"

    def test_non_positive_ticket_falls_back_to_the_default(self):
        out = sr.score(FakeRecord(), ticket_usd=0.0)
        assert out["metrics"]["ticket_usd"] == sr.DEFAULT_TICKET_USD
        assert out["cost"]["basis"] == "cpmm"

    def test_negative_liquidity_rejects(self):
        out = sr.score(FakeRecord(liquidity_usd=-100.0))
        assert out["verdict"] == "REJECT"
        assert sr.REASON_LIQ_UNKNOWN in _kinds(out)


# ─── summarize ────────────────────────────────────────────────────────────────


class TestSummarize:
    def test_counts_and_ranks_reasons(self):
        scored = [
            sr.score(FakeRecord()),                                # PASS
            sr.score(FakeRecord(age_hours=1.0)),                   # age
            sr.score(FakeRecord(age_hours=2.0)),                   # age
            sr.score(FakeRecord(liquidity_usd=1_000.0,             # liq (+cost)
                                volume_h24_usd=2_000.0,
                                fdv_usd=20_000.0)),
        ]
        s = sr.summarize(scored)
        assert s["total"] == 4
        assert s["passed"] == 1
        assert s["rejected"] == 3
        counts = dict(s["top_reasons"])
        assert counts[sr.REASON_AGE] == 2
        assert counts[sr.REASON_LIQUIDITY] == 1
        assert s["top_reasons"][0] == (sr.REASON_AGE, 2)

    def test_top_reasons_sorted_by_count_descending(self):
        s = sr.summarize([sr.score(FakeRecord(age_hours=1.0)) for _ in range(3)]
                         + [sr.score(FakeRecord(buys_h24=1, sells_h24=1))])
        counts = [c for _, c in s["top_reasons"]]
        assert counts == sorted(counts, reverse=True)

    def test_empty_and_none_inputs(self):
        for empty in ([], None):
            s = sr.summarize(empty)
            assert s == {"total": 0, "passed": 0, "rejected": 0,
                         "top_reasons": []}

    def test_one_record_counts_a_reason_kind_only_once(self):
        rec = FakeRecord(liquidity_usd=1_000.0, volume_h24_usd=2_000.0,
                         fdv_usd=20_000.0)
        s = sr.summarize([sr.score(rec)])
        assert all(c == 1 for _, c in s["top_reasons"])

    def test_pump_fun_batch_reports_the_real_killer(self):
        pf = FakeRecord(dex="pumpfun", liquidity_usd=0.0,
                        volume_h24_usd=27_475.0, fdv_usd=13_706.0,
                        age_hours=1.0)
        s = sr.summarize([sr.score(pf) for _ in range(4)]
                         + [sr.score(FakeRecord())])
        assert s["passed"] == 1 and s["rejected"] == 4
        assert dict(s["top_reasons"])[sr.REASON_LIQ_UNKNOWN] == 4


# ─── the module must not read as a recommendation engine ──────────────────────


def test_no_recommendation_language_anywhere_in_output():
    banned = ("buy", "sell signal", "bullish", "bearish", "moon", "gem",
              "opportunity", "recommend", "attractive", "undervalued")
    records = [FakeRecord(), FakeRecord(age_hours=1.0),
               FakeRecord(liquidity_usd=0.0, dex="pumpfun"),
               FakeRecord(boosted=True, pair_count=1)]
    for rec in records:
        out = sr.score(rec)
        blob = " ".join(out["reasons"] + out["flags"]).lower()
        # "buys" as a raw trade count is a fact, not advice; strip it first
        blob = blob.replace("buys", "").replace("buy-side", "")
        for word in banned:
            assert word not in blob, f"{word!r} leaked into output: {blob}"
