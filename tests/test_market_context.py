"""Tests for src/market_context.py — the desk-context reader feeding the brain."""
import json
import os
import time

from src import market_context as mc


def _write_state(tmp_path, payload):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    (d / "state.json").write_text(json.dumps(payload))
    return str(d)


def test_distils_macro_regime_iv_funding(tmp_path):
    data_dir = _write_state(tmp_path, {
        "last_update": "2026-06-15T22:00:00",
        "sentiment": {"fear_greed_score": 20, "fear_greed_label": "Extreme Fear",
                      "btc_dominance": 56.44, "market_cap_change_24h": 1.53,
                      "altcoin_pressure": False},
        "regime_all": {"BTC/USD": {"regime": "RANGING", "confidence": 0.5, "adx": 20.9,
                                   "rsi": 44.1, "atr_pct": 0.085,
                                   "strategy_hint": "RSI mean reversion"}},
        "iv": {"BTC": {"atm_iv": 31.4, "iv_percentile": 50.0, "term_structure": "FLAT",
                       "signal": "NORMAL"}},
        "funding_opportunities": [{"symbol": "PF_XBTUSD", "apy": 12.5, "rate_8h": 0.0011,
                                   "action": "SHORT PERP + LONG SPOT"}],
    })
    ctx = mc.load_market_context(["BTC", "ETH"], data_dir=data_dir)
    assert ctx["stale"] is False
    assert ctx["macro"]["fear_greed"] == 20 and ctx["macro"]["btc_dominance_pct"] == 56.44
    btc = ctx["per_coin"]["BTC"]
    assert btc["regime"] == "RANGING" and btc["adx"] == 20.9 and btc["iv_atm"] == 31.4
    assert btc["funding"]["apy"] == 12.5
    # ETH had no regime/iv/funding → fields present but None, no crash
    assert ctx["per_coin"]["ETH"]["regime"] is None and ctx["per_coin"]["ETH"]["funding"] is None


def test_missing_state_is_failsafe(tmp_path):
    ctx = mc.load_market_context(["BTC"], data_dir=str(tmp_path / "nope"))
    assert ctx["stale"] is True and ctx["macro"] == {} and ctx["per_coin"] == {}


def test_stale_when_old(tmp_path, monkeypatch):
    data_dir = _write_state(tmp_path, {"last_update": "x", "regime_all": {}, "iv": {}})
    # force the file mtime to look old
    old = time.time() - 10_000
    os.utime(os.path.join(data_dir, "state.json"), (old, old))
    ctx = mc.load_market_context(["BTC"], data_dir=data_dir, max_age_sec=1800)
    assert ctx["stale"] is True and ctx["age_sec"] >= 1800


def test_funding_symbol_mapping(tmp_path):
    data_dir = _write_state(tmp_path, {
        "regime_all": {}, "iv": {},
        "funding_opportunities": [{"symbol": "PF_SOLUSD", "apy": -40.0, "rate_8h": -0.004}],
    })
    ctx = mc.load_market_context(["SOL", "BTC"], data_dir=data_dir)
    assert ctx["per_coin"]["SOL"]["funding"]["apy"] == -40.0
    assert ctx["per_coin"]["BTC"]["funding"] is None        # no PF_XBTUSD entry
