"""
Unit tests for src/crypto_vol.py — previously zero dedicated coverage despite
feeding the live position-size multiplier (`vol_monitor.get_size_multiplier`)
in both src/bot.py and src/paper_trading.py.

Bypasses pytest-asyncio (not installed in this project's env in some setups)
by driving coroutines through asyncio.run(), same pattern as
tests/test_funding_scanner.py and tests/test_market_sentiment.py.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Dict, Optional

import pytest

from src.crypto_vol import CryptoVolMonitor, IVSnapshot, _parse_expiry


# ── tiny aiohttp stand-ins (mirrors tests/test_market_sentiment.py's pattern) ──

class _FakeResp:
    def __init__(self, json_data: Any = None, raise_on_enter: Optional[Exception] = None):
        self._json = json_data
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter:
            raise self._raise_on_enter
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self, *args, **kwargs):
        return self._json


class _FakeSession:
    """`responses` maps url -> _FakeResp (or a list, consumed in order)."""

    def __init__(self, responses: Dict[str, Any]):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, url, **kwargs):
        resp = self._responses[url]
        if isinstance(resp, list):
            return resp.pop(0)
        return resp


def _patch_session(monkeypatch, responses: Dict[str, Any]):
    import src.crypto_vol as cv
    monkeypatch.setattr(cv.aiohttp, "ClientSession", lambda **kw: _FakeSession(responses))


def _opt(expiry: str, strike: int, side: str = "C", mark_iv: float = 60.0,
         currency: str = "BTC") -> Dict[str, Any]:
    return {
        "instrument_name": f"{currency}-{expiry}-{strike}-{side}",
        "mark_iv": mark_iv,
    }


def _index_resp(price: float) -> _FakeResp:
    return _FakeResp({"result": {"index_price": price}})


def _options_resp(options: list) -> _FakeResp:
    return _FakeResp({"result": options})


from src.crypto_vol import _DERIBIT_INDEX, _DERIBIT_OPTIONS


# ── _parse_expiry ────────────────────────────────────────────────────────────

class TestParseExpiry:
    def test_two_digit_day(self):
        assert _parse_expiry("29APR26") == date(2026, 4, 29)

    def test_one_digit_day(self):
        assert _parse_expiry("5JAN27") == date(2027, 1, 5)

    def test_unknown_month_returns_none(self):
        assert _parse_expiry("29XXX26") is None

    def test_malformed_token_returns_none(self):
        assert _parse_expiry("not-a-date") is None

    def test_invalid_day_for_month_returns_none(self):
        # Feb 30 doesn't exist
        assert _parse_expiry("30FEB26") is None

    def test_chronological_sort_differs_from_alphabetical(self):
        """Locks down the exact failure mode the bug fix addresses: a plain
        string sort of Deribit expiry tokens orders by day-of-month first,
        not by year/month, so it silently disagrees with calendar order."""
        tokens = ["29APR26", "24JUL26", "25DEC26", "5JAN27", "27FEB26"]
        alphabetical = sorted(tokens)
        chronological = sorted(tokens, key=lambda k: _parse_expiry(k))
        assert alphabetical != chronological
        assert chronological == ["27FEB26", "29APR26", "24JUL26", "25DEC26", "5JAN27"]


# ── IVSnapshot ────────────────────────────────────────────────────────────────

def _snap(**overrides) -> IVSnapshot:
    defaults = dict(
        symbol="BTC", atm_iv=60.0, iv_percentile=50.0, term_structure="FLAT",
        near_iv=60.0, far_iv=58.0, spot_price=65000.0,
    )
    defaults.update(overrides)
    return IVSnapshot(**defaults)


class TestIVSnapshotSignal:
    @pytest.mark.parametrize("pct,expected", [
        (81, "EXTREME"), (100, "EXTREME"),
        (80, "HIGH"), (66, "HIGH"),
        (65, "NORMAL"), (21, "NORMAL"), (20, "NORMAL"),
        (19, "LOW"), (0, "LOW"),
    ])
    def test_boundaries(self, pct, expected):
        assert _snap(iv_percentile=pct).signal == expected


class TestIVSnapshotPositionSizeMultiplier:
    def test_extreme_percentile(self):
        assert _snap(iv_percentile=85).position_size_multiplier == 0.4

    def test_high_percentile(self):
        assert _snap(iv_percentile=70).position_size_multiplier == 0.65

    def test_inverted_term_takes_precedence_over_low_percentile(self):
        """Term-inversion stress overrides the 'calm market, size up' read —
        the order of checks in the source matters here."""
        assert _snap(iv_percentile=10, term_structure="INVERTED").position_size_multiplier == 0.5

    def test_low_percentile_normal_term(self):
        assert _snap(iv_percentile=10, term_structure="NORMAL").position_size_multiplier == 1.2

    def test_mid_percentile_normal_term(self):
        assert _snap(iv_percentile=50, term_structure="FLAT").position_size_multiplier == 1.0


class TestIVSnapshotColorAndDict:
    def test_color_matches_signal(self):
        assert _snap(iv_percentile=85).color() == "#ff1744"
        assert _snap(iv_percentile=70).color() == "#ff9500"
        assert _snap(iv_percentile=10, term_structure="NORMAL").color() == "#00f5a0"
        assert _snap(iv_percentile=50).color() == "#4d9fff"

    def test_to_dict_has_expected_keys_and_rounding(self):
        d = _snap(atm_iv=60.123, near_iv=60.123, far_iv=58.456).to_dict()
        assert d["atm_iv"] == 60.1
        assert d["near_iv"] == 60.1
        assert d["far_iv"] == 58.5
        assert d["symbol"] == "BTC"
        assert d["signal"] == "NORMAL"
        assert "fetched_at" in d and "color" in d and "size_mult" in d


# ── CryptoVolMonitor.get_snapshot / get_size_multiplier ──────────────────────

class TestGetSnapshotAndSizeMultiplier:
    def test_get_snapshot_strips_quote_currency(self):
        mon = CryptoVolMonitor()
        mon._snapshots["BTC"] = _snap()
        assert mon.get_snapshot("BTC/USD") is mon._snapshots["BTC"]

    def test_get_snapshot_missing_returns_none(self):
        mon = CryptoVolMonitor()
        assert mon.get_snapshot("ETH/USD") is None

    def test_get_size_multiplier_no_snapshot_defaults_to_one(self):
        mon = CryptoVolMonitor()
        assert mon.get_size_multiplier("SOL/USD") == 1.0

    def test_get_size_multiplier_uses_snapshot(self):
        mon = CryptoVolMonitor()
        mon._snapshots["BTC"] = _snap(iv_percentile=85)
        assert mon.get_size_multiplier("BTC/USD") == 0.4


# ── CryptoVolMonitor._atm_iv ──────────────────────────────────────────────────

class TestAtmIv:
    def test_picks_call_closest_to_spot(self):
        mon = CryptoVolMonitor()
        options = [
            _opt("29APR26", 50000, mark_iv=55.0),
            _opt("29APR26", 65000, mark_iv=60.0),
            _opt("29APR26", 80000, mark_iv=65.0),
        ]
        assert mon._atm_iv(options, spot=64000.0) == 60.0

    def test_ignores_puts(self):
        mon = CryptoVolMonitor()
        options = [
            _opt("29APR26", 65000, side="P", mark_iv=999.0),
            _opt("29APR26", 70000, side="C", mark_iv=42.0),
        ]
        assert mon._atm_iv(options, spot=65000.0) == 42.0

    def test_ignores_missing_or_zero_mark_iv(self):
        mon = CryptoVolMonitor()
        options = [
            {"instrument_name": "BTC-29APR26-65000-C", "mark_iv": 0.0},
            {"instrument_name": "BTC-29APR26-65500-C"},  # no mark_iv key
            _opt("29APR26", 70000, mark_iv=42.0),
        ]
        assert mon._atm_iv(options, spot=65000.0) == 42.0

    def test_no_valid_calls_returns_none(self):
        mon = CryptoVolMonitor()
        options = [_opt("29APR26", 65000, side="P", mark_iv=42.0)]
        assert mon._atm_iv(options, spot=65000.0) is None

    def test_empty_list_returns_none(self):
        mon = CryptoVolMonitor()
        assert mon._atm_iv([], spot=65000.0) is None


# ── CryptoVolMonitor._fetch_currency (integration via fake aiohttp) ─────────

class TestFetchCurrency:
    def test_picks_chronologically_nearest_two_expiries_not_alphabetical(self, monkeypatch):
        """Regression test for the expiry-sort bug: with these 5 expiries,
        alphabetical sort would pick '24JUL26'/'25DEC26' as near/far, but the
        chronologically nearest two are '27FEB26' (10% IV) and '29APR26' (20%)."""
        options = [
            _opt("27FEB26", 65000, mark_iv=10.0),
            _opt("29APR26", 65000, mark_iv=20.0),
            _opt("24JUL26", 65000, mark_iv=30.0),
            _opt("25DEC26", 65000, mark_iv=40.0),
            _opt("5JAN27",  65000, mark_iv=50.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))

        snap = mon.get_snapshot("BTC/USD")
        assert snap is not None
        assert snap.near_iv == 10.0
        assert snap.far_iv == 20.0
        assert snap.atm_iv == 10.0

    def test_fewer_than_two_expiries_no_snapshot(self, monkeypatch):
        options = [_opt("29APR26", 65000, mark_iv=20.0)]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        assert mon.get_snapshot("BTC/USD") is None

    def test_empty_options_no_snapshot(self, monkeypatch):
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp([]),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        assert mon.get_snapshot("BTC/USD") is None

    def test_near_expiry_with_no_valid_calls_no_snapshot(self, monkeypatch):
        options = [
            _opt("29APR26", 65000, side="P", mark_iv=20.0),  # only a put — no valid call
            _opt("24JUL26", 65000, mark_iv=30.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        assert mon.get_snapshot("BTC/USD") is None

    def test_far_expiry_with_no_valid_calls_falls_back_to_near_and_flat(self, monkeypatch):
        options = [
            _opt("29APR26", 65000, mark_iv=20.0),
            _opt("24JUL26", 65000, side="P", mark_iv=30.0),  # only a put
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        snap = mon.get_snapshot("BTC/USD")
        assert snap is not None
        assert snap.near_iv == 20.0
        assert snap.far_iv == 20.0   # falls back to near_iv per `far_iv if far_iv else near_iv`
        assert snap.term_structure == "FLAT"

    @pytest.mark.parametrize("near,far,expected_term", [
        (60.0, 50.0, "INVERTED"),   # near > far * 1.05
        (40.0, 50.0, "NORMAL"),     # near < far * 0.95
        (49.0, 50.0, "FLAT"),       # within the +/-5% band
    ])
    def test_term_structure_classification(self, monkeypatch, near, far, expected_term):
        options = [
            _opt("27FEB26", 65000, mark_iv=near),
            _opt("29APR26", 65000, mark_iv=far),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        assert mon.get_snapshot("BTC/USD").term_structure == expected_term

    def test_percentile_defaults_to_50_below_five_samples(self, monkeypatch):
        options = [
            _opt("27FEB26", 65000, mark_iv=20.0),
            _opt("29APR26", 65000, mark_iv=20.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        assert mon.get_snapshot("BTC/USD").iv_percentile == 50.0

    def test_percentile_computed_once_five_samples_present(self, monkeypatch):
        mon = CryptoVolMonitor()
        mon._iv_history["BTC"] = [10.0, 20.0, 30.0, 40.0]   # 4 prior samples
        options = [
            _opt("27FEB26", 65000, mark_iv=25.0),
            _opt("29APR26", 65000, mark_iv=25.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        asyncio.run(mon._fetch_currency("BTC"))
        # 2 of the 4 prior samples (10, 20) are <= 25 -> 50th percentile
        assert mon.get_snapshot("BTC/USD").iv_percentile == 50.0

    def test_history_caps_at_96_samples(self, monkeypatch):
        mon = CryptoVolMonitor()
        mon._iv_history["BTC"] = [float(i) for i in range(96)]
        options = [
            _opt("27FEB26", 65000, mark_iv=999.0),
            _opt("29APR26", 65000, mark_iv=999.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        asyncio.run(mon._fetch_currency("BTC"))
        assert len(mon._iv_history["BTC"]) == 96
        assert mon._iv_history["BTC"][0] == 1.0   # oldest (0.0) was dropped
        assert mon._iv_history["BTC"][-1] == 999.0

    def test_network_failure_does_not_raise_and_keeps_no_snapshot(self, monkeypatch):
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX: _FakeResp(raise_on_enter=ConnectionError("boom")),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))  # must not raise
        assert mon.get_snapshot("BTC/USD") is None

    def test_malformed_instrument_names_are_skipped(self, monkeypatch):
        options = [
            {"instrument_name": "", "mark_iv": 20.0},
            {"instrument_name": "BTC-ONLY-TWO", "mark_iv": 20.0},
            _opt("27FEB26", 65000, mark_iv=15.0),
            _opt("29APR26", 65000, mark_iv=25.0),
        ]
        _patch_session(monkeypatch, {
            _DERIBIT_INDEX:   _index_resp(65000.0),
            _DERIBIT_OPTIONS: _options_resp(options),
        })
        mon = CryptoVolMonitor()
        asyncio.run(mon._fetch_currency("BTC"))
        snap = mon.get_snapshot("BTC/USD")
        assert snap is not None
        assert snap.near_iv == 15.0
        assert snap.far_iv == 25.0


# ── CryptoVolMonitor._refresh / to_dict ───────────────────────────────────────

class TestRefreshAndToDict:
    def test_refresh_fetches_both_currencies_and_survives_one_failing(self, monkeypatch):
        btc_options = [
            _opt("27FEB26", 65000, mark_iv=15.0, currency="BTC"),
            _opt("29APR26", 65000, mark_iv=25.0, currency="BTC"),
        ]

        import src.crypto_vol as cv

        async def fake_fetch(self, currency):
            if currency == "BTC":
                self._snapshots["BTC"] = _snap(symbol="BTC")
            else:
                raise RuntimeError("ETH fetch boom")

        monkeypatch.setattr(cv.CryptoVolMonitor, "_fetch_currency", fake_fetch)
        mon = CryptoVolMonitor()
        asyncio.run(mon._refresh())   # must not raise despite ETH failing

        assert mon.get_snapshot("BTC/USD") is not None
        assert mon.get_snapshot("ETH/USD") is None
        assert mon._last_fetch > 0.0

    def test_to_dict_serializes_all_snapshots(self):
        mon = CryptoVolMonitor()
        mon._snapshots["BTC"] = _snap(symbol="BTC")
        mon._snapshots["ETH"] = _snap(symbol="ETH")
        d = mon.to_dict()
        assert set(d.keys()) == {"BTC", "ETH"}
        assert d["BTC"]["symbol"] == "BTC"
