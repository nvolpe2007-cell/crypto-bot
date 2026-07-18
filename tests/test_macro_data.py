"""
Unit tests for src/macro_data.py — previously zero coverage despite feeding
the directional sizing path: paper_trading.py reads MacroDataProvider.current()
every signal cycle and probability_gate._contagion_edge()/_gold_edge() branch
on btc_gold_corr_30d / corr_strength / is_inverse_regime.

Bypasses pytest-asyncio (not installed in this project's env in some setups)
by driving coroutines through asyncio.run(), same pattern as
tests/test_crypto_vol.py / tests/test_market_sentiment.py.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd
import pytest

import src.macro_data as macro_data
from src.macro_data import (
    ALT_BETA,
    MacroDataProvider,
    MacroState,
    _compute_state,
    _fetch_coingecko_history,
    _load_cache,
    _save_cache,
    alt_beta,
)


def _price_df(closes: List[float], start: str = "2024-01-01") -> pd.DataFrame:
    """Build a _fetch_coingecko_history-shaped DataFrame: Date index, Close col."""
    dates = pd.date_range(start=start, periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=pd.Index(dates, name="Date"))


def _trending(n: int, start: float, step: float, seed: int = 0) -> List[float]:
    import random
    rng = random.Random(seed)
    out, price = [], start
    for _ in range(n):
        price += step + rng.uniform(-step * 0.1, step * 0.1)
        out.append(price)
    return out


class _FakeResp:
    def __init__(self, json_data=None, raise_for_status: Optional[Exception] = None):
        self._json = json_data
        self._raise = raise_for_status

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._json


# ── _fetch_coingecko_history ─────────────────────────────────────────────────

class TestFetchCoingeckoHistory:
    def test_parses_prices_into_indexed_dataframe(self, monkeypatch):
        payload = {"prices": [
            [1704067200000, 100.0],   # 2024-01-01
            [1704153600000, 101.0],   # 2024-01-02
        ]}
        monkeypatch.setattr(macro_data.requests, "get",
                             lambda *a, **kw: _FakeResp(json_data=payload))
        df = _fetch_coingecko_history("http://fake")
        assert list(df["Close"]) == [100.0, 101.0]
        assert df.index.name == "Date"

    def test_empty_prices_returns_none(self, monkeypatch):
        monkeypatch.setattr(macro_data.requests, "get",
                             lambda *a, **kw: _FakeResp(json_data={"prices": []}))
        assert _fetch_coingecko_history("http://fake") is None

    def test_missing_prices_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(macro_data.requests, "get",
                             lambda *a, **kw: _FakeResp(json_data={}))
        assert _fetch_coingecko_history("http://fake") is None

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            macro_data.requests, "get",
            lambda *a, **kw: _FakeResp(raise_for_status=Exception("HTTP 503")),
        )
        assert _fetch_coingecko_history("http://fake") is None

    def test_network_exception_returns_none(self, monkeypatch):
        def _raise(*a, **kw):
            raise ConnectionError("no route to host")
        monkeypatch.setattr(macro_data.requests, "get", _raise)
        assert _fetch_coingecko_history("http://fake") is None

    def test_duplicate_dates_deduped(self, monkeypatch):
        payload = {"prices": [
            [1704067200000, 100.0],
            [1704067200001, 999.0],   # same calendar day, different ms — kept first
            [1704153600000, 101.0],
        ]}
        monkeypatch.setattr(macro_data.requests, "get",
                             lambda *a, **kw: _FakeResp(json_data=payload))
        df = _fetch_coingecko_history("http://fake")
        assert len(df) == 2


# ── _compute_state ───────────────────────────────────────────────────────────

class TestComputeState:
    def _patch_fetch(self, monkeypatch, gold_df, btc_df):
        def fake(url):
            return gold_df if url == macro_data.GOLD_HIST_URL else btc_df
        monkeypatch.setattr(macro_data, "_fetch_coingecko_history", fake)

    def test_either_fetch_failing_returns_none(self, monkeypatch):
        self._patch_fetch(monkeypatch, None, _price_df([1.0] * 40))
        assert _compute_state() is None
        self._patch_fetch(monkeypatch, _price_df([1.0] * 40), None)
        assert _compute_state() is None

    def test_insufficient_joined_history_returns_none(self, monkeypatch):
        # Only 5 overlapping days of data — below the n>=10 floor.
        self._patch_fetch(monkeypatch, _price_df([1.0] * 5), _price_df([1.0] * 5))
        assert _compute_state() is None

    def test_normal_case_returns_sane_state(self, monkeypatch):
        gold = _trending(40, 2000.0, 1.0, seed=1)
        btc = _trending(40, 50000.0, 50.0, seed=1)   # correlated trend, same seed
        self._patch_fetch(monkeypatch, _price_df(gold), _price_df(btc))

        state = _compute_state()
        assert state is not None
        assert state.sample_size >= 10
        assert state.gold_close == gold[-1]
        assert state.gold_prev_close == gold[-2]
        expected_chg = (gold[-1] / gold[-2] - 1.0) * 100.0
        assert state.gold_change_1d == pytest.approx(expected_chg)
        assert -1.0 <= state.btc_gold_corr_30d <= 1.0
        assert not math.isnan(state.btc_gold_corr_30d)

    def test_short_history_corr60_inherits_corr30(self, monkeypatch):
        # n in [10, 30) — corr_60 has no separate 60-window to compute on, so it
        # falls back to corr_30 rather than silently differing.
        gold = _trending(15, 2000.0, 1.0, seed=2)
        btc = _trending(15, 50000.0, 50.0, seed=2)
        self._patch_fetch(monkeypatch, _price_df(gold), _price_df(btc))

        state = _compute_state()
        assert state is not None
        assert state.btc_gold_corr_60d == state.btc_gold_corr_30d

    def test_zero_variance_window_does_not_leak_nan(self, monkeypatch):
        """Regression: a flat (zero-variance) price feed makes Pearson
        correlation mathematically undefined (NaN). NaN must never reach
        MacroState — every downstream `<`/`>=` comparison (is_inverse_regime,
        the contagion-edge "no active driver" early-return) is silently False
        against NaN, which fails OPEN into "macro driver active" instead of
        the intended "no measurable correlation" no-op.
        """
        gold = [2000.0] * 40                     # flat -> zero-variance returns
        btc = _trending(40, 50000.0, 50.0, seed=3)
        self._patch_fetch(monkeypatch, _price_df(gold), _price_df(btc))

        state = _compute_state()
        assert state is not None
        assert state.btc_gold_corr_30d == 0.0
        assert state.btc_gold_corr_60d == 0.0
        assert not math.isnan(state.btc_gold_corr_30d)
        assert not math.isnan(state.corr_strength)
        assert state.is_inverse_regime is False
        assert state.is_positive_regime is False

    def test_zero_gold_prev_close_guards_division(self, monkeypatch):
        gold = _trending(40, 2000.0, 1.0, seed=4)
        gold[-2] = 0.0   # degenerate feed value right before "yesterday"
        btc = _trending(40, 50000.0, 50.0, seed=4)
        self._patch_fetch(monkeypatch, _price_df(gold), _price_df(btc))

        state = _compute_state()
        assert state is not None
        assert state.gold_change_1d == 0.0


# ── MacroState properties ────────────────────────────────────────────────────

class TestMacroStateProperties:
    def _state(self, corr_30: float) -> MacroState:
        return MacroState(
            ts=0.0, gold_close=2000.0, gold_prev_close=1990.0,
            gold_change_1d=0.5, btc_gold_corr_30d=corr_30,
            btc_gold_corr_60d=corr_30, sample_size=30,
        )

    def test_corr_strength_is_absolute_value(self):
        assert self._state(-0.7).corr_strength == pytest.approx(0.7)
        assert self._state(0.7).corr_strength == pytest.approx(0.7)

    @pytest.mark.parametrize("corr,expected", [(-0.9, True), (-0.5, True), (-0.49, False), (0.6, False)])
    def test_is_inverse_regime_threshold(self, corr, expected):
        assert self._state(corr).is_inverse_regime is expected

    @pytest.mark.parametrize("corr,expected", [(0.9, True), (0.5, True), (0.49, False), (-0.6, False)])
    def test_is_positive_regime_threshold(self, corr, expected):
        assert self._state(corr).is_positive_regime is expected


# ── cache round-trip ──────────────────────────────────────────────────────────

class TestCache:
    def _state(self) -> MacroState:
        return MacroState(
            ts=1700000000.0, gold_close=2050.5, gold_prev_close=2040.0,
            gold_change_1d=0.51, btc_gold_corr_30d=-0.62, btc_gold_corr_60d=-0.4,
            sample_size=30,
        )

    def test_save_then_load_round_trips(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "macro_state.json"
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(macro_data, "DATA_DIR", str(tmp_path))

        state = self._state()
        _save_cache(state)
        assert cache_file.exists()
        loaded = _load_cache()
        assert loaded == state

    def test_load_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(tmp_path / "nope.json"))
        assert _load_cache() is None

    def test_load_corrupt_json_returns_none(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "macro_state.json"
        cache_file.write_text("{not valid json")
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(cache_file))
        assert _load_cache() is None

    def test_load_schema_mismatch_returns_none(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "macro_state.json"
        cache_file.write_text(json.dumps({"ts": 1.0, "unexpected_field": True}))
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(cache_file))
        assert _load_cache() is None

    def test_save_creates_missing_data_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "data"
        monkeypatch.setattr(macro_data, "DATA_DIR", str(nested))
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(nested / "macro_state.json"))
        _save_cache(self._state())
        assert (nested / "macro_state.json").exists()

    def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        # DATA_DIR/CACHE_FILE point at a path that can't be created (parent is
        # a file, not a directory) — _save_cache must swallow the error.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setattr(macro_data, "DATA_DIR", str(blocker))
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(blocker / "macro_state.json"))
        _save_cache(self._state())   # must not raise


# ── MacroDataProvider ─────────────────────────────────────────────────────────

class TestMacroDataProvider:
    def test_init_with_no_cache_has_none_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(tmp_path / "nope.json"))
        provider = MacroDataProvider()
        assert provider.current() is None

    def test_init_loads_existing_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "macro_state.json"
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(macro_data, "DATA_DIR", str(tmp_path))
        state = MacroState(ts=1700000000.0, gold_close=2000.0, gold_prev_close=1995.0,
                            gold_change_1d=0.25, btc_gold_corr_30d=0.3,
                            btc_gold_corr_60d=0.2, sample_size=30)
        _save_cache(state)

        provider = MacroDataProvider()
        assert provider.current() == state

    def test_refresh_loop_populates_state_from_compute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(tmp_path / "nope.json"))
        new_state = MacroState(ts=123.0, gold_close=2100.0, gold_prev_close=2090.0,
                                gold_change_1d=0.48, btc_gold_corr_30d=0.4,
                                btc_gold_corr_60d=0.3, sample_size=30)
        monkeypatch.setattr(macro_data, "_compute_state", lambda: new_state)
        monkeypatch.setattr(macro_data, "_save_cache", lambda s: None)

        provider = MacroDataProvider(refresh_sec=1)

        async def _run_one_tick():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(provider._refresh_loop(), timeout=0.2)

        asyncio.run(_run_one_tick())
        assert provider.current() == new_state

    def test_refresh_loop_survives_compute_state_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(tmp_path / "nope.json"))

        def _boom():
            raise RuntimeError("coingecko down")
        monkeypatch.setattr(macro_data, "_compute_state", _boom)

        provider = MacroDataProvider(refresh_sec=1)

        async def _run_one_tick():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(provider._refresh_loop(), timeout=0.2)

        asyncio.run(_run_one_tick())   # must not propagate the RuntimeError
        assert provider.current() is None

    def test_refresh_loop_skips_fetch_when_cache_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(macro_data, "CACHE_FILE", str(tmp_path / "nope.json"))
        calls = []
        monkeypatch.setattr(macro_data, "_compute_state",
                             lambda: calls.append(1) or None)

        provider = MacroDataProvider(refresh_sec=3600)
        provider._state = MacroState(
            ts=time_now_fresh(), gold_close=1.0, gold_prev_close=1.0,
            gold_change_1d=0.0, btc_gold_corr_30d=0.0, btc_gold_corr_60d=0.0,
            sample_size=10,
        )

        async def _run_one_tick():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(provider._refresh_loop(), timeout=0.2)

        asyncio.run(_run_one_tick())
        assert calls == []   # cache was fresh — _compute_state never called

    def test_start_returns_running_task_and_is_idempotent(self):
        provider = MacroDataProvider()

        async def _drive():
            task1 = provider.start()
            task2 = provider.start()
            assert task1 is task2   # same in-flight task, not duplicated
            task1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task1

        asyncio.run(_drive())

    def test_start_creates_new_task_after_previous_done(self):
        provider = MacroDataProvider()

        async def _drive():
            task1 = provider.start()
            task1.cancel()
            try:
                await task1
            except asyncio.CancelledError:
                pass
            task2 = provider.start()
            assert task2 is not task1
            task2.cancel()
            try:
                await task2
            except asyncio.CancelledError:
                pass

        asyncio.run(_drive())


def time_now_fresh() -> float:
    import time
    return time.time()


# ── alt_beta ──────────────────────────────────────────────────────────────────

class TestAltBeta:
    def test_known_symbols_match_table(self):
        assert alt_beta("BTC/USD") == ALT_BETA["BTC/USD"]
        assert alt_beta("ETH/USD") == ALT_BETA["ETH/USD"]
        assert alt_beta("SOL/USD") == ALT_BETA["SOL/USD"]

    def test_unknown_symbol_defaults_above_one(self):
        assert alt_beta("DOGE/USD") == 1.2
