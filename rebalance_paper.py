#!/usr/bin/env python3
"""
Rebalanced-allocation forward paper arm — the ONE positive result of the strategy search.

After ~320 strategies across 9 dimensions cleared ZERO survivors of the Monte-Carlo +
multiple-testing + OOS gauntlet (memory exhaustive_search_320_zero), the only lever with
genuine, robust-OOS positive expectancy was STRUCTURAL, not predictive: a diversified,
periodically-rebalanced allocation (memory rebalance_premium_verdict). Adding uncorrelated
tokenized gold (PAXG, ~0.17 corr to crypto) to an equal-weight crypto basket with a cash
reserve, rebalanced monthly, gave ~half the drawdown of holding crypto and a positive
full-cycle return where buy-and-hold lost — prediction-free and SPOT-EXECUTABLE on Kraken
today (long-only, no leverage, no shorting). This is the honest, deployable product; it is
NOT alpha (it can't profit in a full bear, only lose far less), so the proof bar judges it
like every other arm.

SPEC (all env-tunable):
  * Universe: 10 liquid crypto majors + PAXG gold. Book: own $1,000 paper account.
  * Target: 50% crypto (equal-weight across the 10) / 25% gold / 25% cash.
  * Rebalance every REBALANCE_DAYS (default 30) back to target; drift in between.
  * Cost: 0.26% Kraken spot taker on the TURNOVER at each rebalance.
  * Each rebalance BOOKS the elapsed holding period as one closed record (pnl in $),
    so proof_scorecard's n>=30 / expectancy>0 / t>2 bar can judge it (slow by design —
    a low-turnover strategy accrues ~12 obs/yr; that's the honest timeline).
  * A never-rebalanced buy&hold of the SAME initial target is tracked in parallel so each
    record also carries the rebalancing PREMIUM (strategy return - buyhold return).

FORWARD-ONLY: first run seeds the book (and the benchmark) at today's closed prices and
books nothing. Acts only on newly-closed daily bars (no repaint).

    python rebalance_paper.py        # mark-to-market, rebalance if due, then exit
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = [s.strip().upper() for s in os.getenv(
    "REBALANCE_SYMBOLS", "BTC,ETH,SOL,XRP,ADA,LINK,LTC,BCH,AVAX,DOGE").split(",") if s.strip()]
GOLD = os.getenv("REBALANCE_GOLD", "PAXG").strip().upper()
CRYPTO_FRAC = float(os.getenv("REBALANCE_CRYPTO_FRAC", "0.50"))
GOLD_FRAC = float(os.getenv("REBALANCE_GOLD_FRAC", "0.25"))
CASH_FRAC = max(0.0, 1.0 - CRYPTO_FRAC - GOLD_FRAC)
TAKER = float(os.getenv("REBALANCE_TAKER", "0.0026"))
REBALANCE_DAYS = float(os.getenv("REBALANCE_DAYS", "30"))
START_EQUITY = float(os.getenv("REBALANCE_START_EQUITY", "1000"))
STATE_FILE = Path(os.getenv("REBALANCE_STATE_FILE", "data/rebalance_paper_state.json"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def target_weights() -> dict[str, float]:
    w = {s: CRYPTO_FRAC / len(SYMBOLS) for s in SYMBOLS}
    if GOLD_FRAC > 0:
        w[GOLD] = GOLD_FRAC
    return w


def fetch_prices() -> dict[str, dict]:
    """Latest CLOSED daily close + timestamp per asset (drops the in-progress bar)."""
    import ccxt
    ex = ccxt.kraken({"enableRateLimit": True})
    out: dict[str, dict] = {}
    assets = SYMBOLS + ([GOLD] if GOLD_FRAC > 0 else [])
    for b in assets:
        try:
            o = ex.fetch_ohlcv(f"{b}/USD", timeframe="1d", limit=3)
            if len(o) >= 2:
                closed = o[-2]  # last fully-closed bar
                out[b] = {"px": float(closed[4]), "t": int(closed[0]) // 1000}
        except Exception as e:
            print(f"{b}: fetch failed - {e}")
    return out


def _equity(units: dict, cash: float, px: dict) -> float:
    return cash + sum(u * px[s]["px"] for s, u in units.items() if s in px)


def _seed(px: dict) -> dict:
    w = target_weights()
    units = {s: (START_EQUITY * wt) / px[s]["px"] for s, wt in w.items() if s in px}
    cash = START_EQUITY * CASH_FRAC
    return {"units": units, "cash": cash}


def _rebalance(units: dict, cash: float, px: dict) -> tuple[dict, float, float]:
    """Return-to-target; charge taker on turnover; re-target on post-cost equity."""
    eq = _equity(units, cash, px)
    w = target_weights()
    tgt_val = {s: eq * wt for s, wt in w.items()}
    turnover = sum(abs(tgt_val[s] - units.get(s, 0.0) * px[s]["px"]) for s in w if s in px)
    turnover += abs(eq * CASH_FRAC - cash)
    cost = turnover * TAKER
    eq2 = eq - cost
    new_units = {s: (eq2 * wt) / px[s]["px"] for s, wt in w.items() if s in px}
    new_cash = eq2 * CASH_FRAC
    return {"units": new_units, "cash": new_cash}, cost, eq


def _load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def main():
    px = fetch_prices()
    need = set(SYMBOLS) | ({GOLD} if GOLD_FRAC > 0 else set())
    if not need.issubset(px.keys()):
        missing = need - set(px.keys())
        print(f"[rebalance_paper] incomplete prices (missing {missing}); skipping this run.")
        return
    latest_t = max(v["t"] for v in px.values())
    state = _load()

    # first run: seed both strategy book and the buy&hold benchmark; book nothing.
    if not state.get("units"):
        seed = _seed(px)
        state = {
            "units": seed["units"], "cash": seed["cash"],
            "closed": [], "last_bar_t": latest_t, "last_rebal_t": latest_t,
            "equity_at_rebal": START_EQUITY,
            "bh_units": dict(seed["units"]), "bh_cash": seed["cash"],
            "bh_equity_at_rebal": START_EQUITY,
            "starting_equity": START_EQUITY, "started_at": _now_iso(), "n_rebalances": 0,
            "target": {"crypto_frac": CRYPTO_FRAC, "gold_frac": GOLD_FRAC,
                       "cash_frac": CASH_FRAC, "rebalance_days": REBALANCE_DAYS,
                       "symbols": SYMBOLS, "gold": GOLD},
        }
        _save(state)
        print(f"[rebalance_paper] SEEDED ${START_EQUITY:.0f}: "
              f"{int(CRYPTO_FRAC*100)}% crypto ({len(SYMBOLS)}) / "
              f"{int(GOLD_FRAC*100)}% {GOLD} / {int(CASH_FRAC*100)}% cash. "
              f"Rebalance every {REBALANCE_DAYS:.0f}d.")
        return

    if latest_t <= state.get("last_bar_t", 0):
        eq = _equity(state["units"], state["cash"], px)
        print(f"[rebalance_paper] no new daily bar; equity=${eq:.2f}")
        return

    eq = _equity(state["units"], state["cash"], px)
    bh_eq = _equity(state["bh_units"], state["bh_cash"], px)
    days_since = (latest_t - state["last_rebal_t"]) / 86400.0

    if days_since >= REBALANCE_DAYS:
        base = state["equity_at_rebal"] or START_EQUITY
        bh_base = state["bh_equity_at_rebal"] or START_EQUITY
        pnl = eq - base
        ret_pct = pnl / base * 100.0
        bh_ret = (bh_eq - bh_base) / bh_base * 100.0
        rec = {
            "entry_ts": str(state["last_rebal_t"]), "exit_ts": str(latest_t),
            "close_time_iso": _now_iso(),
            "pnl": round(pnl, 4), "ret_pct": round(ret_pct, 3),
            "bh_ret_pct": round(bh_ret, 3), "premium_pct": round(ret_pct - bh_ret, 3),
            "equity_after": round(eq, 2), "days": round(days_since, 1),
        }
        state["closed"].append(rec)
        newbook, cost, _ = _rebalance(state["units"], state["cash"], px)
        state["units"], state["cash"] = newbook["units"], newbook["cash"]
        state["equity_at_rebal"] = eq - cost
        state["bh_equity_at_rebal"] = bh_eq   # benchmark re-baselined, NOT rebalanced
        state["last_rebal_t"] = latest_t
        state["n_rebalances"] += 1
        print(f"[rebalance_paper] REBALANCE #{state['n_rebalances']}: period "
              f"pnl=${pnl:+.2f} ({ret_pct:+.2f}%), premium={ret_pct - bh_ret:+.2f}%, "
              f"cost=${cost:.2f}, equity=${eq:.2f} (bh=${bh_eq:.2f})")
    else:
        prem = (eq / (state['equity_at_rebal'] or START_EQUITY) - 1) * 100 \
               - (bh_eq / (state['bh_equity_at_rebal'] or START_EQUITY) - 1) * 100
        print(f"[rebalance_paper] hold (day {days_since:.0f}/{REBALANCE_DAYS:.0f}) "
              f"equity=${eq:.2f} bh=${bh_eq:.2f} running-premium={prem:+.2f}% "
              f"closed={len(state['closed'])}")

    state["last_bar_t"] = latest_t
    _save(state)


if __name__ == "__main__":
    main()
