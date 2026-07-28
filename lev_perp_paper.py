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

ENTRY FILTERS (pattern-tested 2026-06-30, all gate-able via env vars):
  * RSI < LEV_PERP_RSI_MAX (default 45): avoid crowded overbought/oversold entries.
    Pattern: RSI<45 at entry → +$17-18/trade vs +$0-4 at RSI>65.
  * Trend age >= LEV_PERP_MIN_TREND_AGE days (default 8): skip the first week of a
    new trend — fresh trends have 53% TP rate vs 87% for mature trends (8+ days).
  * Volume > LEV_PERP_VOL_MULT x 20-day avg (default 1.2x): high volume confirms
    trend continuation. >1.5x vol → 78% TP rate vs 47% for low volume.
  * Skip ADX 20-30 zone (LEV_PERP_SKIP_ADX_DEAD_ZONE=1): the 20-30 ADX range is
    "false trend" territory — negative EV (-$8.59/trade) vs positive outside it.
  Set any filter's env var to 0/off to disable it individually.

RISK CONTROLS (added 2026-07-01 after a 410-day BTC hourly backtest of this arm's
own config — fixed 3x lost -41% over a full cycle; the SOL flip-close of 2026-06-30
ran a -14.4% move into a -44%-of-margin loss because nothing capped it):
  * HARD STOP at LEV_PERP_SL_PRICE_FRAC adverse PRICE move (default 5%, symmetric
    with the TP). Checked intrabar. Conservative ordering: liquidation > stop > TP
    when one bar touches several levels.
  * VOL-TARGETED LEVERAGE: effective leverage = min(LEV_PERP_LEVERAGE,
    LEV_PERP_VOL_TARGET / realized 20d daily vol %). Default target 2.0%/day. The
    backtest's only profitable configs averaged ~1.1-1.2x effective; the 3x env cap
    stays as the ceiling, vol decides the actual number per entry.
  * CORRELATION CAP: BTC/ETH/SOL same-direction positions are ~one bet. New entries'
    margin is divided by (1 + open same-direction count).
  * NEWS HALT: if LEV_PERP_NEWS_HALT_FILE (default data/news_halt.json) contains
    {"until": <epoch>} in the future, NO new entries are opened (exits still run).
    The news tracker (or the owner, by hand) writes that file on CRITICAL events.
  Set LEV_PERP_SL_PRICE_FRAC=0 / LEV_PERP_VOL_TARGET=0 to disable those two.

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

KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("LEV_PERP_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

SMA_N           = int(os.getenv("LEV_PERP_SMA", "50"))
LEVERAGE        = float(os.getenv("LEV_PERP_LEVERAGE", "3"))
TP_PRICE_FRAC   = float(os.getenv("LEV_PERP_TP_PRICE_FRAC", "0.05"))
MAINT           = float(os.getenv("LEV_PERP_MAINT", "0.05"))
LIQ_PRICE_FRAC  = max(1e-6, (1.0 - MAINT) / LEVERAGE)
TRADE_COST_FRAC = float(os.getenv("LEV_PERP_COST_FRAC", "0.0015"))
FUNDING_APY     = float(os.getenv("LEV_PERP_FUNDING_APY", "0.10"))
STARTING_EQUITY = float(os.getenv("LEV_PERP_START_EQUITY", "1000"))
ALLOC_FRAC      = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))
MARGIN          = round(STARTING_EQUITY * ALLOC_FRAC, 2)
STATE_FILE      = Path(os.getenv("LEV_PERP_STATE_FILE", "data/lev_perp_state.json"))

