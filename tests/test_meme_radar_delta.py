"""Unit tests for the meme radar's delta triggers and wallet enrichment.

Every failure mode guarded here is SILENT -- the radar keeps running, keeps
logging "PASS but quiet", and simply never fires. Nothing errors, so the only
symptom is an absence of alerts, which is indistinguishable from a genuinely
quiet market. Two such bugs shipped and were caught only by measuring the
enrichment rate on purpose:

  1. the GeckoTerminal network was hardcoded to "solana", so every non-Solana
     candidate silently failed enrichment and could never fire v2 -- the radar
     would have been filtering on "is this token on Solana"
  2. EVM addresses were matched case-sensitively; DexScreener returns them
     checksummed and GeckoTerminal lowercased, so EVM pools failed to match even
     when the fetch succeeded

Covered:

delta v1 (trade counts) -- kept intact for the pre-registered paper test
- fires on liquidity growth and on trade acceleration
- refuses a sell-led move, short windows, and single observations

delta v2 (unique buyers)
- refuses the wash pattern v1 fires on (the entire point of v2)
- never silently falls back to v1 when wallet data is absent
- absolute buyer floor stops "2 buyers became 3" firing on a percentage
- refuses seller-led growth

enrichment
- DexScreener chain ids map to GeckoTerminal network ids
- unmapped chains are skipped, not sent to the wrong network
- addresses are matched case-insensitively across the two services
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import meme_radar as mr  # noqa: E402

M = 60_000


def _polls(n=6, *, liq=50_000, liq_step=0.0, buys0=100, buys_step=5,
           sells0=100, sells_step=5, buyers=None, buyers_step=0,
           sellers=None, sellers_step=0, minutes=10):
    out = []
    for i in range(n):
        p = {
            "ms": i * minutes * M,
            "liq": liq * (1 + liq_step * i),
            "buys": buys0 + buys_step * i,
            "sells": sells0 + sells_step * i,
            "price": 1.0,
        }
        if buyers is not None:
            p["buyers"] = buyers + buyers_step * i
            p["sellers"] = (sellers if sellers is not None else 10) + sellers_step * i
        out.append(p)
    return {"polls": out}


# ── v1: unchanged, guarded so the paper test's baseline cannot drift ──────────


def test_v1_fires_on_liquidity_growth():
    d = mr.evaluate_delta(_polls(liq_step=0.10, buys_step=8, sells_step=2))
    assert d["fired"] is True


def test_v1_fires_on_trade_acceleration():
    polls = {"polls": [{"ms": i * 10 * M, "liq": 50_000,
                        "buys": 100 + (i ** 2) * 20, "sells": 100 + i,
                        "price": 1.0} for i in range(6)]}
    assert mr.evaluate_delta(polls)["fired"] is True


def test_v1_refuses_sell_led_acceleration():
    polls = {"polls": [{"ms": i * 10 * M, "liq": 50_000, "buys": 100 + i,
                        "sells": 100 + (i ** 2) * 20, "price": 1.0}
                       for i in range(6)]}
    d = mr.evaluate_delta(polls)
    assert d["fired"] is False
    assert any("SELL" in w for w in d["why"])


@pytest.mark.parametrize("n", [0, 1])
def test_v1_needs_two_observations(n):
    d = mr.evaluate_delta({"polls": [{"ms": 0, "liq": 1}] * n})
    assert d["fired"] is False


def test_v1_refuses_a_short_window():
    d = mr.evaluate_delta(_polls(n=2, minutes=2, liq_step=1.0))
    assert d["fired"] is False
    assert any("minimum span" in w for w in d["why"])


# ── v2: the reason it exists ─────────────────────────────────────────────────


def test_v2_refuses_the_wash_pattern_that_v1_fires_on():
    """One wallet looping: trades explode, distinct buyers do not move.

    This is the whole justification for v2. If it ever regresses, v2 is just a
    slower v1.
    """
    wash = {"polls": [{"ms": i * 10 * M, "liq": 50_000,
                       "buys": 100 + (i ** 2) * 40, "sells": 100 + i,
                       "buyers": 20, "sellers": 10, "price": 1.0}
                      for i in range(6)]}
    assert mr.evaluate_delta(wash)["fired"] is True, "v1 baseline changed"
    assert mr.evaluate_delta_buyers(wash)["fired"] is False


def test_v2_fires_on_real_distinct_buyer_growth():
    d = mr.evaluate_delta_buyers(
        _polls(buys_step=20, sells_step=2, buyers=20, buyers_step=8,
               sellers=10, sellers_step=1))
    assert d["fired"] is True
    assert "distinct buyers" in d["trigger"]


def test_v2_does_not_fall_back_to_v1_without_wallet_data():
    """Absent wallet data must mean 'do not fire', never 'use trade counts'."""
    accel = {"polls": [{"ms": i * 10 * M, "liq": 50_000,
                        "buys": 100 + (i ** 2) * 30, "sells": 100 + i,
                        "price": 1.0} for i in range(6)]}
    assert mr.evaluate_delta(accel)["fired"] is True
    d = mr.evaluate_delta_buyers(accel)
    assert d["fired"] is False
    assert any("unique-wallet" in w for w in d["why"])


def test_v2_absolute_buyer_floor_beats_the_percentage():
    """2 -> 7 buyers is +250% and still meaningless."""
    d = mr.evaluate_delta_buyers(
        _polls(buyers=2, buyers_step=1, sellers=1, sellers_step=0))
    assert d["fired"] is False
    assert any("distinct buyers" in w and "too few" in w for w in d["why"])


def test_v2_refuses_seller_led_wallet_growth():
    d = mr.evaluate_delta_buyers(
        _polls(buyers=20, buyers_step=2, sellers=10, sellers_step=30))
    assert d["fired"] is False
    assert any("SELLER" in w for w in d["why"])


def test_v2_liquidity_growth_still_qualifies():
    """Liquidity is costly to fake, so it remains an independent trigger."""
    d = mr.evaluate_delta_buyers(
        _polls(liq_step=0.10, buyers=20, buyers_step=1, sellers=10))
    assert d["fired"] is True


# ── enrichment: the two silent bugs ──────────────────────────────────────────


def test_chain_mapping_covers_the_feed_chains():
    """DexScreener ids differ from GeckoTerminal network ids; bsc/base/ethereum
    all appear in the boosted feed and must not be sent to /networks/solana/."""
    for ds, gt in (("solana", "solana"), ("ethereum", "eth"), ("bsc", "bsc"),
                   ("base", "base"), ("polygon", "polygon_pos"),
                   ("avalanche", "avax")):
        assert mr._GT_NETWORK[ds] == gt


def test_unmapped_chain_is_skipped_not_misrouted(monkeypatch):
    """An unknown chain must produce no request at all -- never a lookup under
    the wrong network, which returns an empty set that reads as 'not found'."""
    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("should not have issued a request")

    monkeypatch.setattr(mr.urllib.request, "urlopen", _boom)
    out = mr.fetch_unique_wallets([("robinhood", "0xdeadbeef")])
    assert out == {}
    assert calls == []


def test_enrichment_keys_are_lowercased(monkeypatch):
    """GeckoTerminal lowercases EVM addresses; DexScreener checksums them."""
    checksummed = "0x630B9C39d46314A3268D75Bb25fd79Df4581D1aF"
    lowered = checksummed.lower()

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return b""

    import json as _json

    def _fake_urlopen(req, timeout=None):
        assert "/networks/bsc/" in req.full_url, "wrong network for a bsc pool"
        payload = {"data": [{"attributes": {
            "address": lowered,
            "transactions": {"h24": {"buys": 900, "sells": 800,
                                     "buyers": 484, "sellers": 469}},
        }}]}
        r = _Resp()
        r.read = lambda: _json.dumps(payload).encode()
        return r

    monkeypatch.setattr(mr.json, "load", lambda fh: _json.loads(fh.read()))
    monkeypatch.setattr(mr.urllib.request, "urlopen", _fake_urlopen)

    out = mr.fetch_unique_wallets([("bsc", checksummed)])
    assert out.get(lowered, {}).get("buyers") == 484
    # the caller looks up with .lower(), so the checksummed form must resolve
    assert out.get(checksummed.lower()) is not None


def test_enrichment_groups_by_network(monkeypatch):
    """One call per network represented, not one per token."""
    seen = []

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    import json as _json

    def _fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        r = _Resp()
        r.read = lambda: _json.dumps({"data": []}).encode()
        return r

    monkeypatch.setattr(mr.json, "load", lambda fh: _json.loads(fh.read()))
    monkeypatch.setattr(mr.urllib.request, "urlopen", _fake_urlopen)

    mr.fetch_unique_wallets([("solana", "A"), ("solana", "B"), ("bsc", "0x1")])
    assert len(seen) == 2
    assert any("/networks/solana/" in u and "A,B" in u for u in seen)
    assert any("/networks/bsc/" in u for u in seen)


def test_enrichment_failure_returns_nothing_rather_than_guessing(monkeypatch):
    def _fail(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(mr.urllib.request, "urlopen", _fail)
    assert mr.fetch_unique_wallets([("solana", "A")]) == {}
