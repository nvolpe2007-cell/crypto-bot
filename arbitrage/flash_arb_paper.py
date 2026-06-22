#!/usr/bin/env python3
"""Flash-loan arbitrage — PAPER simulator (no wallet, no money, no contracts).

The honest, safe version of the "flash-loan MEV bot" the YouTube videos sell. It
borrows PAPER capital (a simulated flash loan), looks for a price difference for
the same token across venues, and simulates the atomic arb with a FULL, honest
cost model — then keeps only the paper profit. It never touches a wallet, a
private key, a real contract, or a real dollar.

WHY THIS IS THE RIGHT FIRST STEP (and what it will teach you):
A flash loan does NOT create profit — it only lends you capital for one atomic
transaction. The edge still has to exist AND survive costs. This sim charges the
real ones:
  • flash-loan fee   (Aave-style, ~0.09% of the borrowed notional)
  • 2 swap legs      (buy on venue A + sell on venue B; ~0.3% each on a DEX)
  • slippage         (price impact, modeled from the size vs the quote)
  • gas              (per chain — Ethereum is brutal, L2s are cheap)
You only "submit" when the spread clears ALL of that. This is the OPTIMISTIC
upper bound (it assumes you win every race and never get front-run); if even this
bound rarely profits, the real thing — competing with pro MEV firms — profits
less. That is the lesson, delivered at zero risk.

Real prices come from public CEX tickers (keyless ccxt: Kraken/Coinbase/Bitstamp)
as the data-available proxy for cross-venue spreads; the flash-loan + gas
mechanics are modeled on top. Writes data/flash_arb_state.json in the standard
arm shape, so the dashboard, allocator, and proof_scorecard judge it like any
other forward arm — on the same honest bar.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STARTING_EQUITY = float(os.getenv("FLASH_ARB_START", "1000"))
NOTIONAL = float(os.getenv("FLASH_ARB_NOTIONAL", "10000"))     # paper flash-loan size per arb
FLASHLOAN_FEE_FRAC = float(os.getenv("FLASH_ARB_FLASHLOAN_FEE", "0.0009"))  # Aave 0.09%
SWAP_FEE_FRAC = float(os.getenv("FLASH_ARB_SWAP_FEE", "0.003"))  # 0.30% per leg (Uniswap v2-style)
SLIPPAGE_FRAC = float(os.getenv("FLASH_ARB_SLIPPAGE", "0.0010"))  # 0.10% modeled price impact/leg

# Gas per atomic arb tx, by chain. Ethereum mainnet is the video's focus and is
# deliberately punishing; L2s are cheap. Override with FLASH_ARB_GAS_USD.
_CHAIN_GAS = {"ethereum": 8.0, "base": 0.05, "arbitrum": 0.15, "polygon": 0.02, "optimism": 0.05}
CHAIN = os.getenv("FLASH_ARB_CHAIN", "ethereum").lower()
GAS_USD = float(os.getenv("FLASH_ARB_GAS_USD", str(_CHAIN_GAS.get(CHAIN, 8.0))))

VENUES = [v.strip() for v in os.getenv("FLASH_ARB_VENUES", "kraken,coinbase,bitstamp").split(",") if v.strip()]
TOKENS = [t.strip() for t in os.getenv(
    "FLASH_ARB_TOKENS", "BTC/USD,ETH/USD,SOL/USD,LTC/USD,XRP/USD,ADA/USD,LINK/USD,DOGE/USD").split(",") if t.strip()]

STATE_PATH = Path(os.getenv("DASHBOARD_DATA_DIR", str(Path(__file__).parent.parent / "data"))) / "flash_arb_state.json"


@dataclass
class Opportunity:
    token: str
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    gross_pct: float        # raw spread before costs
    notional: float
    gross_usd: float        # gross_pct * notional
    flashloan_fee: float
    swap_fees: float
    slippage: float
    gas: float
    net_usd: float          # gross minus ALL costs
    needed_pct: float       # spread required just to break even

    @property
    def profitable(self) -> bool:
        return self.net_usd > 0


def evaluate_arb(token: str, quotes: dict[str, dict], *, notional: float = NOTIONAL,
                 flashloan_fee_frac: float = FLASHLOAN_FEE_FRAC, swap_fee_frac: float = SWAP_FEE_FRAC,
                 slippage_frac: float = SLIPPAGE_FRAC, gas_usd: float = GAS_USD) -> Opportunity | None:
    """Best buy-low/sell-high arb for one token across venues, with honest costs.

    `quotes` = {venue: {"ask": float, "bid": float}}. You BUY at the lowest ask and
    SELL at the highest bid. Returns None if fewer than 2 usable venues.
    """
    asks = {v: q["ask"] for v, q in quotes.items() if q.get("ask")}
    bids = {v: q["bid"] for v, q in quotes.items() if q.get("bid")}
    if len(asks) < 1 or len(bids) < 1:
        return None
    buy_venue = min(asks, key=asks.get)
    sell_venue = max(bids, key=bids.get)
    if buy_venue == sell_venue:
        return None
    buy_price, sell_price = asks[buy_venue], bids[sell_venue]
    if buy_price <= 0:
        return None
    gross_pct = (sell_price - buy_price) / buy_price
    gross_usd = gross_pct * notional
    flashloan_fee = flashloan_fee_frac * notional
    swap_fees = 2 * swap_fee_frac * notional          # buy leg + sell leg
    slippage = 2 * slippage_frac * notional           # impact on both legs
    net_usd = gross_usd - flashloan_fee - swap_fees - slippage - gas_usd
    cost_usd = flashloan_fee + swap_fees + slippage + gas_usd
    needed_pct = cost_usd / notional
    return Opportunity(token, buy_venue, sell_venue, buy_price, sell_price, gross_pct,
                       notional, gross_usd, flashloan_fee, swap_fees, slippage, gas_usd,
                       net_usd, needed_pct)


def best_opportunity(quotes_by_token: dict[str, dict], **kw) -> Opportunity | None:
    """Scan all tokens, return the single best (highest net) opportunity — you'd
    execute one flash-loan arb per block."""
    best = None
    for token, quotes in quotes_by_token.items():
        opp = evaluate_arb(token, quotes, **kw)
        if opp and (best is None or opp.net_usd > best.net_usd):
            best = opp
    return best


# ── I/O (kept thin so the logic above stays unit-testable) ──────────────────────
def _fetch_quotes() -> dict[str, dict]:
    """Real bid/ask per token per venue via keyless public ccxt tickers."""
    import ccxt  # noqa: PLC0415

    clients = {}
    for v in VENUES:
        try:
            clients[v] = getattr(ccxt, v)({"enableRateLimit": True})
        except Exception as e:
            print(f"[flash_arb] venue {v} unavailable: {e}")
    out: dict[str, dict] = {}
    for token in TOKENS:
        q = {}
        for v, cli in clients.items():
            try:
                t = cli.fetch_ticker(token)
                if t.get("ask") and t.get("bid"):
                    q[v] = {"ask": float(t["ask"]), "bid": float(t["bid"])}
            except Exception:
                continue  # token not listed on this venue / transient
        if len(q) >= 2:
            out[token] = q
    return out


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            pass
    return {"starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "equity_mtm": STARTING_EQUITY, "started_at": datetime.now(timezone.utc).isoformat(),
            "positions": {}, "closed": [], "equity_curve": []}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def _alert(html: str) -> None:
    if os.getenv("FLASH_ARB_NOTIFY", "0") != "1":
        return
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from src.notifications import create_notifier_from_env
        create_notifier_from_env().send_message(html)
    except Exception as e:
        print(f"[flash_arb] telegram alert skipped: {e}")


def main() -> int:
    state = _load_state()
    quotes = _fetch_quotes()
    if not quotes:
        print("[flash_arb] no quotes (need >=2 venues per token); skipping tick.")
        return 0

    best = best_opportunity(quotes)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    fired = False
    if best is not None:
        state["last_scan"] = {
            "ts": ts, "token": best.token, "buy": best.buy_venue, "sell": best.sell_venue,
            "gross_pct": round(best.gross_pct * 100, 4), "needed_pct": round(best.needed_pct * 100, 4),
            "net_usd": round(best.net_usd, 2),
        }
        if best.profitable:
            # "Submit" the atomic arb — paper. Flash loan borrowed + repaid same tx;
            # only the net profit accrues to the book.
            trade = {
                "pnl": round(best.net_usd, 4), "entry_ts": ts, "exit_ts": ts,
                "token": best.token, "buy_venue": best.buy_venue, "sell_venue": best.sell_venue,
                "gross_pct": round(best.gross_pct * 100, 4), "notional": best.notional,
                "gas": best.gas, "reason": "flash_arb_executed",
            }
            state.setdefault("closed", []).append(trade)
            state["equity"] = round(state.get("equity", STARTING_EQUITY) + best.net_usd, 4)
            fired = True
            _alert(f"⚡ <b>Flash-Arb (paper)</b> {best.token}: buy {best.buy_venue} / sell "
                   f"{best.sell_venue}  spread {best.gross_pct*100:.3f}%  net <b>${best.net_usd:+.2f}</b> "
                   f"on ${best.notional:,.0f}")

    state["equity_mtm"] = state.get("equity", STARTING_EQUITY)  # atomic: nothing held
    state.setdefault("equity_curve", []).append(
        {"ts": now.isoformat(), "equity_mtm": round(state["equity_mtm"], 2)})
    state["equity_curve"] = state["equity_curve"][-500:]
    _save_state(state)

    ls = state.get("last_scan", {})
    eq = state.get("equity", STARTING_EQUITY)
    n = len(state.get("closed", []))
    if ls:
        verdict = "FIRED" if fired else f"skip (need {ls['needed_pct']:.3f}% on {CHAIN} gas ${GAS_USD})"
        print(f"[flash_arb] {now:%Y-%m-%d %H:%M} best={ls['token']} spread={ls['gross_pct']:.3f}% "
              f"net=${ls['net_usd']:+.2f} -> {verdict} | equity=${eq:.2f} trades={n} "
              f"({eq-STARTING_EQUITY:+.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