# ── Entry filter parameters ───────────────────────────────────────────────────
RSI_PERIOD          = int(os.getenv("LEV_PERP_RSI_PERIOD", "14"))
RSI_MAX             = float(os.getenv("LEV_PERP_RSI_MAX", "45"))      # skip entries when RSI >= this
MIN_TREND_AGE       = int(os.getenv("LEV_PERP_MIN_TREND_AGE", "8"))   # min days in current trend before entry
VOL_MULT            = float(os.getenv("LEV_PERP_VOL_MULT", "1.2"))    # min volume vs 20-day avg
VOL_LOOKBACK        = int(os.getenv("LEV_PERP_VOL_LOOKBACK", "20"))   # days for avg volume
SKIP_ADX_DEAD_ZONE  = os.getenv("LEV_PERP_SKIP_ADX_DEAD_ZONE", "1") == "1"
ADX_DEAD_LOW        = float(os.getenv("LEV_PERP_ADX_DEAD_LOW", "20"))
ADX_DEAD_HIGH       = float(os.getenv("LEV_PERP_ADX_DEAD_HIGH", "30"))
ADX_PERIOD          = int(os.getenv("LEV_PERP_ADX_PERIOD", "14"))

# ── Risk-control parameters ───────────────────────────────────────────────────
SL_PRICE_FRAC   = float(os.getenv("LEV_PERP_SL_PRICE_FRAC", "0.05"))   # hard stop; 0 disables
VOL_TARGET      = float(os.getenv("LEV_PERP_VOL_TARGET", "2.0"))      # %/day; 0 disables sizing
VOL_N           = int(os.getenv("LEV_PERP_VOL_N", "20"))              # realized-vol lookback (days)
NEWS_HALT_FILE  = Path(os.getenv("LEV_PERP_NEWS_HALT_FILE", "data/news_halt.json"))

INTERVAL_DAILY  = 1440
HOURS_PER_YEAR  = 24.0 * 365.0


