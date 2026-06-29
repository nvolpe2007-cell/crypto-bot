#!/usr/bin/env python3
"""
LEVERAGED perp paper arm — "place a trade with leverage, sell in profit."

A FORWARD paper runner (single-shot, loop-friendly) that opens a LEVERAGED perp
position in the trend direction and exits on a FIXED TAKE-PROFIT — the "sell in
profit" the owner asked for — with a realistic LIQUIDATION as the downside, because
leverage cuts both ways. It runs its OWN $1k paper book and is judged head-to-head
by proof_scorecard (_lev_perp_forward), exactly like every other arm.

HONEST FRAMING (do not lose this):
  • Leverage is settled-dangerous here (memory doubling_in_a_month_verdict: the
    leverage that doubles fast also liquidates ~half the time). This arm exists to
    SHOW that on the forward clock with real prices, paper money, NO real risk — not
    because leverage is believed to add edge. The proof bar (n>=30, family-wise t)
    decides, same as everything else.
  • PAPER ONLY. US Kraken-spot cannot trade perps/leverage with real funds today;
    Kraken US perps (Bitnomial) aren't integrated. This is a simulation.

PRE-SPECIFIED SPEC (fixed, not swept):
  * Universe: BTC, ETH, SOL (the liquid trenders).
  * Direction: daily close vs SMA(50). +1 LONG above, -1 SHORT below.
  * Leverage: LEV_PERP_LEVERAGE (default 3x). Margin per position = equal fraction
    of STARTING equity; notional = margin * leverage.
  * "Sell in profit": fixed TAKE-PROFIT at LEV_PERP_TP_PRICE_FRAC favorable PRICE
    move (default 5% -> 15% on margin at 3x). Detected intrabar via the bar's high/low.
  * Downside: LIQUIDATION at an adverse price move of (1-maint)/leverage
    (maint=LEV_PERP_MAINT, default 5%). At 3x that's ~31.7% against you -> margin gone.
    Conservative: if a single bar touches BOTH take-profit and liquidation, assume
    LIQUIDATION hit first (worst case).
  * Also exits on a trend FLIP (signal reverses) at the bar close.
  * Costs: perp taker round-trip (LEV_PERP_COST_FRAC) on NOTIONAL, plus a conservative
    funding drag (LEV_PERP_FUNDING_APY) on NOTIONAL for the hold — both always a cost.

FORWARD-ONLY: first run per symbol seeds the current position at TODAY's price/ts and
books no history. Acts only on newly-CLOSED daily bars (no repaint).

    python lev_perp_paper.py        # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.state import sanitize_for_json

KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("LEV_PERP_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

SMA_N = int(os.getenv("LEV_PERP_SMA", "50"))
LEVERAGE = float(os.getenv("LEV_PERP_LEVERAGE", "3"))
TP_PRICE_FRAC = float(os.getenv("LEV_PERP_TP_PRICE_FRAC", "0.05"))   # favorable PRICE move to take profit
MAINT = float(os.getenv("LEV_PERP_MAINT", "0.05"))                   # maintenance-margin buffer
LIQ_PRICE_FRAC = max(1e-6, (1.0 - MAINT) / LEVERAGE)                 # adverse PRICE move to liquidate
TRADE_COST_FRAC = float(os.getenv("LEV_PERP_COST_FRAC", "0.0015"))  # perp taker round-trip on NOTIONAL
FUNDING_APY = float(os.getenv("LEV_PERP_FUNDING_APY", "0.10"))      # conservative funding drag on NOTIONAL
STARTING_EQUITY = float(os.getenv("LEV_PERP_START_EQUITY", "1000"))
ALLOC_FRAC = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))                     # equal margin across the universe
MARGIN = round(STARTING_EQUITY * ALLOC_FRAC, 2)                      # margin committed per position
STATE_FILE = Path(os.getenv("LEV_PERP_STATE_FILE", "data/lev_perp_state.json"))
INTERVAL_DAILY = 1440
HOURS_PER_YEAR = 24.0 * 365.0


def fetch_closed_daily(pair: str) -> list[dict]:
    """Ascending daily OHLC with the in-progress bar dropped (no repaint)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_DAILY}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(row[0]), "h": float(row[2]), "l": float(row[3]),
             "c": float(row[4])} for row in series]
    return bars[:-1]


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _target_side(close: float, sma: float) -> int:
    """+1 long above SMA, -1 short below (pure sign — direction only)."""
    return 1 if close >= sma else -1


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "leverage": LEVERAGE, "tp_price_frac": TP_PRICE_FRAC,
            "liq_price_frac": LIQ_PRICE_FRAC,
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitize_for_json(state), indent=2))
    tmp.replace(STATE_FILE)


