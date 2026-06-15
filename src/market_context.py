"""
Market context reader — surfaces the desk-grade signals the main bot already
computes (sentiment, per-symbol regime, implied vol, funding) so the discretionary
brain can reason over the SAME picture the rest of the system sees, instead of a
thin price/SMA snapshot.

The main paper-trading loop writes everything to `data/state.json` every tick. The
brain runs as a SEPARATE process, so we just read that shared artifact — no extra
API calls, no duplication. FAIL-SAFE: missing / stale / malformed state → a context
marked `stale=True` with empty fields; the caller (and the brain's prompt) treats it
as "no context available" rather than crashing.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

# Kraken-futures perp symbol for each base coin, to pull its funding from the
# funding_opportunities scan (which is keyed by exchange perp symbol).
_PERP_SYMBOL = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD", "SOL": "PF_SOLUSD"}

# Beyond this age the main loop has likely stalled or this is a standalone run;
# don't feed the brain numbers that look live but aren't.
DEFAULT_MAX_AGE_SEC = float(os.getenv("MARKET_CONTEXT_MAX_AGE_SEC", "1800"))


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else v


def _funding_for(coin: str, opps: List[dict]) -> Optional[dict]:
    sym = _PERP_SYMBOL.get(coin)
    if not sym or not isinstance(opps, list):
        return None
    for o in opps:
        if isinstance(o, dict) and o.get("symbol") == sym:
            return {"apy": _round(o.get("apy")), "rate_8h": _round(o.get("rate_8h"), 4),
                    "action": o.get("action")}
    return None


def load_market_context(coins, data_dir: str = "data",
                        max_age_sec: float = DEFAULT_MAX_AGE_SEC) -> Dict:
    """Read data/state.json and distil the context relevant to the brain.

    Returns {as_of, stale, age_sec, macro, per_coin{coin->{...}}}. Never raises."""
    path = os.path.join(data_dir, "state.json")
    ctx = {"as_of": None, "stale": True, "age_sec": None, "macro": {}, "per_coin": {}}
    try:
        age = time.time() - os.path.getmtime(path)
        ctx["age_sec"] = round(age)
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return ctx

    ctx["as_of"] = d.get("last_update")
    ctx["stale"] = ctx["age_sec"] is not None and ctx["age_sec"] > max_age_sec

    sent = d.get("sentiment") or {}
    ctx["macro"] = {
        "fear_greed": sent.get("fear_greed_score"),
        "fear_greed_label": sent.get("fear_greed_label"),
        "btc_dominance_pct": _round(sent.get("btc_dominance")),
        "market_cap_change_24h_pct": _round(sent.get("market_cap_change_24h")),
        "altcoin_pressure": sent.get("altcoin_pressure"),
    }

    regime_all = d.get("regime_all") or {}
    iv = d.get("iv") or {}
    opps = d.get("funding_opportunities") or []
    for coin in coins:
        rg = regime_all.get(f"{coin}/USD") or {}
        ivc = iv.get(coin) or {}
        ctx["per_coin"][coin] = {
            "regime": rg.get("regime"),
            "regime_confidence": _round(rg.get("confidence")),
            "adx": _round(rg.get("adx"), 1),
            "rsi": _round(rg.get("rsi"), 1),
            "atr_pct": _round(rg.get("atr_pct"), 3),
            "strategy_hint": rg.get("strategy_hint"),
            "iv_atm": _round(ivc.get("atm_iv"), 1),
            "iv_percentile": _round(ivc.get("iv_percentile")),
            "iv_term": ivc.get("term_structure"),
            "iv_signal": ivc.get("signal"),
            "funding": _funding_for(coin, opps),
        }
    return ctx