def fetch_closed_daily(pair: str) -> list[dict]:
    """Ascending daily OHLC+volume with the in-progress bar dropped (no repaint)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_DAILY}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(row[0]), "h": float(row[2]), "l": float(row[3]),
             "c": float(row[4]), "v": float(row[6])} for row in series]
    return bars[:-1]


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    import math
    deltas = [closes[i] - closes[i-1] for i in range(len(closes)-n, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _adx(bars: list[dict], n: int = 14) -> float | None:
    """Approximate ADX over the last n*2 bars."""
    if len(bars) < n + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        h, l   = bars[i]["h"], bars[i]["l"]
        ph, pl = bars[i-1]["h"], bars[i-1]["l"]
        pc     = bars[i-1]["c"]
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    atr_n    = sum(trs[-n:]) / n
    if atr_n == 0:
        return 0.0
    plus_di  = 100.0 * (sum(plus_dm[-n:]) / n) / atr_n
    minus_di = 100.0 * (sum(minus_dm[-n:]) / n) / atr_n
    denom    = plus_di + minus_di
    return 100.0 * abs(plus_di - minus_di) / denom if denom else 0.0


def _trend_age(closes: list[float], sma_n: int) -> int:
    """How many consecutive bars has the current trend (close vs SMA) been in place?"""
    if len(closes) < sma_n + 1:
        return 0
    current_side = 1 if closes[-1] > (sum(closes[-sma_n:]) / sma_n) else -1
    age = 0
    for i in range(len(closes) - 1, sma_n - 1, -1):
        s = sum(closes[i-sma_n:i]) / sma_n
        side = 1 if closes[i] > s else -1
        if side != current_side:
            break
        age += 1
    return age


def _vol_ratio(bars: list[dict], lookback: int) -> float | None:
    """Current bar volume vs N-day average."""
    if len(bars) < lookback + 1:
        return None
    avg = sum(b["v"] for b in bars[-(lookback+1):-1]) / lookback
    return bars[-1]["v"] / avg if avg > 0 else None


def _entry_filter(bars: list[dict], closes: list[float], reason_out: list) -> bool:
    """Returns True if OK to enter, False if filtered out. Appends skip reason."""
    # RSI filter
    rsi_val = _rsi(closes, RSI_PERIOD)
    if rsi_val is not None and rsi_val >= RSI_MAX:
        reason_out.append(f"RSI={rsi_val:.1f}>={RSI_MAX}")
        return False

    # Trend age filter
    age = _trend_age(closes, SMA_N)
    if age < MIN_TREND_AGE:
        reason_out.append(f"trend_age={age}<{MIN_TREND_AGE}")
        return False

    # Volume filter
    vr = _vol_ratio(bars, VOL_LOOKBACK)
    if vr is not None and vr < VOL_MULT:
        reason_out.append(f"vol_ratio={vr:.2f}<{VOL_MULT}")
        return False

    # ADX dead-zone filter
    if SKIP_ADX_DEAD_ZONE:
        adx_val = _adx(bars[-(ADX_PERIOD*2+1):], ADX_PERIOD)
        if adx_val is not None and ADX_DEAD_LOW <= adx_val <= ADX_DEAD_HIGH:
            reason_out.append(f"ADX={adx_val:.1f} in dead-zone [{ADX_DEAD_LOW},{ADX_DEAD_HIGH}]")
            return False

    return True


def _target_side(close: float, sma: float) -> int:
    return 1 if close >= sma else -1


def _realized_vol(closes: list[float], n: int = 20) -> float | None:
    """Realized daily vol in %, population stdev of the last n daily returns."""
    if len(closes) < n + 1:
        return None
    rets = [(closes[i] / closes[i-1] - 1) * 100 for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    return (sum((r - mean) ** 2 for r in rets) / n) ** 0.5


def _effective_leverage(closes: list[float]) -> float:
    """Vol-targeted leverage: min(cap, target daily vol / realized vol)."""
    if VOL_TARGET <= 0:
        return LEVERAGE
    vol = _realized_vol(closes, VOL_N)
    if not vol:
        return LEVERAGE
    return max(0.1, min(LEVERAGE, VOL_TARGET / vol))


def _entry_margin(state: dict, side: int) -> float:
    """Correlation cap: same-direction majors are ~one bet; split the margin."""
    same = sum(1 for p in state["positions"].values() if p["side"] == side)
    return round(MARGIN / (1 + same), 2)


def _news_halted() -> bool:
    """True while NEWS_HALT_FILE's {"until": epoch} is in the future."""
    try:
        until = float(json.loads(NEWS_HALT_FILE.read_text()).get("until", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return datetime.now(timezone.utc).timestamp() < until


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
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _open(state: dict, base: str, side: int, price: float, ts: str,
          closes: list[float] | None = None) -> None:
    lev = _effective_leverage(closes or [])
    margin = _entry_margin(state, side)
    liq_frac = max(1e-6, (1.0 - MAINT) / lev)
    pos = {
        "symbol": base, "side": side, "entry": price, "entry_ts": ts,
        "margin_usd": margin, "leverage": round(lev, 2),
        "notional_usd": round(margin * lev, 2),
        "tp":  round(price * (1 + side * TP_PRICE_FRAC), 8),
        "liq": round(price * (1 - side * liq_frac), 8),
    }
    if SL_PRICE_FRAC > 0:
        pos["stop"] = round(price * (1 - side * SL_PRICE_FRAC), 8)
    state["positions"][base] = pos


def _funding_cost(notional: float, entry_ts: str, exit_ts: str) -> float:
    try:
        hours = (int(exit_ts) - int(entry_ts)) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return notional * FUNDING_APY * max(0.0, hours) / HOURS_PER_YEAR


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos     = state["positions"].pop(base)
    side    = pos["side"]
    notional = pos["notional_usd"]
    ret     = side * (price - pos["entry"]) / pos["entry"]
    gross   = notional * ret
    cost    = notional * TRADE_COST_FRAC
    funding = _funding_cost(notional, pos["entry_ts"], ts)
    net     = gross - cost - funding
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
    """Conservative intrabar ordering: liquidation > stop > take-profit.
    Positions opened before the stop existed get one synthesized from entry."""
    side = pos["side"]
    tp, liq = pos["tp"], pos["liq"]
    stop = pos.get("stop")
    if stop is None and SL_PRICE_FRAC > 0:
        stop = round(pos["entry"] * (1 - side * SL_PRICE_FRAC), 8)
    if side > 0:
        if bar["l"] <= liq:
            return liq, "liquidation"
        if stop is not None and bar["l"] <= stop:
            return stop, "stop_loss"
        if bar["h"] >= tp:
            return tp, "take_profit"
    else:
        if bar["h"] >= liq:
            return liq, "liquidation"
        if stop is not None and bar["h"] >= stop:
            return stop, "stop_loss"
        if bar["l"] <= tp:
            return tp, "take_profit"
    return None


def process_symbol(base: str, bars: list[dict], state: dict, prices: dict | None = None) -> int:
    closes = [b["c"] for b in bars]
    if len(closes) < SMA_N + 1:
        print(f"{base}: warm-up ({len(closes)}/{SMA_N + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    sma = _sma(closes, SMA_N)

    if last_t is None:
        state["last_bar_t"][base] = latest["t"]
        # Apply entry filters even at seed time
        skip_reasons: list = []
        if _news_halted():
            print(f"{base}: SEED SKIPPED — news halt active")
        elif _entry_filter(bars, closes, skip_reasons):
            side = _target_side(latest["c"], sma)
            _open(state, base, side, latest["c"], str(latest["t"]), closes)
            p = state["positions"][base]
            print(f"{base}: SEED {'LONG' if side > 0 else 'SHORT'} {p['leverage']:g}x @ {latest['c']:.2f} "
                  f"(tp {p['tp']:.2f} stop {p.get('stop', 0):.2f} liq {p['liq']:.2f})")
        else:
            print(f"{base}: SEED SKIPPED — filters: {', '.join(skip_reasons)}")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        s = _sma(closes[: idx + 1], SMA_N)
        if s is None:
            continue

        # 1) Exit open position on TP / liquidation
        pos = state["positions"].get(base)
        if pos:
            ex = _check_exit(pos, bar)
            if ex:
                price, reason = ex
                rec = _close(state, base, price, str(bar["t"]), reason)
                tag = {"take_profit": "TP✅", "stop_loss": "STOP🛑"}.get(reason, "LIQ❌")
                print(f"{base}: {tag} {'LONG' if pos['side'] > 0 else 'SHORT'} @ {price:.2f} "
                      f"net=${rec['pnl']:+.2f} ({rec['pnl_pct_margin']:+.1f}% margin)")
                _notify_trade_close(rec, state, prices or {})
                acted += 1

        # 2) Flip exit: trend reversed, close at bar close
        pos = state["positions"].get(base)
        want = _target_side(bar["c"], s)
        if pos and pos["side"] != want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "flip")
            print(f"{base}: FLIP-CLOSE {'LONG' if pos['side'] > 0 else 'SHORT'} @ {bar['c']:.2f} "
                  f"net=${rec['pnl']:+.2f}")
            _notify_trade_close(rec, state, prices or {})
            acted += 1

        # 3) Re-enter only if filters pass
        if not state["positions"].get(base):
            skip_reasons: list = []
            bars_to_idx = bars[: idx + 1]
            closes_to_idx = closes[: idx + 1]
            if _news_halted():
                print(f"{base}: SKIP entry — news halt active")
            elif _entry_filter(bars_to_idx, closes_to_idx, skip_reasons):
                _open(state, base, want, bar["c"], str(bar["t"]), closes_to_idx)
                p = state["positions"][base]
                print(f"{base}: OPEN {'LONG' if want > 0 else 'SHORT'} {p['leverage']:g}x "
                      f"@ {bar['c']:.2f} (margin ${p['margin_usd']:.0f})")
                acted += 1
            else:
                print(f"{base}: SKIP entry — {', '.join(skip_reasons)}")

        state["last_bar_t"][base] = bar["t"]
    return acted


def _send_telegram(msg: str, state: dict) -> None:
    """Best-effort Telegram send. Updates last_notify_ts on success."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from src.notifications import create_notifier_from_env
        notifier = create_notifier_from_env()
        notifier._async_safe = False
        if notifier.send_message(msg):
            state["last_notify_ts"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        print(f"[lev_perp_paper] telegram notify skipped: {e}")


def _notify_trade_close(rec: dict, state: dict, prices: dict[str, float]) -> None:
    """Fire a Telegram message immediately when a trade closes."""
    if os.getenv("LEV_PERP_NOTIFY", "1") != "1":
        return

    eq     = state.get("equity", STARTING_EQUITY)
    start  = state.get("starting_equity", STARTING_EQUITY)
    total_pnl = eq - start
    closed = state.get("closed", [])
    wins   = sum(1 for c in closed if c.get("pnl", 0) > 0)
    tps    = sum(1 for c in closed if c.get("reason") == "take_profit")
    liqs   = sum(1 for c in closed if c.get("reason") == "liquidation")
    n      = len(closed)

    pnl    = rec["pnl"]
    reason = rec["reason"]
    base   = rec["symbol"]
    side   = "SHORT" if rec["side"] < 0 else "LONG"
    pct    = rec["pnl_pct_margin"]

    if reason == "take_profit":
        trade_icon = "✅ TAKE PROFIT"
    elif reason == "liquidation":
        trade_icon = "💀 LIQUIDATED"
    elif reason == "stop_loss":
        trade_icon = "🛑 STOP LOSS"
    else:
        trade_icon = "🔄 TREND FLIP"

    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    win_rate = f"{wins/n*100:.0f}%" if n else "—"

    # Open positions status
    pos_lines = []
    unreal = 0.0
    for b, p in state.get("positions", {}).items():
        px = prices.get(b)
        if px:
            r = p["side"] * (px - p["entry"]) / p["entry"]
            u = p["notional_usd"] * r
            unreal += u
            arrow = "🟢" if u >= 0 else "🔴"
            sd = "L" if p["side"] > 0 else "S"
            pos_lines.append(f"{arrow} {b} {sd}{p['leverage']:g}x  ${px:,.2f}  {r*100:+.1f}%  ${u:+.2f}")

    lines = [
        f"<b>{trade_icon}</b>",
        f"",
        f"<b>{base}</b> {side} {LEVERAGE:g}x",
        f"Entry: ${rec['entry']:,.2f}  →  Exit: ${rec['exit']:,.2f}",
        f"P&L: <b>{pnl_str}</b>  ({pct_str} on margin)",
        f"",
        f"━━━━━━━━━━━━━━━━━━",
        f"💰 <b>Account</b>",
        f"Equity:   <b>${eq:,.2f}</b>",
        f"Total P&L: <b>{total_str}</b>  ({total_pnl/start*100:+.1f}%)",
        f"Record:   {wins}W / {n-wins}L  (WR {win_rate})  |  {tps} TP / {liqs} liq",
        f"Unrealized: ${unreal:+.2f}",
        f"",
        f"━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Open Positions</b>",
        *(["\n".join(pos_lines)] if pos_lines else ["None — waiting for filters"]),
    ]

    _send_telegram("\n".join(lines), state)


def main():
    state  = _load_state()
    total  = 0
    prices: dict[str, float] = {}
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed_daily(pair)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        if bars:
            prices[base] = bars[-1]["c"]
        total += process_symbol(base, bars, state, prices)
    _save_state(state)
    eq    = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    held  = {b: (("L" if p["side"] > 0 else "S") + f"{p['leverage']:g}x")
             for b, p in state["positions"].items()}
    print(f"[lev_perp_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) lev={LEVERAGE:g}x "
          f"margin=${MARGIN:.0f} tp={TP_PRICE_FRAC*100:.0f}% liq={LIQ_PRICE_FRAC*100:.1f}% "
          f"filters=RSI<{RSI_MAX}|age≥{MIN_TREND_AGE}d|vol≥{VOL_MULT}x|adx_dead={'on' if SKIP_ADX_DEAD_ZONE else 'off'} "
          f"universe={list(KRAKEN_PAIRS)} acted={total} held={held} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()
