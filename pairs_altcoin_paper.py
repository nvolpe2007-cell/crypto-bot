#!/usr/bin/env python3
"""
MARKET-NEUTRAL ALTCOIN PAIRS — FORWARD paper runner (single-shot, scheduler-friendly).

pairs_paper.py only ever traded the 3 combinations of {BTC, ETH, SOL} -- it never
actually TESTED cointegration, it assumed the majors were related enough. This arm
is the result of doing that test properly (research/backtest_altcoin_pairs.py,
2026-08-04): an Engle-Granger cointegration scan (OLS hedge ratio + ADF on the
residual) across all C(10,2)=45 pairs in a wider 10-coin universe, discovered on
the FIRST HALF of 5 years of hourly data only, then the z-score mean-reversion
strategy backtested STRICTLY on the unseen second half (no look-ahead into the
data used to find the relationship).

4 pairs survived that honest out-of-sample test, and held up in BOTH sub-halves
of the held-out period independently (not just averaged over one lucky stretch):

    AVAX/ETH   t=+5.18 then +6.07 (both halves)
    BCH/ETH    t=+4.47 then +2.69 (both halves, degrading but never negative)
    ETH/XRP    t=+1.56 then +6.68 (both halves, weak-but-positive then strong)
    LINK/SOL   t=+7.71 then +8.96 (both halves, the strongest and most consistent)

This is the most statistically robust finding of the whole 2026-08-04 research
session (Sidak family-wise bar for 10 pairs tested is ~2.8; all 4 clear it on the
FULL held-out set, and 3 of 4 clear it in EACH SEPARATE sub-half too). It is still
a backtest, not proof -- this arm exists to earn that proof on the forward clock,
exactly like every other arm here, via the SAME pre-registered mechanics/costs as
the production pairs_paper.py (entry|z|>=2.0, exit|z|<=0.5, stop|z|>=3.5, 7-day
time stop, 0.15%x2-leg cost + funding drag on the short leg) so it is directly,
fairly comparable to that arm's own (currently FAILED) live numbers.

Deliberately its OWN book/state file, not merged into pairs_paper.py's universe:
that arm already has a real (losing) forward trade history on its 3 majors: mixing
in new pairs would corrupt its in-progress proof record. Reuses pairs_paper.py's
pure mechanics (fetch_closed/_spread_series/_zscore/_open/_close/_leg_pnl/
_unrealized/mtm_equity/maybe_drawdown_stop/_alert) as the single source of truth
for cost/exit math; only the pair universe, state file, and Telegram label differ.

    python pairs_altcoin_paper.py        # process any newly-closed bar, then exit
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pairs_paper as pp
from src.state import sanitize_for_json

try:
    from src.kill_switch import is_killed as _is_killed
except Exception:  # pragma: no cover - import-path safety net
    def _is_killed() -> bool:
        return False

# Kraken USD-pair codes for every coin these 4 validated pairs touch.
KRAKEN_PAIRS = {
    "ETH": "ETHUSD", "AVAX": "AVAXUSD", "BCH": "BCHUSD",
    "XRP": "XRPUSD", "LINK": "LINKUSD", "SOL": "SOLUSD",
}

# The 4 pairs that survived out-of-sample cointegration + mean-reversion testing.
# NOT a combinatorial expansion -- an explicit, pre-validated list. Adding a pair
# here without the same discovery-on-train / backtest-on-test discipline would be
# exactly the p-hacking this repo's proof bar exists to catch.
PAIRS = [("AVAX", "ETH"), ("BCH", "ETH"), ("ETH", "XRP"), ("LINK", "SOL")]

STATE_FILE = Path(os.getenv("PAIRS_ALTCOIN_STATE_FILE", "data/pairs_altcoin_paper_state.json"))


def _load_state() -> dict:
    if STATE_FILE.exists():
        import json
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {}, "equity_curve": [],
            "starting_equity": pp.STARTING_EQUITY, "equity": pp.STARTING_EQUITY,
            "equity_mtm": pp.STARTING_EQUITY, "halted": False,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    import json
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitize_for_json(state), indent=2))
    tmp.replace(STATE_FILE)


def _notify(state: dict, prices: dict, acted: int) -> None:
    if os.getenv("PAIRS_NOTIFY", "1") != "1":
        return
    force = os.getenv("PAIRS_NOTIFY_FORCE", "0") == "1"
    if not (force or acted > 0):
        return
    eq = state.get("equity", pp.STARTING_EQUITY)
    start = state.get("starting_equity", pp.STARTING_EQUITY)
    upnl = pp._unrealized(state, prices)
    eq_mtm = eq + upnl
    closed = state.get("closed", [])
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    icon = "📈" if eq_mtm >= start else "📉"
    halt = "  ⛔HALTED" if state.get("halted") else ""
    lines = [f"{icon} <b>Market-Neutral ALTCOIN Pairs — Paper</b>{halt}",
             f"Equity (MTM): <b>${eq_mtm:,.2f}</b>  ({eq_mtm-start:+.2f})  "
             f"[realized ${eq:,.2f}, open ${upnl:+.2f}]  closed: {len(closed)}"
             + (f" ({wins}W/{len(closed)-wins}L)" if closed else "")]
    for key, p in state.get("positions", {}).items():
        u = pp._leg_pnl(p, prices)
        lines.append(f"  {key}: LONG {p['long_sym']} / SHORT {p['short_sym']} "
                     f"(z@entry {p.get('entry_z')}, mark ${u:+.2f})" if u is not None
                     else f"  {key}: LONG {p['long_sym']} / SHORT {p['short_sym']}")
    if not state.get("positions"):
        lines.append("  (no open pairs)")
    pp._alert("\n".join(lines))


def process(state: dict, closes_by_base: dict, prices: dict, now) -> int:
    acted = 0
    allow_open = not state.get("halted", False) and not _is_killed()
    for pair in PAIRS:
        a, b = pair
        key = "-".join(pair)
        if a not in closes_by_base or b not in closes_by_base:
            continue
        series = pp._spread_series(closes_by_base[a], closes_by_base[b])
        if len(series) < pp.LOOKBACK + 1:
            continue
        latest_t = series[-1][0]
        if state["last_bar_t"].get(key) == latest_t:
            continue
        state["last_bar_t"][key] = latest_t
        z = pp._zscore([s for _, s in series], pp.LOOKBACK)
        if z is None:
            continue
        ts = str(latest_t)
        pos = state["positions"].get(key)
        if pos:
            held = sum(1 for t, _ in series if t > int(pos["entry_ts"]))
            if abs(z) <= pp.EXIT_Z:
                rec = pp._close(state, key, prices, ts, "converged")
                print(f"{key}: EXIT converged z={z:+.2f} net=${rec['pnl']:+.2f}")
                acted += 1
            elif abs(z) >= pp.STOP_Z:
                rec = pp._close(state, key, prices, ts, "stop:diverged")
                print(f"{key}: STOP diverged z={z:+.2f} net=${rec['pnl']:+.2f}")
                acted += 1
            elif held >= pp.MAX_HOLD_BARS:
                rec = pp._close(state, key, prices, ts, "stop:time")
                print(f"{key}: STOP time held={held} net=${rec['pnl']:+.2f}")
                acted += 1
        elif abs(z) >= pp.ENTRY_Z and allow_open:
            pp._open(state, pair, z, prices, ts)
            p = state["positions"][key]
            print(f"{key}: OPEN z={z:+.2f} LONG {p['long_sym']} / SHORT {p['short_sym']} "
                  f"@ ${pp.LEG_NOTIONAL:.0f}/leg")
            acted += 1
    return acted


def main() -> int:
    state = _load_state()
    if state.get("halted") and os.getenv("PAIRS_ALTCOIN_REARM", "0") == "1":
        state["halted"] = False
        state.pop("halted_at", None)
        print("[pairs_altcoin_paper] re-armed (PAIRS_ALTCOIN_REARM=1).")

    closes_by_base, prices = {}, {}
    for base, code in KRAKEN_PAIRS.items():
        try:
            bars = pp.fetch_closed(code)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        if bars:
            closes_by_base[base] = {b["t"]: b["c"] for b in bars}
            prices[base] = bars[-1]["c"]

    now = datetime.now(timezone.utc)
    ts_now = str(max((max(d) for d in closes_by_base.values()), default=""))
    if prices:
        stopped = pp.maybe_drawdown_stop(state, prices, ts_now, now)
        eq_mtm = pp.mtm_equity(state, prices)
        state["equity_mtm"] = round(eq_mtm, 2)
        state.setdefault("equity_curve", []).append(
            {"ts": now.isoformat(), "equity_mtm": round(eq_mtm, 2),
             "realized": round(state.get("equity", pp.STARTING_EQUITY), 2)})
        state["equity_curve"] = state["equity_curve"][-400:]
        if stopped:
            print(f"[pairs_altcoin_paper] DRAWDOWN STOP — MTM ${eq_mtm:,.2f}; flattened + halted.")

    acted = process(state, closes_by_base, prices, now)
    state["equity_mtm"] = round(pp.mtm_equity(state, prices), 2)
    _notify(state, prices, acted)
    _save_state(state)
    eq_mtm = state.get("equity_mtm", state.get("equity", pp.STARTING_EQUITY))
    print(f"[pairs_altcoin_paper] {now:%Y-%m-%d %H:%M} UTC equity_mtm=${eq_mtm:.2f} "
          f"({eq_mtm-state.get('starting_equity', pp.STARTING_EQUITY):+.2f}) acted={acted} "
          f"open={list(state['positions'].keys())} "
          f"{'HALTED ' if state.get('halted') else ''}closed={len(state['closed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
