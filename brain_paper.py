#!/usr/bin/env python3
"""
BRAIN paper trader — FORWARD runner for the Claude-driven discretionary arm.

The mechanical arms follow a fixed rule. This one asks Claude (src/trade_brain.py)
to decide LONG / SHORT / FLAT per coin each day from the full market picture, runs
its OWN $1k paper perp account (1x, can short), and is judged head-to-head against
the rules by proof_scorecard (_brain_forward). The honest test of whether a thinking
brain beats rules or just adds expensive noise.

  python brain_paper.py        # build today's snapshot, ask the brain, act, exit

FAIL-SAFE: no ANTHROPIC_API_KEY or any API error → the brain returns nothing and we
HOLD current positions (no churn, no crash). Acts only on newly-CLOSED daily bars,
so running it every 6h is idempotent (one decision per coin per day max).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
COST_FRAC = float(os.getenv("BRAIN_COST_FRAC", "0.0015"))      # perp taker + slippage round-trip
FUNDING_APY = float(os.getenv("BRAIN_FUNDING_APY", "0.10"))    # conservative funding drag
STARTING_EQUITY = float(os.getenv("BRAIN_START_EQUITY", "1000"))
BASE_ALLOC = round(STARTING_EQUITY / len(KRAKEN_PAIRS), 2)     # per-coin base notional
# Mark-to-market drawdown stop (USD). When the book's MTM equity (realized + open
# unrealized at current price) falls to start - this, flatten every position at the
# current price and HALT new entries until re-armed. The brain can still flip/close
# while halted; it just can't open fresh risk. 0 disables. Default 200 = -20% on a
# $1k book: a generous leash that respects the brain's conviction but caps a blowup.
MAX_DRAWDOWN = float(os.getenv("BRAIN_MAX_DRAWDOWN", "200"))
# DRY RUN / self-test: exercise the FULL pipeline (live data -> snapshot -> decide ->
# paper account -> Telegram) with a transparent local heuristic instead of the API,
# on a SEPARATE state file so it never pollutes the real brain ledger. Lets you verify
# the machine end-to-end before funding the Anthropic account.
DRY_RUN = os.getenv("BRAIN_DRY_RUN", "0") == "1"
_default_state = "data/brain_dryrun_state.json" if DRY_RUN else "data/brain_paper_state.json"
STATE_FILE = Path(os.getenv("BRAIN_STATE_FILE", _default_state))
INTERVAL_DAILY = 1440
HOURS_PER_YEAR = 24.0 * 365.0


def fetch_closed_daily(pair: str) -> list[dict]:
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_DAILY}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(row[0]), "c": float(row[4])} for row in series]
    return bars[:-1]


def _sma(c: list[float], n: int):
    return sum(c[-n:]) / n if len(c) >= n else None


def _pct(a: float, b: float):
    return (a / b - 1.0) * 100 if b else 0.0


def build_snapshot(closes_by_coin: dict[str, list[float]], state: dict,
                   market_ctx: dict | None = None) -> dict:
    """Factual per-coin market picture for the brain to reason over (no opinion baked in).
    When market_ctx is given, fold in the desk signals (regime/IV/funding) the main bot
    already computes so the brain reasons over the same picture as the rest of the system."""
    per_coin = (market_ctx or {}).get("per_coin", {})
    snap = {}
    for coin, c in closes_by_coin.items():
        if len(c) < 60:
            continue
        price = c[-1]
        sma50, sma100, sma200 = _sma(c, 50), _sma(c, 100), _sma(c, 200)
        pos = state["positions"].get(coin)
        cur = None
        if pos:
            ret = pos["side"] * (price - pos["entry"]) / pos["entry"] * 100
            cur = {"side": "long" if pos["side"] > 0 else "short",
                   "entry": round(pos["entry"], 4), "unrealized_pct": round(ret, 2)}
        snap[coin] = {
            "price": round(price, 4),
            "vs_sma50_pct": round(_pct(price, sma50), 2) if sma50 else None,
            "vs_sma100_pct": round(_pct(price, sma100), 2) if sma100 else None,
            "vs_sma200_pct": round(_pct(price, sma200), 2) if sma200 else None,
            "ret_5d_pct": round(_pct(price, c[-6]), 2) if len(c) > 6 else None,
            "ret_20d_pct": round(_pct(price, c[-21]), 2) if len(c) > 21 else None,
            "ret_60d_pct": round(_pct(price, c[-61]), 2) if len(c) > 61 else None,
            "current_position": cur or "flat",
        }
        if coin in per_coin:
            snap[coin]["market"] = per_coin[coin]
    return snap


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {}, "decisions": [],
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "equity_mtm": STARTING_EQUITY, "equity_curve": [], "halted": False,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _funding_cost(size: float, entry_ts: str, exit_ts: str) -> float:
    try:
        hours = (int(exit_ts) - int(entry_ts)) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return size * FUNDING_APY * max(0.0, hours) / HOURS_PER_YEAR


def _close(state: dict, coin: str, price: float, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(coin)
    ret = pos["side"] * (price - pos["entry"]) / pos["entry"]
    funding = _funding_cost(pos["size_usd"], pos["entry_ts"], ts)
    net = pos["size_usd"] * ret - pos["size_usd"] * COST_FRAC - funding
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": coin, "side": pos["side"], "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "size_usd": pos["size_usd"],
           "funding_cost": round(funding, 4), "pnl": round(net, 4),
           "pnl_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def _open(state: dict, coin: str, side: int, size_usd: float, price: float, ts: str) -> None:
    state["positions"][coin] = {"symbol": coin, "side": side, "entry": price,
                                "entry_ts": ts, "size_usd": round(size_usd, 2)}


def _unrealized(state: dict, prices: dict) -> float:
    """Open positions' P&L marked at current prices (0 for any coin we can't price)."""
    upnl = 0.0
    for coin, p in state.get("positions", {}).items():
        px = prices.get(coin)
        if px and p.get("entry"):
            upnl += p["size_usd"] * p["side"] * (px - p["entry"]) / p["entry"]
    return upnl


def mtm_equity(state: dict, prices: dict) -> float:
    """Realized equity + open unrealized P&L. This is the honest book value; the
    plain `equity` field only moves on a close and hides open drawdown."""
    return state.get("equity", STARTING_EQUITY) + _unrealized(state, prices)


def maybe_drawdown_stop(state: dict, prices: dict, latest: dict, now) -> bool:
    """If MTM equity has breached the cap, flatten every priceable position at the
    current price, mark the book halted, and alert. Returns True if it engaged.
    Idempotent: a book already halted with no open positions just stays halted."""
    if MAX_DRAWDOWN <= 0:
        return False
    start = state.get("starting_equity", STARTING_EQUITY)
    if mtm_equity(state, prices) > start - MAX_DRAWDOWN:
        return False
    already = state.get("halted", False)
    realized_before = state.get("equity", STARTING_EQUITY)
    for coin in list(state["positions"].keys()):
        px = prices.get(coin)
        if not px:
            continue
        ts = str(latest.get(coin, {}).get("t", ""))
        rec = _close(state, coin, px, ts, "risk:drawdown_stop")
        print(f"{coin}: DRAWDOWN STOP close @ {px:.4f} net=${rec['pnl']:+.2f}")
    state["halted"] = True
    state["halted_at"] = now.isoformat()
    if not already:
        _alert(state, f"\U0001f6d1 <b>AI Brain — DRAWDOWN STOP</b>\n"
                      f"MTM equity breached the -${MAX_DRAWDOWN:.0f} cap. Flattened all "
                      f"positions (realized ${state.get('equity', STARTING_EQUITY)-realized_before:+.2f}); "
                      f"new entries halted until re-armed (set BRAIN_REARM=1 or raise "
                      f"BRAIN_MAX_DRAWDOWN). Equity now ${state.get('equity', STARTING_EQUITY):,.2f}.")
    return True


def apply_decision(state: dict, coin: str, dec, price: float, ts: str,
                   allow_open: bool = True) -> int:
    """Reconcile the coin's position to the brain's target. Returns # actions taken.
    When allow_open is False (book halted) the brain may still close/flatten but cannot
    open fresh risk."""
    target = {"long": 1, "short": -1, "flat": 0}.get(dec.action, 0)
    # Aggressive but bounded: conviction-scaled size up to 2.5x base per coin.
    size_usd = BASE_ALLOC * max(0.0, min(2.5, dec.size_mult)) if target != 0 else 0.0
    cur = state["positions"].get(coin, {}).get("side", 0)
    acted = 0
    # Close if direction changes or going flat.
    if cur != 0 and cur != target:
        rec = _close(state, coin, price, ts, f"brain:{dec.action}")
        print(f"{coin}: CLOSE {'LONG' if cur>0 else 'SHORT'} @ {price:.4f} "
              f"net=${rec['pnl']:+.2f} ({dec.key_signal[:40]})")
        acted += 1
        cur = 0
    # Open the new side.
    if target != 0 and cur == 0:
        if not allow_open:
            print(f"{coin}: OPEN {dec.action.upper()} SKIPPED — book halted (drawdown stop)")
            return acted
        _open(state, coin, target, size_usd, price, ts)
        print(f"{coin}: OPEN {dec.action.upper()} @ {price:.4f} size=${size_usd:.0f} "
              f"conv={dec.conviction} ({dec.key_signal[:40]})")
        acted += 1
    return acted


def _alert(state: dict, html: str) -> None:
    """Fire a one-off Telegram message (risk alerts). Never raises."""
    if os.getenv("BRAIN_NOTIFY", "1") != "1":
        return
    try:
        from src.notifications import create_notifier_from_env
        create_notifier_from_env().send_message(html)
    except Exception as e:
        print(f"[brain_paper] telegram alert skipped: {e}")


def _notify(state: dict, result, prices: dict, acted: int) -> None:
    if os.getenv("BRAIN_NOTIFY", "1") != "1":
        return
    # Notify on a trade, a forced run, OR any day the brain made a decision (so you
    # get its reasoning every day it thinks, even when it holds). The runner only
    # reaches here on days with a fresh bar, so this is ~one update/day, not spam.
    if not (os.getenv("BRAIN_NOTIFY_FORCE", "0") == "1" or acted > 0
            or (result and getattr(result, "decisions", None))):
        return
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    upnl = _unrealized(state, prices)
    eq_mtm = eq + upnl
    icon = "🧠📈" if eq_mtm >= start else "🧠📉"
    title = "AI Brain — SELF-TEST (dry run, not the AI)" if DRY_RUN else "AI Brain — Paper Account"
    halt = "  ⛔HALTED" if state.get("halted") else ""
    lines = [f"{icon} <b>{title}</b>{halt}",
             f"Equity (MTM): <b>${eq_mtm:,.2f}</b>  ({eq_mtm-start:+.2f}, {(eq_mtm/start-1)*100:+.1f}%)  "
             f"[realized ${eq:,.2f}, open ${upnl:+.2f}]  closed: {len(state.get('closed', []))}"]
    for coin, p in state.get("positions", {}).items():
        px = prices.get(coin)
        sd = "🟢LONG" if p["side"] > 0 else "🔴SHORT"
        u = p["side"] * (px - p["entry"]) / p["entry"] * 100 if px else 0.0
        lines.append(f"  {coin} {sd} @ ${p['entry']:,.2f} ({u:+.1f}%)")
    if not state.get("positions"):
        lines.append("  (all flat)")
    for coin, dec in (result.decisions or {}).items():
        lines.append(f"<b>{coin}</b> → {dec.action.upper()} (conv {dec.conviction}): "
                     f"{dec.reasoning[:180]}")
    try:
        from src.notifications import create_notifier_from_env
        create_notifier_from_env().send_message("\n".join(lines))
    except Exception as e:
        print(f"[brain_paper] telegram notify skipped: {e}")


def _dry_run_result(snapshot: dict):
    """A transparent stand-in for the brain: a plain trend heuristic, clearly labeled,
    so a self-test exercises the whole pipeline without an API call. NOT the real brain."""
    from src.trade_brain import BrainResult, CoinDecision
    decs = {}
    for coin, s in snapshot.items():
        v50 = s.get("vs_sma50_pct"); m20 = s.get("ret_20d_pct")
        if v50 is None or m20 is None:
            action, key = "flat", "warming up"
        elif v50 > 0 and m20 > 0:
            action, key = "long", "above SMA50 + positive 20d momentum"
        elif v50 < 0 and m20 < 0:
            action, key = "short", "below SMA50 + negative 20d momentum"
        else:
            action, key = "flat", "mixed (price/MA and momentum disagree)"
        decs[coin] = CoinDecision(coin=coin, action=action, conviction=6, size_mult=1.0,
                                  key_signal=key, invalidation="trend reverses",
                                  reasoning=f"[SELF-TEST heuristic, not the AI] {key}")
    return BrainResult(decisions=decs, model="dry-run-heuristic")


def main():
    from src.trade_brain import TradeBrain
    state = _load_state()
    closes, prices, latest = {}, {}, {}
    for coin, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed_daily(pair)
        except Exception as e:
            print(f"{coin}: fetch failed - {e}")
            continue
        if bars:
            closes[coin] = [b["c"] for b in bars]
            prices[coin] = bars[-1]["c"]
            latest[coin] = bars[-1]

    now = datetime.now(timezone.utc)
    # Re-arm a previously halted book only on explicit request.
    if state.get("halted") and os.getenv("BRAIN_REARM", "0") == "1":
        state["halted"] = False
        state.pop("halted_at", None)
        print("[brain_paper] re-armed (BRAIN_REARM=1) — new entries allowed again.")

    # Risk + observability run on EVERY invocation (even with no fresh bar or no API
    # key): mark the book to market and enforce the drawdown stop on open positions,
    # so a losing book can't bleed unbounded between daily decisions or hide behind a
    # frozen realized-equity number.
    if prices:
        stopped = maybe_drawdown_stop(state, prices, latest, now)
        eq_mtm = mtm_equity(state, prices)
        state["equity_mtm"] = round(eq_mtm, 2)
        state.setdefault("equity_curve", []).append(
            {"ts": now.isoformat(), "equity_mtm": round(eq_mtm, 2),
             "realized": round(state.get("equity", STARTING_EQUITY), 2)})
        state["equity_curve"] = state["equity_curve"][-400:]
        if stopped:
            print(f"[brain_paper] DRAWDOWN STOP engaged — MTM ${eq_mtm:,.2f} "
                  f"<= start-${MAX_DRAWDOWN:.0f}; positions flattened, new entries halted.")

    brain = None
    if not DRY_RUN:
        brain = TradeBrain()
        if not brain.available():
            print("[brain_paper] no ANTHROPIC_API_KEY — brain idle, holding current positions.")
            _save_state(state)
            return

    # Only decide on coins with a newly-closed bar (idempotent: one decision/coin/day).
    # A dry run ignores that and always acts, so the self-test always shows something.
    fresh = {c: latest[c] for c in latest
             if (DRY_RUN or state["last_bar_t"].get(c) != latest[c]["t"])
             and len(closes.get(c, [])) >= 60}
    if not fresh:
        print("[brain_paper] no new daily bar — nothing to decide.")
        _save_state(state)
        return

    # Desk context (regime/IV/funding/sentiment) the main loop already computes — read
    # from the shared state.json so the brain sees the same picture. Best-effort.
    try:
        from src.market_context import load_market_context
        market_ctx = load_market_context(list(closes.keys()))
    except Exception as e:
        print(f"[brain_paper] market context unavailable: {e}")
        market_ctx = {"stale": True, "macro": {}, "per_coin": {}}
    macro = {**market_ctx.get("macro", {}),
             "stale": market_ctx.get("stale", True),
             "as_of": market_ctx.get("as_of"), "age_sec": market_ctx.get("age_sec")}

    snapshot = build_snapshot({c: closes[c] for c in closes}, state, market_ctx)
    if DRY_RUN:
        result = _dry_run_result(snapshot)
        print("[brain_paper] *** DRY RUN / SELF-TEST *** local heuristic, NO API call, "
              f"separate ledger ({STATE_FILE.name}).")
    else:
        result = brain.decide(snapshot, now, macro=macro)
        if not result.ok:
            print(f"[brain_paper] brain unavailable ({result.error}) — holding.")
            _save_state(state)
            return

    total = 0
    allow_open = not state.get("halted", False)
    for coin in fresh:
        dec = result.decisions.get(coin)
        if dec is None:
            continue
        total += apply_decision(state, coin, dec, prices[coin], str(latest[coin]["t"]),
                                allow_open=allow_open)
        state["last_bar_t"][coin] = latest[coin]["t"]
    # Positions may have changed (flips/closes) — refresh the MTM mark before saving.
    state["equity_mtm"] = round(mtm_equity(state, prices), 2)
    # Keep a rolling log of the brain's reasoning (last 50 decisions).
    state["decisions"].append({"ts": now.isoformat(),
                               "decisions": {c: vars(d) for c, d in result.decisions.items()},
                               "tokens_in": result.input_tokens,
                               "tokens_out": result.output_tokens})
    state["decisions"] = state["decisions"][-50:]
    _notify(state, result, prices, total)
    _save_state(state)
    eq = state.get("equity", STARTING_EQUITY)
    eq_mtm = state.get("equity_mtm", eq)
    held = {c: ("L" if p["side"] > 0 else "S") for c, p in state["positions"].items()}
    print(f"[brain_paper] {now:%Y-%m-%d %H:%M} UTC  equity_mtm=${eq_mtm:.2f} "
          f"({eq_mtm-state.get('starting_equity', STARTING_EQUITY):+.2f}) "
          f"[realized ${eq:.2f}] acted={total} held={held} "
          f"{'HALTED ' if state.get('halted') else ''}closed={len(state['closed'])} "
          f"model={result.model} tok={result.input_tokens}/{result.output_tokens}")


if __name__ == "__main__":
    main()
