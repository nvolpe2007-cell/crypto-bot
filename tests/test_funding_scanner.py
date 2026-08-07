"""
Unit tests for FundingScanner — _scan_kraken (the conversion from Kraken's
absolute-USD `fundingRate` field to a fractional rate is subtle, it cost a
debug cycle when first added), plus _scan_binance/_scan_bybit and the
top-level _scan() orchestration (sort + majors-preserving top-50 truncation).

These tests bypass pytest-asyncio entirely (the plugin isn't installed in this
project's env) by driving each coroutine through asyncio.run() in a sync test.
"""
from __future__ import annotations

import asyncio
import sys
import types
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


class _MultiMockSession:
    """Dispatches by URL substring — needed to drive _scan(), which fans out
    to all three exchanges in one call and must see distinct per-exchange data."""

    def __init__(self, routes: Dict[str, Dict[str, Any]], status: int = 200):
        self._routes = routes  # url-substring -> json payload
        self._status = status

    def get(self, url, *args, **kwargs):
        for needle, data in self._routes.items():
            if needle in url:
                return _MockResponse(data, self._status)
        return _MockResponse({}, 404)


def _scanner_with(data: Dict[str, Any]) -> FundingScanner:
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    return s


def _run_scan(s: FundingScanner):
    return asyncio.run(s._scan_kraken())


def _run_binance(s: FundingScanner):
    return asyncio.run(s._scan_binance())


def _run_bybit(s: FundingScanner):
    return asyncio.run(s._scan_bybit())


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


# ── _scan_binance ─────────────────────────────────────────────────────────────

def _binance_item(symbol: str, rate, **overrides):
    item = {"symbol": symbol, "lastFundingRate": rate}
    item.update(overrides)
    return item


def test_binance_basic_parse_and_action_labels():
    data = [_binance_item("BTCUSDT", 0.001), _binance_item("ETHUSDT", -0.001)]
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    [pos, neg] = _run_binance(s)
    assert pos.symbol == "BTCUSDT"
    assert pos.exchange == "Binance"
    assert pos.action == "SHORT PERP + LONG SPOT"  # positive → shorts get paid
    assert neg.action == "LONG PERP + SHORT SPOT"


def test_binance_skips_non_usdt_symbols():
    data = [_binance_item("BTCUSD", 0.01), _binance_item("ETHBUSD", 0.01)]
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    assert _run_binance(s) == []


def test_binance_skips_zero_rate():
    s = FundingScanner()
    s.session = _MockSession([_binance_item("BTCUSDT", 0.0)])  # type: ignore[assignment]
    assert _run_binance(s) == []


def test_binance_filters_below_min_show_apy():
    # apy = rate*3*365*100; need apy < MIN_SHOW_APY=10 → rate < 9.13e-5
    s = FundingScanner()
    s.session = _MockSession([_binance_item("QUIETUSDT", 5e-5)])  # type: ignore[assignment]
    assert _run_binance(s) == []


def test_binance_handles_non_200_response():
    s = FundingScanner()
    s.session = _MockSession([_binance_item("BTCUSDT", 0.01)], status=500)  # type: ignore[assignment]
    assert _run_binance(s) == []


def test_binance_malformed_item_does_not_abort_remaining_parse():
    """Regression: premiumIndex returns hundreds of symbols in one response; a
    single bad `lastFundingRate` (e.g. `None` from a degraded upstream entry)
    must not raise out of the loop and silently drop every symbol after it."""
    data = [
        _binance_item("BTCUSDT", 0.001),
        _binance_item("WONKYUSDT", None),
        _binance_item("ETHUSDT", 0.001),
    ]
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    symbols = {r.symbol for r in _run_binance(s)}
    assert symbols == {"BTCUSDT", "ETHUSDT"}


# ── _scan_bybit ───────────────────────────────────────────────────────────────

def _bybit_payload(tickers: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"result": {"list": tickers}}


def _bybit_ticker(symbol: str, rate, **overrides):
    t = {"symbol": symbol, "fundingRate": rate}
    t.update(overrides)
    return t


def test_bybit_basic_parse_and_action_labels():
    data = _bybit_payload([
        _bybit_ticker("BTCUSDT", "0.001"),
        _bybit_ticker("ETHUSDT", "-0.001"),
    ])
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    [pos, neg] = _run_bybit(s)
    assert pos.exchange == "Bybit"
    assert pos.action == "SHORT PERP + LONG SPOT"
    assert neg.action == "LONG PERP + SHORT SPOT"


def test_bybit_skips_non_usdt_symbols():
    s = FundingScanner()
    s.session = _MockSession(_bybit_payload([_bybit_ticker("BTCUSD", "0.01")]))  # type: ignore[assignment]
    assert _run_bybit(s) == []


def test_bybit_skips_zero_rate():
    s = FundingScanner()
    s.session = _MockSession(_bybit_payload([_bybit_ticker("BTCUSDT", "0")]))  # type: ignore[assignment]
    assert _run_bybit(s) == []


