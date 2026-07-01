#!/usr/bin/env python3
"""
MARKET-NEUTRAL PAIRS — FORWARD paper runner (single-shot, scheduler-friendly).

The honest hedge-fund staple: a DOLLAR-NEUTRAL relative-value trade, not a directional
bet. When two correlated majors' price ratio stretches far from its mean, we LONG the
cheap leg and SHORT the rich leg in equal dollar size, and profit from CONVERGENCE —
regardless of whether crypto goes up or down. (Contrast dispatch's src/pairs_strategy.py,
which trades only ONE leg directionally; that is not market-neutral.)

This is PAPER, so the short leg is simulated honestly (perp short + a conservative funding
drag) — which lets us forward-test whether the neutral edge clears REAL costs BEFORE
Kraken's US perps land. Its own $1k book, judged on the SAME pre-registered proof bar as
every other arm (proof_scorecard._pairs_forward). The cost wall is brutal here: a pair
round-trip pays ~4 legs of fees, so the spread must converge by more than that to win —
the forward test is exactly to see if it does. No assuming an edge.

PRE-SPECIFIED SPEC (not swept):
  * Universe: all pairs of {BTC, ETH, SOL} — the liquid, highly-correlated majors.
  * Spread: log price ratio  s = ln(P_a) - ln(P_b), z-scored over a rolling window.
  * Entry |z| >= ENTRY_Z: z>0 (a rich vs b) -> SHORT a + LONG b; z<0 -> LONG a + SHORT b.
    Equal dollar notional per leg = dollar-neutral (market beta ~cancels).
  * Exit when |z| <= EXIT_Z (convergence captured), or stop if |z| >= STOP_Z (the spread
    broke down, not mean-reverting) or held > MAX_HOLD_BARS (time stop).
  * Costs: each leg pays a round-trip taker+slippage on entry AND exit; the short leg is
    charged a conservative funding drag for the holding period. Deliberately pessimistic.
  * One position per pair; own $1k book; per-book drawdown stop flattens + halts.

FORWARD-ONLY & idempotent: acts only on newly-CLOSED bars (default hourly), one decision
per pair per bar. Run it on a schedule (cron/timer) — each invocation processes any new bar.

    python pairs_paper.py        # process any newly-closed bar, then exit
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

# Master kill switch (best-effort import — never block trading if it can't load).
try:
    from src.kill_switch import is_killed as _is_killed
except Exception:  # pragma: no cover - import-path safety net
    def _is_killed() -> bool:
        return False

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

INTERVAL_MIN = int(os.getenv("PAIRS_INTERVAL_MIN", "60"))          # 60 = hourly bars
LOOKBACK = int(os.getenv("PAIRS_LOOKBACK", "168"))                 # z-score window (7d hourly)
ENTRY_Z = float(os.getenv("PAIRS_ENTRY_Z", "2.0"))
EXIT_Z = float(os.getenv("PAIRS_EXIT_Z", "0.5"))
STOP_Z = float(os.getenv("PAIRS_STOP_Z", "3.5"))
MAX_HOLD_BARS = int(os.getenv("PAIRS_MAX_HOLD_BARS", "168"))       # time stop (7d hourly)
COST_FRAC = float(os.getenv("PAIRS_COST_FRAC", "0.0015"))         # per leg, round-trip
FUNDING_APY = float(os.getenv("PAIRS_FUNDING_APY", "0.10"))       # conservative short-leg drag
STARTING_EQUITY = float(os.getenv("PAIRS_START_EQUITY", "1000"))
LEG_FRAC = float(os.getenv("PAIRS_LEG_FRAC", "0.15"))            # notional/leg as frac of start
LEG_NOTIONAL = round(STARTING_EQUITY * LEG_FRAC, 2)
MAX_DRAWDOWN = float(os.getenv("PAIRS_MAX_DRAWDOWN", "150"))      # MTM stop; 0 disables
STATE_FILE = Path(os.getenv("PAIRS_STATE_FILE", "data/pairs_paper_state.json"))
HOURS_PER_YEAR = 24.0 * 365.0
_PAIRS = [tuple(sorted(p)) for p in combinations(KRAKEN_PAIRS, 2)]


def fetch_closed(pair: str) -> list[dict]:
    """Ascending closes with the in-progress bar dropped (no repaint)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_MIN}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    return [{"t": int(row[0]), "c": float(row[4])} for row in series][:-1]


def _spread_series(a_by_t: dict[int, float], b_by_t: dict[int, float]) -> list[tuple[int, float]]:
    """Aligned ln(Pa/Pb) on the common timestamps, ascending."""
    common = sorted(set(a_by_t) & set(b_by_t))
    return [(t, math.log(a_by_t[t]) - math.log(b_by_t[t]))
            for t in common if a_by_t[t] > 0 and b_by_t[t] > 0]


