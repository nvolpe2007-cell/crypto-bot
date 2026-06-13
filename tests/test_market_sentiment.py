"""
Unit tests for src/market_sentiment.py

SentimentMonitor.allows_long() is wired into the live entry checklist
(paper_trading.py and live_trading.py) as a real veto on new long positions
during "Extreme Fear", and SentimentMonitor._check_alerts() fires Telegram
alerts on Fear/Greed threshold crossings. Both were previously untested.

Covers:
- SentimentSnapshot: allows_long / altcoin_pressure / high_mempool threshold
  properties, to_dict() serialization, telegram_summary() formatting
- SentimentMonitor.allows_long(): no-snapshot fail-open, fear veto,
  altcoin-pressure warning (non-blocking)
- _fetch_fear_greed / _fetch_coingecko / _fetch_blockchain: success parsing,
  network-failure fallback to defaults, malformed-payload handling
- _tick: only refetches data sources whose refresh interval has elapsed
- _check_alerts: all four Fear/Greed threshold-crossing alerts, no-alert
  when no threshold is crossed, first-call baseline, no-notifier safety
- start()/stop(): initial snapshot + Telegram summary, graceful shutdown

A custom mock aiohttp session is used (the global conftest stub's
_FakeSession does not accept the `timeout=` kwarg that this module passes
to aiohttp.ClientSession).
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from src import market_sentiment
from src.market_sentiment import (
    SentimentMonitor,
    SentimentSnapshot,
    _COINGECKO_URL,
    _FEAR_GREED_URL,
    _MEMPOOL_URL,
    _REFRESH_BC,
    _REFRESH_CG,
    _REFRESH_FG,
    _TX24H_URL,
)


# ── mock aiohttp session ────────────────────────────────────────────────────

class _MockResponse:
    """Minimal async-context-manager response with .json() / .text()."""

    def __init__(self, json_data=None, text_data=""):
        self._json_data = json_data
        self._text_data = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, **_kw):
        return self._json_data

    async def text(self):
        return self._text_data


class _RaisingResponse:
    """A response whose __aenter__ raises, simulating a connection error."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _MockSession:
    def __init__(self, get_map):
        self._get_map = get_map

    def get(self, url, *a, **kw):
        return self._get_map[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch, get_map):
    """Replace aiohttp.ClientSession with one returning canned responses by URL."""
    monkeypatch.setattr(
        market_sentiment.aiohttp, "ClientSession",
        lambda *a, **kw: _MockSession(get_map),
    )


def _snapshot(**overrides):
    defaults = dict(
        fear_greed_score=50,
        fear_greed_label="Neutral",
        btc_dominance=50.0,
        market_cap_change_24h=0.0,
        mempool_tx_count=0,
        tx_count_24h=0,
    )
    defaults.update(overrides)
    return SentimentSnapshot(**defaults)


# ── SentimentSnapshot ────────────────────────────────────────────────────────

class TestSentimentSnapshotAllowsLong:
    def test_blocks_below_25(self):
        assert _snapshot(fear_greed_score=24).allows_long is False

    def test_allows_at_25(self):
        assert _snapshot(fear_greed_score=25).allows_long is True

    def test_allows_above_25(self):
        assert _snapshot(fear_greed_score=80).allows_long is True


class TestSentimentSnapshotAltcoinPressure:
    def test_at_60_is_not_pressure(self):
        assert _snapshot(btc_dominance=60.0).altcoin_pressure is False

    def test_above_60_is_pressure(self):
        assert _snapshot(btc_dominance=60.01).altcoin_pressure is True

    def test_below_60_is_not_pressure(self):
        assert _snapshot(btc_dominance=45.0).altcoin_pressure is False


class TestSentimentSnapshotHighMempool:
    def test_at_50000_is_not_high(self):
        assert _snapshot(mempool_tx_count=50_000).high_mempool is False

    def test_above_50000_is_high(self):
        assert _snapshot(mempool_tx_count=50_001).high_mempool is True