def test_bybit_filters_below_min_show_apy():
    s = FundingScanner()
    s.session = _MockSession(_bybit_payload([_bybit_ticker("QUIETUSDT", "5e-5")]))  # type: ignore[assignment]
    assert _run_bybit(s) == []


def test_bybit_handles_non_200_response():
    s = FundingScanner()
    s.session = _MockSession(  # type: ignore[assignment]
        _bybit_payload([_bybit_ticker("BTCUSDT", "0.01")]), status=500
    )
    assert _run_bybit(s) == []


def test_bybit_handles_null_funding_field():
    """Regression: a null `fundingRate` makes `float(None)` raise TypeError, not
    ValueError — the original guard only caught ValueError, so a null field would
    propagate to the outer except and drop every ticker after it for this cycle."""
    data = _bybit_payload([
        _bybit_ticker("BTCUSDT", "0.001"),
        _bybit_ticker("WONKYUSDT", None),
        _bybit_ticker("ETHUSDT", "0.001"),
    ])
    s = FundingScanner()
    s.session = _MockSession(data)  # type: ignore[assignment]
    symbols = {r.symbol for r in _run_bybit(s)}
    assert symbols == {"BTCUSDT", "ETHUSDT"}


# ── _scan() orchestration ─────────────────────────────────────────────────────

def _synthetic_alts(count: int, apy_start: float, apy_step: float):
    """`count` Binance items named ALT000USDT.. with APY descending from
    apy_start by apy_step each, all comfortably clear of MAJOR_SYMBOLS."""
    items = []
    for i in range(count):
        apy = apy_start - i * apy_step
        rate = apy / (3 * 365 * 100)  # invert _scan_binance's apy formula
        items.append(_binance_item(f"ALT{i:03d}USDT", rate))
    return items


def test_scan_sorts_by_abs_apy_and_preserves_majors_through_top50():
    """_scan() fans out to all 3 exchanges, sorts the combined results by
    |APY| descending, then caps at 50 — but liquid majors (per
    funding_arb_paper.MAJOR_SYMBOLS) must survive the cap even at a modest APY,
    backfilling the remaining slots with the highest-|APY| non-majors. Build 2
    majors at 15% APY plus 60 non-major alts spanning 200%→23% APY: with majors
    (2) < 50, others get 50-2=48 slots, so only the top-48 alts by APY survive."""
    major_apy = 15.0
    major_rate = major_apy / (3 * 365 * 100)
    binance_data = (
        [_binance_item("BTCUSDT", major_rate), _binance_item("ETHUSDT", major_rate)]
        + _synthetic_alts(60, apy_start=200.0, apy_step=3.0)
    )

    s = FundingScanner()
    s.session = _MultiMockSession({  # type: ignore[assignment]
        "fapi.binance.com": binance_data,
        "api.bybit.com": _bybit_payload([]),
        "futures.kraken.com": {"tickers": []},
    })
    asyncio.run(s._scan())

    symbols = {o.symbol for o in s.opportunities}
    assert len(s.opportunities) == 50
    assert "BTCUSDT" in symbols
    assert "ETHUSDT" in symbols
    for i in range(48):
        assert f"ALT{i:03d}USDT" in symbols, f"ALT{i:03d}USDT should survive (top-48 by APY)"
    for i in range(48, 60):
        assert f"ALT{i:03d}USDT" not in symbols, f"ALT{i:03d}USDT should be cropped"


def test_scan_falls_back_to_plain_top50_if_majors_import_fails():
    """If the majors-preserving import ever breaks, _scan() must degrade to a
    plain abs(APY) top-50 rather than propagate the exception. Force the import
    inside _scan() to fail by swapping in a dummy module lacking the expected
    names, with no majors in the universe so plain-top-50 and majors-preserving
    behaviour would otherwise be indistinguishable from the test's perspective."""
    binance_data = _synthetic_alts(60, apy_start=200.0, apy_step=3.0)
    s = FundingScanner()
    s.session = _MultiMockSession({  # type: ignore[assignment]
        "fapi.binance.com": binance_data,
        "api.bybit.com": _bybit_payload([]),
        "futures.kraken.com": {"tickers": []},
    })

    dummy = types.ModuleType("arbitrage.funding_arb_paper")  # no MAJOR_SYMBOLS/_base_symbol
    real = sys.modules.get("arbitrage.funding_arb_paper")
    sys.modules["arbitrage.funding_arb_paper"] = dummy
    try:
        asyncio.run(s._scan())
    finally:
        if real is not None:
            sys.modules["arbitrage.funding_arb_paper"] = real
        else:
            del sys.modules["arbitrage.funding_arb_paper"]

    symbols = [o.symbol for o in s.opportunities]
    assert symbols == [f"ALT{i:03d}USDT" for i in range(50)]