def _zscore(spreads: list[float], n: int) -> float | None:
    if len(spreads) < n:
        return None
    win = spreads[-n:]
    mean = sum(win) / n
    var = sum((x - mean) ** 2 for x in win) / n
    std = math.sqrt(var)
    if std < 1e-12:
        return None
    return (spreads[-1] - mean) / std


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {}, "equity_curve": [],
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "equity_mtm": STARTING_EQUITY, "halted": False,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _funding_cost(notional: float, entry_ts: str, exit_ts: str) -> float:
    try:
        hours = (int(exit_ts) - int(entry_ts)) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return notional * FUNDING_APY * max(0.0, hours) / HOURS_PER_YEAR


def _leg_pnl(pos: dict, prices: dict[str, float]) -> float | None:
    """Mark both legs at current price: long leg gains as it rises, short as it falls."""
    pl = prices.get(pos["long_sym"]); ps = prices.get(pos["short_sym"])
    if not pl or not ps:
        return None
    long_ret = (pl - pos["long_entry"]) / pos["long_entry"]
    short_ret = (pos["short_entry"] - ps) / pos["short_entry"]
    return pos["leg_notional"] * (long_ret + short_ret)


def _open(state: dict, pair: tuple[str, str], z: float, prices: dict, ts: str) -> None:
    a, b = pair
    # z>0: a is rich vs b -> short a, long b. z<0: long a, short b.
    short_sym, long_sym = (a, b) if z > 0 else (b, a)
    state["positions"]["-".join(pair)] = {
        "pair": "-".join(pair), "long_sym": long_sym, "short_sym": short_sym,
        "long_entry": prices[long_sym], "short_entry": prices[short_sym],
        "leg_notional": LEG_NOTIONAL, "entry_ts": ts, "entry_z": round(z, 3)}


