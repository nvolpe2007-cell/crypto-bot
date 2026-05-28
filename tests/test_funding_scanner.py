"""
Unit tests for FundingScanner._scan_kraken — the conversion from Kraken's
absolute-USD `fundingRate` field to a fractional rate is subtle (it cost a
debug cycle when I first added it), so this locks it down.

These tests bypass pytest-asyncio entirely (the plugin isn't installed in this
project's env) by driving each coroutine through asyncio.run() in a sync test.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from arbitrage.funding_scanner import FundingScanner, MIN_SHOW_APY


# ── tiny aiohttp.ClientSession stand-in ──────────────────────────────────────
# Replicates just the `async with session.get(url) as resp: resp.json()` shape
# used inside the scanner. Avoids mocking aiohttp internals.

class _MockResponse:
    def __init__(self, data: Dict[str, Any], status: int = 200):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self._data


class _MockSession:
    def __init__(self, response_data: Dict[str, Any], status: int = 200):
        self._data = response_data
        self._status = status

    def get(self, *args, **kwargs):
        return _MockResponse(self._data, self._status)


def _scanner_with(data: Dict[str, Any]) -> FundingScanner:
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    return s


def _run_scan(s: FundingScanner):
    return asyncio.run(s._scan_kraken())


def _ticker(symbol: str, funding_rate: float, mark_price: float, **overrides):
    """Build a minimal ticker dict shaped like Kraken's tickers endpoint."""
    t = {
        "symbol": symbol,
        "fundingRate": funding_rate,
        "markPrice": mark_price,
        "suspended": False,
    }
    t.update(overrides)
    return t


# ── conversion math ──────────────────────────────────────────────────────────

def test_kraken_fundingRate_is_divided_by_markPrice():
    """The scanner's headline bug-trap: `fundingRate` is USD-per-contract-per-hour,
    not a fractional rate. APY must be derived by dividing by markPrice."""
    # Use a small enough rate to actually clear the MIN_SHOW_APY=10% gate.
    # fundingRate/mark = 1.0/73700 = 1.357e-5/hr; APY = 11.89%
    data = {"tickers": [_ticker("PF_XBTUSD", funding_rate=1.0, mark_price=73700.0)]}
    [opp] = _run_scan(_scanner_with(data))

    expected_per_hour = 1.0 / 73700.0
    expected_apy = expected_per_hour * 24 * 365 * 100
    assert opp.symbol == "PF_XBTUSD"
    assert opp.exchange == "Kraken Futures"
    assert abs(opp.apy - round(expected_apy, 2)) < 0.01

    # rate_8h is stored as a PERCENT (matches _scan_binance/_scan_bybit convention)
    expected_rate_8h_pct = expected_per_hour * 8 * 100
    assert abs(opp.rate_8h - round(expected_rate_8h_pct, 4)) < 1e-4


def test_kraken_positive_funding_action_label():
    data = {"tickers": [_ticker("PF_SOLUSD", funding_rate=0.005, mark_price=80.0)]}
    [opp] = _run_scan(_scanner_with(data))
    assert opp.action == "SHORT PERP + LONG SPOT"  # positive → shorts get paid


def test_kraken_negative_funding_action_label():
    data = {"tickers": [_ticker("PF_SOLUSD", funding_rate=-0.005, mark_price=80.0)]}
    [opp] = _run_scan(_scanner_with(data))
    assert opp.action == "LONG PERP + SHORT SPOT"


# ── filters ──────────────────────────────────────────────────────────────────

def test_kraken_skips_suspended_instruments():
    data = {"tickers": [
        _ticker("PF_SOLUSD", funding_rate=0.005, mark_price=80.0, suspended=True),
    ]}
    assert _run_scan(_scanner_with(data)) == []


def test_kraken_skips_non_PF_symbols():
    # Kraken Futures has PI_/FI_ (inverse, dated) and other prefixes — only PF_
    # multi-collateral perps are the ones we'd actually pair with Kraken spot.
    data = {"tickers": [
        _ticker("PI_XBTUSD", funding_rate=0.005, mark_price=73700.0),
        _ticker("FI_ETHUSD_240329", funding_rate=0.005, mark_price=2000.0),
    ]}
    assert _run_scan(_scanner_with(data)) == []


def test_kraken_skips_zero_funding():
    data = {"tickers": [_ticker("PF_BORINGUSD", funding_rate=0.0, mark_price=10.0)]}
    assert _run_scan(_scanner_with(data)) == []


def test_kraken_skips_invalid_markprice():
    """Defensive: a missing/zero mark would cause a ZeroDivisionError."""
    data = {"tickers": [_ticker("PF_BROKENUSD", funding_rate=0.005, mark_price=0.0)]}
    assert _run_scan(_scanner_with(data)) == []


def test_kraken_filters_absurd_per_hour_rate():
    """A per-hour fractional rate > 0.1% (≈876% APY) is almost always a stale-data
    glitch from an illiquid micro-cap, not a real funding rate. Skip."""
    # fundingRate=10, mark=100 → 0.1/hr exactly — should be filtered
    data = {"tickers": [_ticker("PF_GLITCHUSD", funding_rate=15.0, mark_price=100.0)]}
    assert _run_scan(_scanner_with(data)) == []


def test_kraken_filters_below_min_show_apy():
    # Pick a rate that yields <MIN_SHOW_APY (=10%) annualised.
    # rate_per_hour = APY / (24*365*100). For APY=5%: 5.7e-7.
    # fundingRate / mark = 5.7e-7 → fundingRate = 5.7e-7 * 100 = 5.7e-5
    data = {"tickers": [_ticker("PF_QUIETUSD", funding_rate=5.7e-5, mark_price=100.0)]}
    results = _run_scan(_scanner_with(data))
    # Sanity: APY computed would be ~5%, below the 10% MIN_SHOW_APY threshold.
    assert results == [], f"expected filter, got APY {results[0].apy if results else None}"
    # And confirm a higher rate DOES pass.
    # APY = fundingRate/mark × 24 × 365 × 100. For >10% APY: rate/mark > 1.14e-5
    # → with mark=100, need fundingRate > 1.14e-3. Use 2.0e-3 (APY ≈ 17.5%).
    data = {"tickers": [_ticker("PF_LOUDUSD", funding_rate=2.0e-3, mark_price=100.0)]}
    [opp] = _run_scan(_scanner_with(data))
    assert opp.apy > MIN_SHOW_APY


# ── robustness ──────────────────────────────────────────────────────────────

def test_kraken_handles_non_200_response():
    s = FundingScanner()
    s.session = _MockSession({"tickers": [_ticker("PF_BTCUSD", 0.005, 73700.0)]}, status=500)
    assert _run_scan(s) == []


def test_kraken_handles_malformed_funding_field():
    data = {"tickers": [
        {"symbol": "PF_WONKYUSD", "fundingRate": "not-a-number", "markPrice": 100.0},
    ]}
    assert _run_scan(_scanner_with(data)) == []