class TestSentimentSnapshotToDict:
    def test_rounds_floats_and_includes_flags(self):
        snap = _snapshot(
            fear_greed_score=18,
            fear_greed_label="Extreme Fear",
            btc_dominance=55.5678,
            market_cap_change_24h=-1.2345,
            mempool_tx_count=12_000,
            tx_count_24h=300_000,
        )
        d = snap.to_dict()
        assert d["fear_greed_score"] == 18
        assert d["fear_greed_label"] == "Extreme Fear"
        assert d["btc_dominance"] == round(55.5678, 2)
        assert d["market_cap_change_24h"] == round(-1.2345, 2)
        assert d["mempool_tx_count"] == 12_000
        assert d["tx_count_24h"] == 300_000
        assert d["allows_long"] is False
        assert d["altcoin_pressure"] is False
        assert "fetched_at" in d and isinstance(d["fetched_at"], str)


class TestSentimentSnapshotTelegramSummary:
    def test_includes_block_message_during_fear(self):
        snap = _snapshot(fear_greed_score=18, fear_greed_label="Extreme Fear")
        msg = snap.telegram_summary()
        assert "no longs" in msg
        assert "Extreme Fear" in msg
        assert "18/100" in msg

    def test_no_block_message_when_neutral(self):
        snap = _snapshot(fear_greed_score=50, fear_greed_label="Neutral")
        msg = snap.telegram_summary()
        assert "no longs" not in msg
        assert "Neutral" in msg


# ── SentimentMonitor.allows_long ─────────────────────────────────────────────

class TestMonitorAllowsLong:
    def test_true_when_no_snapshot_yet(self):
        mon = SentimentMonitor()
        assert mon.get_snapshot() is None
        assert mon.allows_long("BTC/USD") is True

    def test_false_during_extreme_fear(self):
        mon = SentimentMonitor()
        mon._fg_score = 18
        mon._fg_label = "Extreme Fear"
        mon._build_snapshot()
        assert mon.allows_long("ETH/USD") is False

    def test_true_above_fear_threshold(self):
        mon = SentimentMonitor()
        mon._fg_score = 40
        mon._build_snapshot()
        assert mon.allows_long("ETH/USD") is True

    def test_altcoin_pressure_does_not_block(self):
        """High BTC dominance only logs a warning — it does not veto entries."""
        mon = SentimentMonitor()
        mon._fg_score = 50
        mon._btc_dom = 65.0
        mon._build_snapshot()
        assert mon.allows_long("ETH/USD") is True

    def test_altcoin_pressure_check_does_not_crash_without_symbol(self):
        mon = SentimentMonitor()
        mon._fg_score = 50
        mon._btc_dom = 65.0
        mon._build_snapshot()
        assert mon.allows_long() is True


# ── fetch: Fear & Greed ──────────────────────────────────────────────────────

class TestFetchFearGreed:
    async def test_success_updates_score_and_label(self, monkeypatch):
        resp = _MockResponse(json_data={
            "data": [{"value": "18", "value_classification": "Extreme Fear"}]
        })
        _patch_session(monkeypatch, {_FEAR_GREED_URL: resp})

        mon = SentimentMonitor()
        await mon._fetch_fear_greed()

        assert mon._fg_score == 18
        assert mon._fg_label == "Extreme Fear"
        assert mon._t_fg > 0

    async def test_network_failure_leaves_defaults(self, monkeypatch):
        _patch_session(monkeypatch, {_FEAR_GREED_URL: _RaisingResponse(RuntimeError("boom"))})

        mon = SentimentMonitor()
        await mon._fetch_fear_greed()

        assert mon._fg_score == 50
        assert mon._fg_label == "Neutral"
        assert mon._t_fg == 0.0

    async def test_malformed_payload_does_not_crash(self, monkeypatch):
        resp = _MockResponse(json_data={"unexpected": "shape"})
        _patch_session(monkeypatch, {_FEAR_GREED_URL: resp})

        mon = SentimentMonitor()
        await mon._fetch_fear_greed()  # KeyError must be caught internally

        assert mon._fg_score == 50
        assert mon._t_fg == 0.0


# ── fetch: CoinGecko ─────────────────────────────────────────────────────────