def _open(state: dict, base: str, side: int, price: float, ts: str) -> None:
    state["positions"][base] = {
        "symbol": base, "side": side, "entry": price, "entry_ts": ts,
        "margin_usd": MARGIN, "leverage": LEVERAGE, "notional_usd": round(MARGIN * LEVERAGE, 2),
        "tp": round(price * (1 + side * TP_PRICE_FRAC), 8),
        "liq": round(price * (1 - side * LIQ_PRICE_FRAC), 8),
    }


def _funding_cost(notional: float, entry_ts: str, exit_ts: str) -> float:
    """Conservative funding drag on the LEVERED notional for the hold (always a cost)."""
    try:
        hours = (int(exit_ts) - int(entry_ts)) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return notional * FUNDING_APY * max(0.0, hours) / HOURS_PER_YEAR


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(base)
    side = pos["side"]
    notional = pos["notional_usd"]
    ret = side * (price - pos["entry"]) / pos["entry"]          # price return in trade direction
    gross = notional * ret                                       # levered P&L on the move
    cost = notional * TRADE_COST_FRAC
    funding = _funding_cost(notional, pos["entry_ts"], ts)
    net = gross - cost - funding
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "side": side, "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "margin_usd": pos["margin_usd"],
           "leverage": pos["leverage"], "notional_usd": notional,
           "funding_cost": round(funding, 4), "cost": round(cost, 4),
           "pnl": round(net, 4), "pnl_pct_margin": round(net / pos["margin_usd"] * 100, 2),
           "price_move_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def _check_exit(pos: dict, bar: dict) -> tuple[float, str] | None:
    """Did this bar's range hit take-profit or liquidation? Conservative: if both are
    touched in the same bar, liquidation is assumed to hit first (worst case)."""
    side = pos["side"]
    tp, liq = pos["tp"], pos["liq"]
    if side > 0:   # LONG: liq below, tp above
        if bar["l"] <= liq:
            return liq, "liquidation"
        if bar["h"] >= tp:
            return tp, "take_profit"
    else:          # SHORT: liq above, tp below
        if bar["h"] >= liq:
            return liq, "liquidation"
        if bar["l"] <= tp:
            return tp, "take_profit"
    return None


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    """Advance one symbol on newly-closed daily bars. First run seeds the current side
    at today's price (forward-only). Returns # of actions (opens/closes)."""
    closes = [b["c"] for b in bars]
    if len(closes) < SMA_N + 1:
        print(f"{base}: warm-up ({len(closes)}/{SMA_N + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    sma = _sma(closes, SMA_N)

    if last_t is None:                              # baseline / inception
        state["last_bar_t"][base] = latest["t"]
        side = _target_side(latest["c"], sma)
        _open(state, base, side, latest["c"], str(latest["t"]))
        print(f"{base}: SEED {'LONG' if side > 0 else 'SHORT'} {LEVERAGE:g}x @ {latest['c']:.2f} "
              f"(tp {state['positions'][base]['tp']:.2f} liq {state['positions'][base]['liq']:.2f})")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        s = _sma(closes[: idx + 1], SMA_N)
        if s is None:
            continue

        # 1) Exit an open position on take-profit / liquidation hit intrabar.
        pos = state["positions"].get(base)
        if pos:
            ex = _check_exit(pos, bar)
            if ex:
                price, reason = ex
                rec = _close(state, base, price, str(bar["t"]), reason)
                tag = "TP✅" if reason == "take_profit" else "LIQ❌"
                print(f"{base}: {tag} {'LONG' if pos['side'] > 0 else 'SHORT'} @ {price:.2f} "
                      f"net=${rec['pnl']:+.2f} ({rec['pnl_pct_margin']:+.1f}% margin)")
                acted += 1

        # 2) Flip exit: if still open against a reversed trend, realize it at the close.
        pos = state["positions"].get(base)
        want = _target_side(bar["c"], s)
        if pos and pos["side"] != want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "flip")
            print(f"{base}: FLIP-CLOSE {'LONG' if pos['side'] > 0 else 'SHORT'} @ {bar['c']:.2f} "
                  f"net=${rec['pnl']:+.2f}")
            acted += 1

        # 3) Re-enter in the trend direction whenever flat (open at the bar close).
        if not state["positions"].get(base):
            _open(state, base, want, bar["c"], str(bar["t"]))
            print(f"{base}: OPEN {'LONG' if want > 0 else 'SHORT'} {LEVERAGE:g}x @ {bar['c']:.2f}")
            acted += 1

        state["last_bar_t"][base] = bar["t"]
    return acted