def _close(state: dict, key: str, prices: dict, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(key)
    gross = _leg_pnl(pos, prices) or 0.0
    cost = pos["leg_notional"] * COST_FRAC * 2        # both legs round-trip
    funding = _funding_cost(pos["leg_notional"], pos["entry_ts"], ts)
    net = gross - cost - funding
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": pos["pair"], "long_sym": pos["long_sym"], "short_sym": pos["short_sym"],
           "entry_ts": pos["entry_ts"], "exit_ts": ts, "entry_z": pos.get("entry_z"),
           "size_usd": pos["leg_notional"] * 2, "funding_cost": round(funding, 4),
           "gross_pnl": round(gross, 4), "pnl": round(net, 4),
           "pnl_pct": round(net / (pos["leg_notional"] * 2) * 100, 3),
           "reason": reason, "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def _unrealized(state: dict, prices: dict) -> float:
    return sum((_leg_pnl(p, prices) or 0.0) for p in state.get("positions", {}).values())


def mtm_equity(state: dict, prices: dict) -> float:
    return state.get("equity", STARTING_EQUITY) + _unrealized(state, prices)


def maybe_drawdown_stop(state: dict, prices: dict, ts: str, now) -> bool:
    """Flatten all + halt new entries if MTM breaches the cap. Returns True if engaged."""
    if MAX_DRAWDOWN <= 0:
        return False
    start = state.get("starting_equity", STARTING_EQUITY)
    if mtm_equity(state, prices) > start - MAX_DRAWDOWN:
        return False
    already = state.get("halted", False)
    for key in list(state["positions"].keys()):
        _close(state, key, prices, ts, "risk:drawdown_stop")
    state["halted"] = True
    state["halted_at"] = now.isoformat()
    if not already:
        _alert(f"\U0001f6d1 <b>Pairs arm — DRAWDOWN STOP</b>\nMTM breached -${MAX_DRAWDOWN:.0f}; "
               f"flattened all pairs, new entries halted (re-arm PAIRS_REARM=1). "
               f"Equity ${state.get('equity', STARTING_EQUITY):,.2f}.")
    return True


def _alert(html: str) -> None:
    if os.getenv("PAIRS_NOTIFY", "1") != "1":
        return
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from src.notifications import create_notifier_from_env
        notifier = create_notifier_from_env()
        # This is a one-shot cron script, not the long-running bot process — force
        # a synchronous send so the HTTP call completes before the process exits
        # (the default async_safe queue's worker thread would get killed first).
        notifier._async_safe = False
        notifier.send_message(html)
    except Exception as e:
        print(f"[pairs_paper] telegram alert skipped: {e}")


def _notify(state: dict, prices: dict, acted: int) -> None:
    if os.getenv("PAIRS_NOTIFY", "1") != "1":
        return
    force = os.getenv("PAIRS_NOTIFY_FORCE", "0") == "1"
    if not (force or acted > 0):
        return
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    upnl = _unrealized(state, prices)
    eq_mtm = eq + upnl
    closed = state.get("closed", [])
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    icon = "📈" if eq_mtm >= start else "📉"
    halt = "  ⛔HALTED" if state.get("halted") else ""
    lines = [f"{icon} <b>Market-Neutral Pairs — Paper</b>{halt}",
             f"Equity (MTM): <b>${eq_mtm:,.2f}</b>  ({eq_mtm-start:+.2f})  "
             f"[realized ${eq:,.2f}, open ${upnl:+.2f}]  closed: {len(closed)}"
             + (f" ({wins}W/{len(closed)-wins}L)" if closed else "")]
    for key, p in state.get("positions", {}).items():
        u = _leg_pnl(p, prices)
        lines.append(f"  {key}: LONG {p['long_sym']} / SHORT {p['short_sym']} "
                     f"(z@entry {p.get('entry_z')}, mark ${u:+.2f})" if u is not None
                     else f"  {key}: LONG {p['long_sym']} / SHORT {p['short_sym']}")
    if not state.get("positions"):
        lines.append("  (no open pairs)")
    _alert("\n".join(lines))


def process(state: dict, closes_by_base: dict[str, dict[int, float]],
            prices: dict[str, float], now) -> int:
    acted = 0
    allow_open = not state.get("halted", False) and not _is_killed()
    for pair in _PAIRS:
        a, b = pair
        key = "-".join(pair)
        if a not in closes_by_base or b not in closes_by_base:
            continue
        series = _spread_series(closes_by_base[a], closes_by_base[b])
        if len(series) < LOOKBACK + 1:
            continue
        latest_t = series[-1][0]
        if state["last_bar_t"].get(key) == latest_t:
            continue                                  # idempotent: already processed this bar
        state["last_bar_t"][key] = latest_t
        z = _zscore([s for _, s in series], LOOKBACK)
        if z is None:
            continue
        ts = str(latest_t)
        pos = state["positions"].get(key)
        if pos:
            held = sum(1 for t, _ in series if t > int(pos["entry_ts"]))
            if abs(z) <= EXIT_Z:
                rec = _close(state, key, prices, ts, "converged")
                print(f"{key}: EXIT converged z={z:+.2f} net=${rec['pnl']:+.2f}")
                acted += 1
            elif abs(z) >= STOP_Z:
                rec = _close(state, key, prices, ts, "stop:diverged")
                print(f"{key}: STOP diverged z={z:+.2f} net=${rec['pnl']:+.2f}")
                acted += 1
            elif held >= MAX_HOLD_BARS:
                rec = _close(state, key, prices, ts, "stop:time")
                print(f"{key}: STOP time held={held} net=${rec['pnl']:+.2f}")
                acted += 1
        elif abs(z) >= ENTRY_Z and allow_open:
            _open(state, pair, z, prices, ts)
            p = state["positions"][key]
            print(f"{key}: OPEN z={z:+.2f} LONG {p['long_sym']} / SHORT {p['short_sym']} "
                  f"@ ${LEG_NOTIONAL:.0f}/leg")
            acted += 1
    return acted


def main() -> int:
    state = _load_state()
    if state.get("halted") and os.getenv("PAIRS_REARM", "0") == "1":
        state["halted"] = False
        state.pop("halted_at", None)
        print("[pairs_paper] re-armed (PAIRS_REARM=1).")

    closes_by_base, prices = {}, {}
    for base, code in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed(code)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        if bars:
            closes_by_base[base] = {b["t"]: b["c"] for b in bars}
            prices[base] = bars[-1]["c"]

    now = datetime.now(timezone.utc)
    ts_now = str(max((max(d) for d in closes_by_base.values()), default=""))
    # Risk + mark every run, even with no new bar.
    if prices:
        stopped = maybe_drawdown_stop(state, prices, ts_now, now)
        eq_mtm = mtm_equity(state, prices)
        state["equity_mtm"] = round(eq_mtm, 2)
        state.setdefault("equity_curve", []).append(
            {"ts": now.isoformat(), "equity_mtm": round(eq_mtm, 2),
             "realized": round(state.get("equity", STARTING_EQUITY), 2)})
        state["equity_curve"] = state["equity_curve"][-400:]
        if stopped:
            print(f"[pairs_paper] DRAWDOWN STOP — MTM ${eq_mtm:,.2f}; flattened + halted.")

    acted = process(state, closes_by_base, prices, now)
    state["equity_mtm"] = round(mtm_equity(state, prices), 2)
    _notify(state, prices, acted)
    _save_state(state)
    eq_mtm = state.get("equity_mtm", state.get("equity", STARTING_EQUITY))
    print(f"[pairs_paper] {now:%Y-%m-%d %H:%M} UTC equity_mtm=${eq_mtm:.2f} "
          f"({eq_mtm-state.get('starting_equity', STARTING_EQUITY):+.2f}) acted={acted} "
          f"open={list(state['positions'].keys())} "
          f"{'HALTED ' if state.get('halted') else ''}closed={len(state['closed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