class TestFetchCoinGecko:
    async def test_success_updates_dominance_and_change(self, monkeypatch):
        resp = _MockResponse(json_data={"data": {
            "market_cap_percentage": {"btc": 55.5, "eth": 15.0},
            "market_cap_change_percentage_24h_usd": -2.34,
        }})
        _patch_session(monkeypatch, {_COINGECKO_URL: resp})

        mon = SentimentMonitor()
        await mon._fetch_coingecko()

        assert mon._btc_dom == 55.5
        assert mon._mkt_change == -2.34
        assert mon._t_cg > 0

    async def test_missing_btc_key_defaults_to_50(self, monkeypatch):
        resp = _MockResponse(json_data={"data": {
            "market_cap_percentage": {"eth": 15.0},
        }})
        _patch_session(monkeypatch, {_COINGECKO_URL: resp})

        mon = SentimentMonitor()
        await mon._fetch_coingecko()

        assert mon._btc_dom == 50.0
        assert mon._mkt_change == 0.0

    async def test_network_failure_leaves_defaults(self, monkeypatch):
        _patch_session(monkeypatch, {_COINGECKO_URL: _RaisingResponse(RuntimeError("down"))})

        mon = SentimentMonitor()
        await mon._fetch_coingecko()

        assert mon._btc_dom == 50.0
        assert mon._t_cg == 0.0


# ── fetch: blockchain.info ───────────────────────────────────────────────────

class TestFetchBlockchain:
    async def test_success_updates_mempool_and_tx24h(self, monkeypatch):
        _patch_session(monkeypatch, {
            _MEMPOOL_URL: _MockResponse(text_data="  12345\n"),
            _TX24H_URL: _MockResponse(text_data="678901"),
        })

        mon = SentimentMonitor()
        await mon._fetch_blockchain()

        assert mon._mempool == 12345
        assert mon._tx24h == 678901
        assert mon._t_bc > 0

    async def test_network_failure_leaves_defaults(self, monkeypatch):
        _patch_session(monkeypatch, {
            _MEMPOOL_URL: _RaisingResponse(RuntimeError("down")),
            _TX24H_URL: _MockResponse(text_data="678901"),
        })

        mon = SentimentMonitor()
        await mon._fetch_blockchain()

        assert mon._mempool == 0
        assert mon._tx24h == 0
        assert mon._t_bc == 0.0

    async def test_non_numeric_response_does_not_crash(self, monkeypatch):
        _patch_session(monkeypatch, {
            _MEMPOOL_URL: _MockResponse(text_data="not-a-number"),
            _TX24H_URL: _MockResponse(text_data="678901"),
        })

        mon = SentimentMonitor()
        await mon._fetch_blockchain()

        assert mon._mempool == 0
        assert mon._tx24h == 0
        assert mon._t_bc == 0.0


# ── _tick ────────────────────────────────────────────────────────────────────

class TestTick:
    async def test_only_refreshes_expired_sources(self, monkeypatch):
        mon = SentimentMonitor()
        now = time.monotonic()
        mon._t_fg = now - _REFRESH_FG - 1   # expired
        mon._t_cg = now                      # fresh
        mon._t_bc = now                      # fresh

        fg, cg, bc = AsyncMock(), AsyncMock(), AsyncMock()
        monkeypatch.setattr(mon, "_fetch_fear_greed", fg)
        monkeypatch.setattr(mon, "_fetch_coingecko", cg)
        monkeypatch.setattr(mon, "_fetch_blockchain", bc)

        await mon._tick()

        fg.assert_called_once()
        cg.assert_not_called()
        bc.assert_not_called()
        assert mon._snapshot is not None  # rebuilt because something refreshed

    async def test_nothing_due_skips_fetch_and_rebuild(self, monkeypatch):
        mon = SentimentMonitor()
        now = time.monotonic()
        mon._t_fg = now
        mon._t_cg = now
        mon._t_bc = now

        fg, cg, bc = AsyncMock(), AsyncMock(), AsyncMock()
        monkeypatch.setattr(mon, "_fetch_fear_greed", fg)
        monkeypatch.setattr(mon, "_fetch_coingecko", cg)
        monkeypatch.setattr(mon, "_fetch_blockchain", bc)

        await mon._tick()

        fg.assert_not_called()
        cg.assert_not_called()
        bc.assert_not_called()
        assert mon._snapshot is None

    async def test_all_expired_refreshes_all_three(self, monkeypatch):
        mon = SentimentMonitor()
        now = time.monotonic()
        mon._t_fg = now - _REFRESH_FG - 1
        mon._t_cg = now - _REFRESH_CG - 1
        mon._t_bc = now - _REFRESH_BC - 1

        fg, cg, bc = AsyncMock(), AsyncMock(), AsyncMock()
        monkeypatch.setattr(mon, "_fetch_fear_greed", fg)
        monkeypatch.setattr(mon, "_fetch_coingecko", cg)
        monkeypatch.setattr(mon, "_fetch_blockchain", bc)

        await mon._tick()

        fg.assert_called_once()
        cg.assert_called_once()
        bc.assert_called_once()


