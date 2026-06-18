"""
Unit tests for src/market_sentiment.py — previously zero coverage despite being
a hard gate on new longs (SentimentSnapshot.allows_long) and an input feeding
the probability-gate sentiment edge. Bypasses pytest-asyncio (not installed in
this project's env) by driving coroutines through asyncio.run(), same pattern
as tests/test_funding_scanner.py.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from src.market_sentiment import SentimentMonitor, SentimentSnapshot


# ── tiny aiohttp stand-ins (mirrors tests/test_funding_scanner.py's pattern,
# extended for ClientSession-as-context-manager + .text()) ──────────────────

class _FakeResp:
    def __init__(self, json_data: Any = None, text_data: str = "", status: int = 200,
                 raise_on_enter: Optional[Exception] = None):
        self._json = json_data
        self._text = text_data
        self.status = status
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter:
            raise self._raise_on_enter
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self, *args, **kwargs):
        return self._json

    async def text(self):
        return self._text


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


class _FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, msg, *a, **kw):
        self.messages.append(msg)
        return True


def _patch_session(monkeypatch, responses: Dict[str, Any]):
    import src.market_sentiment as ms
    monkeypatch.setattr(ms.aiohttp, "ClientSession", lambda **kw: _FakeSession(responses))


# ── SentimentSnapshot ─────────────────────────────────────────────────────────

def _snap(**overrides) -> SentimentSnapshot:
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


class TestSentimentSnapshot:
    def test_allows_long_true_above_threshold(self):
        assert _snap(fear_greed_score=25).allows_long is True
        assert _snap(fear_greed_score=99).allows_long is True

    def test_allows_long_false_below_threshold(self):
        assert _snap(fear_greed_score=24).allows_long is False
        assert _snap(fear_greed_score=0).allows_long is False

    def test_altcoin_pressure_threshold(self):
        assert _snap(btc_dominance=60.0).altcoin_pressure is False  # boundary: not > 60
        assert _snap(btc_dominance=60.1).altcoin_pressure is True
        assert _snap(btc_dominance=59.9).altcoin_pressure is False

    def test_high_mempool_threshold(self):
        assert _snap(mempool_tx_count=50_000).high_mempool is False
        assert _snap(mempool_tx_count=50_001).high_mempool is True

    def test_to_dict_contains_derived_fields_and_rounds(self):
        d = _snap(btc_dominance=61.2345, market_cap_change_24h=-1.2399,
                  fear_greed_score=10).to_dict()
        assert d["allows_long"] is False
        assert d["altcoin_pressure"] is True
        assert d["btc_dominance"] == 61.23
        assert d["market_cap_change_24h"] == -1.24
        assert "fetched_at" in d and isinstance(d["fetched_at"], str)

    def test_telegram_summary_blocks_message_when_longs_blocked(self):
        text = _snap(fear_greed_score=10, fear_greed_label="Extreme Fear").telegram_summary()
        assert "no longs right now" in text
        assert "Extreme Fear" in text and "10/100" in text

    def test_telegram_summary_no_block_when_longs_allowed(self):
        text = _snap(fear_greed_score=50, fear_greed_label="Neutral").telegram_summary()
        assert "no longs right now" not in text


# ── SentimentMonitor.allows_long (the live trading gate) ───────────────────────

class TestMonitorAllowsLong:
    def test_no_snapshot_yet_fails_open(self):
        m = SentimentMonitor()
        assert m.get_snapshot() is None
        assert m.allows_long("BTC/USD") is True

    def test_blocks_when_extreme_fear(self):
        m = SentimentMonitor()
        m._fg_score = 10
        m._build_snapshot()
        assert m.allows_long("BTC/USD") is False

    def test_allows_when_fear_greed_neutral(self):
        m = SentimentMonitor()
        m._fg_score = 50
        m._build_snapshot()
        assert m.allows_long("ETH/USD") is True

    def test_altcoin_pressure_warns_but_does_not_block(self):
        m = SentimentMonitor()
        m._fg_score = 50
        m._btc_dom = 70.0
        m._build_snapshot()
        # Soft warning only — altcoin_pressure never gates the trade.
        assert m.allows_long("ETH/USD") is True

    def test_altcoin_pressure_ignored_for_btc_symbol(self):
        m = SentimentMonitor()
        m._fg_score = 50
        m._btc_dom = 70.0
        m._build_snapshot()
        assert m.allows_long("BTC/USD") is True


# ── SentimentMonitor._build_snapshot ────────────────────────────────────────

class TestBuildSnapshot:
    def test_snapshot_reflects_cached_fields(self):
        m = SentimentMonitor()
        m._fg_score, m._fg_label = 33, "Fear"
        m._btc_dom, m._mkt_change = 58.5, 2.1
        m._mempool, m._tx24h = 12345, 678910
        m._build_snapshot()
        s = m.get_snapshot()
        assert s.fear_greed_score == 33
        assert s.fear_greed_label == "Fear"
        assert s.btc_dominance == 58.5
        assert s.market_cap_change_24h == 2.1
        assert s.mempool_tx_count == 12345
        assert s.tx_count_24h == 678910


# ── SentimentMonitor._check_alerts (threshold-crossing Telegram alerts) ────────

class TestCheckAlerts:
    def test_first_call_only_seeds_prev_no_alert(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 10
        m._check_alerts()
        assert notifier.messages == []
        assert m._fg_prev == 10

    def test_no_alert_when_no_threshold_crossed(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 50
        m._check_alerts()  # seed prev=50
        m._fg_score = 55
        m._check_alerts()
        assert notifier.messages == []

    def test_crossing_into_extreme_fear_alerts(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 30
        m._check_alerts()  # seed prev=30
        m._fg_score = 24
        m._check_alerts()
        assert len(notifier.messages) == 1
        assert "Extreme Fear" in notifier.messages[0]

    def test_fear_clearing_alerts(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 20
        m._check_alerts()  # seed prev=20
        m._fg_score = 25
        m._check_alerts()
        assert len(notifier.messages) == 1
        assert "Fear cleared" in notifier.messages[0]

    def test_crossing_into_greed_alerts(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 70
        m._check_alerts()  # seed prev=70
        m._fg_score = 76
        m._check_alerts()
        assert len(notifier.messages) == 1
        assert "very greedy" in notifier.messages[0]

    def test_greed_fading_alerts(self):
        notifier = _FakeNotifier()
        m = SentimentMonitor(notifier=notifier)
        m._fg_score = 80
        m._check_alerts()  # seed prev=80
        m._fg_score = 75
        m._check_alerts()
        assert len(notifier.messages) == 1
        assert "Greed fading" in notifier.messages[0]

    def test_no_notifier_does_not_raise(self):
        m = SentimentMonitor(notifier=None)
        m._fg_score = 30
        m._check_alerts()
        m._fg_score = 10
        m._check_alerts()  # would alert if there were a notifier — must not raise


# ── SentimentMonitor._tick (per-source refresh cadence) ───────────────────────

class TestTickRefreshCadence:
    def test_skips_all_fetches_when_nothing_due(self, monkeypatch):
        m = SentimentMonitor()
        calls = {"fg": 0, "cg": 0, "bc": 0}

        async def _fg():
            calls["fg"] += 1

        async def _cg():
            calls["cg"] += 1

        async def _bc():
            calls["bc"] += 1

        monkeypatch.setattr(m, "_fetch_fear_greed", _fg)
        monkeypatch.setattr(m, "_fetch_coingecko", _cg)
        monkeypatch.setattr(m, "_fetch_blockchain", _bc)

        import time
        now = time.monotonic()
        m._t_fg = m._t_cg = m._t_bc = now  # all just refreshed

        asyncio.run(m._tick())
        assert calls == {"fg": 0, "cg": 0, "bc": 0}

    def test_fetches_only_the_source_past_its_refresh_window(self, monkeypatch):
        m = SentimentMonitor()
        calls = {"fg": 0, "cg": 0, "bc": 0}

        async def _fg():
            calls["fg"] += 1

        async def _cg():
            calls["cg"] += 1

        async def _bc():
            calls["bc"] += 1

        monkeypatch.setattr(m, "_fetch_fear_greed", _fg)
        monkeypatch.setattr(m, "_fetch_coingecko", _cg)
        monkeypatch.setattr(m, "_fetch_blockchain", _bc)

        import time
        now = time.monotonic()
        m._t_fg = now            # fresh — not due
        m._t_cg = now            # fresh — not due
        m._t_bc = now - 61       # stale — due (refresh window 60s)

        asyncio.run(m._tick())
        assert calls == {"fg": 0, "cg": 0, "bc": 1}

    def test_rebuilds_snapshot_and_checks_alerts_when_any_fetch_runs(self, monkeypatch):
        m = SentimentMonitor()

        async def _noop():
            pass

        monkeypatch.setattr(m, "_fetch_fear_greed", _noop)
        monkeypatch.setattr(m, "_fetch_coingecko", _noop)
        monkeypatch.setattr(m, "_fetch_blockchain", _noop)

        built = {"n": 0}
        alerted = {"n": 0}
        monkeypatch.setattr(m, "_build_snapshot", lambda: built.__setitem__("n", built["n"] + 1))
        monkeypatch.setattr(m, "_check_alerts", lambda: alerted.__setitem__("n", alerted["n"] + 1))

        m._t_fg = m._t_cg = m._t_bc = 0.0  # all due
        asyncio.run(m._tick())
        assert built["n"] == 1
        assert alerted["n"] == 1


# ── Async fetch methods (real aiohttp call sites, faked) ───────────────────────

class TestFetchFearGreed:
    def test_success_updates_score_and_label(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://api.alternative.me/fng/?limit=1": _FakeResp(
                json_data={"data": [{"value": "17", "value_classification": "Extreme Fear"}]}
            ),
        })
        m = SentimentMonitor()
        asyncio.run(m._fetch_fear_greed())
        assert m._fg_score == 17
        assert m._fg_label == "Extreme Fear"
        assert m._t_fg > 0.0

    def test_malformed_response_leaves_prior_state_and_does_not_raise(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://api.alternative.me/fng/?limit=1": _FakeResp(json_data={"unexpected": "shape"}),
        })
        m = SentimentMonitor()
        m._fg_score, m._fg_label = 50, "Neutral"
        asyncio.run(m._fetch_fear_greed())  # KeyError inside is caught
        assert m._fg_score == 50
        assert m._fg_label == "Neutral"
        assert m._t_fg == 0.0  # never advanced — will retry next tick


class TestFetchCoingecko:
    def test_success_updates_dominance_and_market_change(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://api.coingecko.com/api/v3/global": _FakeResp(json_data={
                "data": {
                    "market_cap_percentage": {"btc": 61.4, "eth": 12.0},
                    "market_cap_change_percentage_24h_usd": -3.25,
                }
            }),
        })
        m = SentimentMonitor()
        asyncio.run(m._fetch_coingecko())
        assert m._btc_dom == 61.4
        assert m._mkt_change == -3.25
        assert m._t_cg > 0.0

    def test_missing_btc_key_defaults_to_50(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://api.coingecko.com/api/v3/global": _FakeResp(json_data={
                "data": {"market_cap_percentage": {}, "market_cap_change_percentage_24h_usd": 0.0}
            }),
        })
        m = SentimentMonitor()
        asyncio.run(m._fetch_coingecko())
        assert m._btc_dom == 50.0

    def test_request_failure_does_not_raise(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://api.coingecko.com/api/v3/global": _FakeResp(raise_on_enter=ConnectionError("boom")),
        })
        m = SentimentMonitor()
        m._btc_dom = 55.0
        asyncio.run(m._fetch_coingecko())  # must swallow, not raise
        assert m._btc_dom == 55.0  # unchanged
        assert m._t_cg == 0.0


class TestFetchBlockchain:
    def test_success_updates_mempool_and_tx24h(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://blockchain.info/q/unconfirmedcount": _FakeResp(text_data=" 4321 \n"),
            "https://blockchain.info/q/24hrtransactioncount": _FakeResp(text_data="555000"),
        })
        m = SentimentMonitor()
        asyncio.run(m._fetch_blockchain())
        assert m._mempool == 4321
        assert m._tx24h == 555000
        assert m._t_bc > 0.0

    def test_non_numeric_response_does_not_raise(self, monkeypatch):
        _patch_session(monkeypatch, {
            "https://blockchain.info/q/unconfirmedcount": _FakeResp(text_data="not-a-number"),
            "https://blockchain.info/q/24hrtransactioncount": _FakeResp(text_data="555000"),
        })
        m = SentimentMonitor()
        m._mempool, m._tx24h = 1, 2
        asyncio.run(m._fetch_blockchain())
        # int() raised on the first call — both fields stay at their prior values
        assert m._mempool == 1
        assert m._tx24h == 2
        assert m._t_bc == 0.0


# ── SentimentMonitor._refresh_all (startup path) ────────────────────────────

class TestRefreshAll:
    def test_runs_all_three_fetches_and_builds_snapshot(self, monkeypatch):
        m = SentimentMonitor()
        calls = []

        async def _fg():
            calls.append("fg")

        async def _cg():
            calls.append("cg")

        async def _bc():
            calls.append("bc")

        monkeypatch.setattr(m, "_fetch_fear_greed", _fg)
        monkeypatch.setattr(m, "_fetch_coingecko", _cg)
        monkeypatch.setattr(m, "_fetch_blockchain", _bc)

        asyncio.run(m._refresh_all())
        assert sorted(calls) == ["bc", "cg", "fg"]
        assert m.get_snapshot() is not None

    def test_one_fetch_raising_does_not_stop_the_others(self, monkeypatch):
        m = SentimentMonitor()
        calls = []

        async def _fg():
            raise RuntimeError("network blip")

        async def _cg():
            calls.append("cg")

        async def _bc():
            calls.append("bc")

        monkeypatch.setattr(m, "_fetch_fear_greed", _fg)
        monkeypatch.setattr(m, "_fetch_coingecko", _cg)
        monkeypatch.setattr(m, "_fetch_blockchain", _bc)

        asyncio.run(m._refresh_all())  # gather(..., return_exceptions=True) absorbs it
        assert sorted(calls) == ["bc", "cg"]
        assert m.get_snapshot() is not None