def _notify(state: dict, prices: dict[str, float], acted: int) -> None:
    """Telegram snapshot of the leveraged paper book. Sends on any action, on a forced
    run, or once per NOTIFY_INTERVAL_HOURS. Best-effort; never breaks the arm."""
    if os.getenv("LEV_PERP_NOTIFY", "1") != "1":
        return
    force = os.getenv("LEV_PERP_NOTIFY_FORCE", "0") == "1"
    interval_h = float(os.getenv("LEV_PERP_NOTIFY_INTERVAL_HOURS", "24"))
    now = datetime.now(timezone.utc)
    last = state.get("last_notify_ts")
    due = True
    if last:
        try:
            due = (now - datetime.fromisoformat(last)).total_seconds() >= interval_h * 3600
        except ValueError:
            due = True
    if not (force or acted > 0 or due):
        return

    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    pnl = eq - start
    icon = "📈" if pnl >= 0 else "📉"
    unreal = 0.0
    pos_lines = []
    for base, p in state.get("positions", {}).items():
        px = prices.get(base)
        if px:
            r = p["side"] * (px - p["entry"]) / p["entry"]
            u = p["notional_usd"] * r
            unreal += u
            sd = "🟢LONG" if p["side"] > 0 else "🔴SHORT"
            pos_lines.append(f"  {base} {sd} {p['leverage']:g}x @ ${p['entry']:,.2f} "
                             f"(mark ${px:,.2f}, {r*100:+.1f}% px = ${u:+.2f}; "
                             f"tp ${p['tp']:,.2f} / liq ${p['liq']:,.2f})")
        else:
            sd = "LONG" if p["side"] > 0 else "SHORT"
            pos_lines.append(f"  {base} {sd} {p['leverage']:g}x @ ${p['entry']:,.2f}")
    closed = state.get("closed", [])
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    tps = sum(1 for c in closed if c.get("reason") == "take_profit")
    liqs = sum(1 for c in closed if c.get("reason") == "liquidation")

    lines = [
        f"{icon} <b>Leveraged Perp ({LEVERAGE:g}x) — Paper Account</b>",
        f"Equity: <b>${eq:,.2f}</b>  (start ${start:,.0f}, {pnl:+.2f})",
        f"Unrealized: <b>${unreal:+.2f}</b>   Closed: {len(closed)}"
        + (f" ({wins}W/{len(closed)-wins}L, {tps} TP / {liqs} liq)" if closed else ""),
        "<b>Open positions:</b>" if pos_lines else "No open positions.",
        *pos_lines,
    ]
    if closed:
        last_c = closed[-1]
        sd = "SHORT" if last_c.get("side", 1) < 0 else "LONG"
        lines.append(f"Last close: {last_c['symbol']} {sd} ${last_c['pnl']:+.2f} "
                     f"({last_c.get('pnl_pct_margin', 0):+.1f}% margin, {last_c.get('reason')})")
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from src.notifications import create_notifier_from_env
        notifier = create_notifier_from_env()
        # One-shot cron script, not the long-running bot — force a synchronous
        # send so the call completes before the process exits (default
        # async_safe queue's worker thread would get killed first).
        notifier._async_safe = False
        if notifier.send_message("\n".join(lines)):
            state["last_notify_ts"] = now.isoformat()
    except Exception as e:
        print(f"[lev_perp_paper] telegram notify skipped: {e}")


def main():
    state = _load_state()
    total = 0
    prices: dict[str, float] = {}
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed_daily(pair)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        if bars:
            prices[base] = bars[-1]["c"]
        total += process_symbol(base, bars, state)
    _notify(state, prices, total)
    _save_state(state)
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    held = {b: (("L" if p["side"] > 0 else "S") + f"{p['leverage']:g}x")
            for b, p in state["positions"].items()}
    print(f"[lev_perp_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) lev={LEVERAGE:g}x "
          f"margin=${MARGIN:.0f} tp={TP_PRICE_FRAC*100:.0f}% liq={LIQ_PRICE_FRAC*100:.1f}% "
          f"universe={list(KRAKEN_PAIRS)} acted={total} held={held} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()
