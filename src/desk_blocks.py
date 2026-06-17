"""
Composable "lego" desk-context blocks for the AI brain.

Each block is a small, INDEPENDENT, FAIL-SAFE provider that returns a namespaced
dict; `build_desk_blocks()` composes the enabled ones into a single bundle that
brain_paper merges into the brain's macro context. They all work together — the
brain reads one `desk_blocks` object — but each can be toggled off via env and
none can break the others (any error → that block is simply absent).

Adding a future block (on-chain, OI/liquidations, news sentiment, a low-fee-venue
feed) = write one fail-safe fetch fn + register it in build_desk_blocks. That's it.

Current blocks:
  • cross_asset  — macro risk backdrop (S&P, dollar, gold, 10Y) via Yahoo daily.
  • flow         — SLOW directional volume per coin (a daily up/down-volume CVD
                   proxy from Kraken's own bars; NOT tick scalping — confirms or
                   diverges from price).
  • risk_budget  — the brain's OWN net/gross exposure + correlation concentration
                   (BTC/ETH/SOL move together → treat them as one bet).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

BLOCK_CROSS_ASSET = os.getenv("BRAIN_BLOCK_CROSS_ASSET", "1") == "1"
BLOCK_FLOW = os.getenv("BRAIN_BLOCK_FLOW", "1") == "1"
BLOCK_RISK = os.getenv("BRAIN_BLOCK_RISK", "1") == "1"

_YH = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=3mo"
_HDR = {"User-Agent": "Mozilla/5.0"}
# (label, yahoo symbol)
_CROSS_ASSETS = [("spx", "%5EGSPC"), ("dxy", "DX-Y.NYB"), ("gold", "GC=F"), ("ust10y", "%5ETNX")]


# ── block 1: cross-asset macro ───────────────────────────────────────────────
def _yahoo_closes(sym: str) -> list[float]:
    req = urllib.request.Request(_YH.format(sym=sym), headers=_HDR)
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return [c for c in q if c is not None]


def _ret_pct(closes: list[float], n: int) -> float | None:
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None


def _risk_label(spx: float | None, dxy: float | None, gold: float | None) -> str:
    """Crypto tracks risk-on. Equities up + dollar flat/down = risk_on; equities
    down + dollar up (flight to safety) = risk_off; otherwise mixed."""
    if spx is None or dxy is None:
        return "unknown"
    if spx > 0 and dxy <= 0:
        return "risk_on"
    if spx < 0 and dxy > 0:
        return "risk_off"
    return "mixed"


def cross_asset_macro() -> dict:
    """20-day cross-asset returns + a risk-on/off read. Fail-safe → {}."""
    try:
        rets = {}
        for label, sym in _CROSS_ASSETS:
            try:
                rets[label] = _ret_pct(_yahoo_closes(sym), 20)
            except Exception:
                rets[label] = None
        if all(v is None for v in rets.values()):
            return {}
        return {
            "spx_20d_pct": rets["spx"], "dxy_20d_pct": rets["dxy"],
            "gold_20d_pct": rets["gold"], "ust10y_20d_pct": rets["ust10y"],
            "regime": _risk_label(rets["spx"], rets["dxy"], rets["gold"]),
            "note": "Crypto tends to track risk-ON. A strong/ rising dollar (dxy up) "
                    "and rising yields are headwinds; risk_off favors caution/FLAT.",
        }
    except Exception as e:
        logger.warning("[desk_blocks] cross_asset failed: %s", e)
        return {}


# ── block 2: slow directional volume (poor-man's CVD) ────────────────────────
def _buy_pressure(closes: list[float], vols: list[float], n: int) -> float | None:
    """Net directional volume over last n bars: (up-day vol − down-day vol) / total,
    in [-1, 1]. A SLOW order-flow proxy — positive = net buying volume."""
    if len(closes) < n + 1 or len(vols) < n + 1:
        return None
    up = dn = 0.0
    for i in range(len(closes) - n, len(closes)):
        v = vols[i]
        if closes[i] > closes[i - 1]:
            up += v
        elif closes[i] < closes[i - 1]:
            dn += v
    tot = up + dn
    return round((up - dn) / tot, 3) if tot > 0 else 0.0


def volume_flow(ohlc_by_coin: dict) -> dict:
    """Per-coin slow buy-pressure (20d/5d) + a divergence read. Fail-safe → {}."""
    out = {}
    try:
        for coin, bars in (ohlc_by_coin or {}).items():
            closes = [float(b["c"]) for b in bars if "c" in b]
            vols = [float(b.get("v", 0.0)) for b in bars]
            bp20 = _buy_pressure(closes, vols, 20)
            bp5 = _buy_pressure(closes, vols, 5)
            if bp20 is None:
                continue
            price_up = len(closes) > 20 and closes[-1] > closes[-21]
            if price_up and bp20 < -0.1:
                div = "bearish_divergence"      # price up on net selling = weak rally
            elif (not price_up) and bp20 > 0.1:
                div = "bullish_divergence"      # price down on net buying = accumulation
            else:
                div = "confirms"
            out[coin] = {"buy_pressure_20d": bp20, "buy_pressure_5d": bp5,
                         "vs_price": div}
        return out
    except Exception as e:
        logger.warning("[desk_blocks] volume_flow failed: %s", e)
        return {}


# ── block 3: portfolio risk budget / correlation concentration ───────────────
def risk_budget(state: dict, prices: dict) -> dict:
    """The brain's own exposure: net (signed) and gross notional, and whether it's
    secretly one big correlated bet (BTC/ETH/SOL co-move). Fail-safe → {}."""
    try:
        positions = (state or {}).get("positions", {}) or {}
        if not positions:
            return {"directional_concentration": "flat",
                    "note": "No open positions — full dry powder."}
        net = gross = 0.0
        sides = set()
        for p in positions.values():
            side = p.get("side", 0)
            size = float(p.get("size_usd", 0.0))
            net += side * size
            gross += size
            if side > 0:
                sides.add("long")
            elif side < 0:
                sides.add("short")
        if sides == {"long"}:
            conc = "all_long"
        elif sides == {"short"}:
            conc = "all_short"
        else:
            conc = "mixed"
        start = float((state or {}).get("starting_equity", 1000.0)) or 1000.0
        return {
            "net_exposure_usd": round(net, 2), "gross_exposure_usd": round(gross, 2),
            "net_exposure_pct_equity": round(net / start * 100, 1),
            "open_positions": len(positions), "directional_concentration": conc,
            "note": "BTC/ETH/SOL are highly correlated — a book that is all_long or "
                    "all_short is really ONE bet at gross size. Size accordingly.",
        }
    except Exception as e:
        logger.warning("[desk_blocks] risk_budget failed: %s", e)
        return {}


# ── compose ──────────────────────────────────────────────────────────────────
def build_desk_blocks(ohlc_by_coin: dict | None = None, state: dict | None = None,
                      prices: dict | None = None) -> dict:
    """Run the enabled blocks and merge the non-empty ones into one bundle. Each
    block is independent and fail-safe, so a slow/broken provider just drops out."""
    blocks = {}
    if BLOCK_CROSS_ASSET:
        ca = cross_asset_macro()
        if ca:
            blocks["cross_asset"] = ca
    if BLOCK_FLOW:
        fl = volume_flow(ohlc_by_coin or {})
        if fl:
            blocks["flow"] = fl
    if BLOCK_RISK:
        rb = risk_budget(state or {}, prices or {})
        if rb:
            blocks["risk_budget"] = rb
    return blocks