# ── _check_alerts ────────────────────────────────────────────────────────────

class TestCheckAlerts:
    def test_first_call_sets_baseline_without_alert(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_score = 18

        mon._check_alerts()

        assert mon._fg_prev == 18
        notifier.send_message.assert_not_called()

    def test_extreme_fear_crossing_alerts(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_prev = 30
        mon._fg_score = 20

        mon._check_alerts()

        msg = notifier.send_message.call_args[0][0]
        assert "Extreme Fear" in msg
        assert mon._fg_prev == 20

    def test_fear_cleared_crossing_alerts(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_prev = 20
        mon._fg_score = 30

        mon._check_alerts()

        msg = notifier.send_message.call_args[0][0]
        assert "Fear cleared" in msg

    def test_very_greedy_crossing_alerts(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_prev = 70
        mon._fg_score = 80

        mon._check_alerts()

        msg = notifier.send_message.call_args[0][0]
        assert "very greedy" in msg

    def test_greed_fading_crossing_alerts(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_prev = 80
        mon._fg_score = 70

        mon._check_alerts()

        msg = notifier.send_message.call_args[0][0]
        assert "Greed fading" in msg

    def test_no_alert_when_no_threshold_crossed(self):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)
        mon._fg_prev = 50
        mon._fg_score = 55

        mon._check_alerts()

        notifier.send_message.assert_not_called()
        assert mon._fg_prev == 55

    def test_no_notifier_does_not_crash_and_still_updates_prev(self):
        mon = SentimentMonitor(notifier=None)
        mon._fg_prev = 30
        mon._fg_score = 20

        mon._check_alerts()  # must not raise

        assert mon._fg_prev == 20


# ── start() / stop() ─────────────────────────────────────────────────────────

class TestStartStop:
    async def test_start_sends_initial_telegram_summary_then_stops(self, monkeypatch):
        notifier = MagicMock()
        mon = SentimentMonitor(notifier=notifier)

        async def fake_refresh_all():
            mon._fg_score = 18
            mon._fg_label = "Extreme Fear"
            mon._build_snapshot()

        async def fake_tick():
            mon._running = False  # stop after one loop iteration

        monkeypatch.setattr(mon, "_refresh_all", fake_refresh_all)
        monkeypatch.setattr(mon, "_tick", fake_tick)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

        await mon.start()

        notifier.send_message.assert_called_once()
        msg = notifier.send_message.call_args[0][0]
        assert "Extreme Fear" in msg
        assert "no longs" in msg

    async def test_start_without_notifier_does_not_crash(self, monkeypatch):
        mon = SentimentMonitor(notifier=None)

        async def fake_refresh_all():
            mon._build_snapshot()

        async def fake_tick():
            mon._running = False

        monkeypatch.setattr(mon, "_refresh_all", fake_refresh_all)
        monkeypatch.setattr(mon, "_tick", fake_tick)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

        await mon.start()  # must not raise

    def test_stop_sets_running_false(self):
        mon = SentimentMonitor()
        mon._running = True

        mon.stop()

        assert mon._running is False
