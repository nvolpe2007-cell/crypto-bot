#!/usr/bin/env python3
"""
Swing strategy — FORWARD paper runner (single-shot, cron-friendly).

This is the clock that earns real proof. Run it on a schedule (hourly cron is
fine — it only acts when a new 4h bar has CLOSED). On each new closed bar it
evaluates the strategy on every major, manages open paper positions
(stop / target / trend-break), opens new ones, logs every decision, and records
closed trades to data/swing_paper_state.json — which proof_scorecard.py reads.

FORWARD-ONLY by construction: on the very first run per symbol it just records
the current bar as the baseline and takes NO trade, so the live record is built
only from bars that close AFTER you start. No replaying history into the ledger
(that would just be the in-sample backtest wearing a disguise).

    python swing_paper.py        # process any newly-closed bars, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.swing_strategy import SwingStrategy, ROUND_TRIP_COST_FRAC
from src.decision_log import DecisionLog
from src.volume_profile import volume_profile, DEFAULT_BINS
from src.event_calendar import blackout_reason
from src.cot_report import cot_signal
from src.session_filter import SessionEdge, window_of_hour
from src.td_sequential import td_state
from src.state import sanitize_for_json

# Master kill switch (best-effort import — never block trading if it can't load).
try:
    from src.kill_switch import is_killed as _is_killed
except Exception:  # pragma: no cover - import-path safety net
    def _is_killed() -> bool:
        return False

# Session/time-of-day gate. Default OFF (measure-first): we tag every trade with
# its session verdict so proof_scorecard can measure per-session edge BEFORE we
# ever gate on it. Set SESSION_FILTER_HARD=1 to actually veto entries in windows
# the bot has measured itself losing money in. See src/session_filter.py.
SESSION_FILTER_HARD = os.getenv("SESSION_FILTER_HARD", "").strip().lower() in (
    "1", "true", "yes", "on")

# ── "A few good trades, day and night" cadence governors ─────────────────────
# These CAP how many NEW entries the (unchanged) 4h-majors edge takes per window;
# they never force a trade. Day = EU+US (8-23 UTC), Night = Asia (0-7 UTC). When
# more setups qualify on one bar-close than the budget allows, the highest-
# conviction ones win (see _conviction). A flat session correctly takes 0.
MAX_TRADES_DAY = int(os.getenv("SWING_MAX_TRADES_DAY", "3"))
MAX_TRADES_NIGHT = int(os.getenv("SWING_MAX_TRADES_NIGHT", "3"))
# Concurrent-position cap so the $500 paper bankroll isn't over-committed
# (7 × $62.50 ≈ $440). Override with SWING_MAX_OPEN_POSITIONS.
MAX_OPEN_POSITIONS = int(os.getenv("SWING_MAX_OPEN_POSITIONS", "7"))

# Validated liquid Kraken USD-spot majors (pair codes confirmed live against the
# OHLC endpoint). A WIDER universe is the fastest, cleanest way to reach the
# proof bar (n>=30, t>2): more uncorrelated symbols = more INDEPENDENT setups
# per unit time, with the SAME locked per-trade edge. This is data expansion,
# NOT a strategy change — swing_strategy.py is untouched. Subset the active set
# with SWING_SYMBOLS="BTC,ETH,..." (bases).
KRAKEN_PAIRS_ALL = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "ADA": "ADAUSD",
    "DOT": "DOTUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD", "LTC": "LTCUSD",
    "XRP": "XRPUSD", "ATOM": "ATOMUSD", "UNI": "UNIUSD", "BCH": "BCHUSD",
    "DOGE": "XDGUSD", "AAVE": "AAVEUSD", "FIL": "FILUSD", "ALGO": "ALGOUSD",
}
_env_syms = os.getenv("SWING_SYMBOLS", "").strip()
if _env_syms:
    _want = {s.strip().upper() for s in _env_syms.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

# Timeframes the (same, locked) strategy runs on. 4h is the original; daily
# (1440) adds longer-horizon, largely-independent samples. Each (symbol,
# timeframe) keeps its own clock + position under a namespaced state key
# ("BASE@INTERVAL"), so the two never collide. Caveat: same-symbol 4h vs daily
# trades are somewhat correlated — cross-symbol independence is the real driver
# of the t-stat, so the universe width matters more than the extra timeframe.
INTERVALS = [int(x) for x in os.getenv("SWING_INTERVALS", "240,1440").split(",")
             if x.strip()]
# State ledger path. Overridable so a wider, MEASURE-FIRST universe can forward-
# test into a SEPARATE file without polluting the canonical 6-major proof track
# (see deploy/swing_cron.txt). Defaults to the canonical ledger.
STATE_FILE = Path(os.getenv("SWING_STATE_FILE", "data/swing_paper_state.json"))

# ── Paper account ────────────────────────────────────────────────────────────
# A real $500 paper bankroll. Sizing is a FIXED fraction of the STARTING equity
# (not compounding) so every trade's $ P&L scales identically — that keeps the
# proof_scorecard t-stat clean (uniform scaling leaves it unchanged; this is
# capital allocation, NOT a strategy change, so the locked strategy stays locked).
# Per-trade size is a FIXED fraction of starting equity. With the universe now
# up to 16 symbols × 2 timeframes, 1/3 each would over-deploy, so the default
# drops to ~1/8 (≈8 concurrent positions ≈ full account). This is capital
# allocation only: per-trade expectancy and the proof t-stat are SCALE-INVARIANT
# to uniform sizing, so the locked strategy stays locked. Override with
# SWING_ALLOC_FRAC.
STARTING_EQUITY = float(os.getenv("SWING_START_EQUITY", "500"))
ALLOC_FRAC = float(os.getenv("SWING_ALLOC_FRAC", "0.125"))
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)


def fetch_closed_bars(pair: str, interval_min: int) -> list[dict]:
    """Ascending OHLC with the in-progress final interval DROPPED, so we only
    ever act on fully-closed bars (no repainting / lookahead)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval_min}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count].
    # Volume (row[6]) is kept so the volume-profile annotation has data.
    bars = [{"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
             "l": float(row[3]), "c": float(row[4]), "v": float(row[6])}
            for row in series]
    return bars[:-1]            # drop the still-forming current bar


def fetch_ticker(pair: str) -> float | None:
    """Current last-trade price for entry-fill realism (the swing cron acts
    minutes after a bar closes, so fills happen at the live price, not the
    bar's close). Returns None on any failure → caller falls back to the close."""
    try:
        url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        if data.get("error"):
            return None
        res = next(v for k, v in data["result"].items())
        return float(res["c"][0])          # 'c' = [last_price, lot_volume]
    except Exception:
        return None


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitize_for_json(state), indent=2))
    tmp.replace(STATE_FILE)


def _vp_context(window: list[dict], fill: float) -> dict:
    """Volume-profile annotation for an entry (RESEARCH ONLY — not acted on).
    Records where the fill sits in the profile so we can later MEASURE whether
    that structure separates winners from losers. See src/volume_profile.py."""
    if len(window) < 20:
        return {"vp_zone": "n/a"}
    vp = volume_profile(window, n_bins=DEFAULT_BINS)
    if vp is None:
        return {"vp_zone": "n/a"}
    return {
        "vp_zone": vp.classify(fill),
        "vp_poc": round(vp.poc, 4),
        "vp_dist_poc_pct": round((fill - vp.poc) / fill * 100.0, 3) if fill else 0.0,
        "vp_in_value": vp.val <= fill <= vp.vah,
    }


# ── attribution keys carried from an open position onto its closed record ─────
_CARRY_KEYS = ("vp_zone", "vp_dist_poc_pct", "vp_in_value", "cot_bias",
               "entry_hour", "entry_atr_pct", "entry_session", "session_verdict",
               "entry_window", "entry_day", "conviction", "td_signal",
               "td_buy_setup", "td_sell_setup", "td_buy_countdown",
               "td_sell_countdown")


def _conviction(dec) -> float:
    """Rank competing ENTER setups by trend STRENGTH so a limited window budget
    buys the best ones, not the first alphabetically. Two momentum measures the
    locked strategy already computes: 20-bar rate-of-change + how far price sits
    above the EMA50 trend line (both fractions of price). Deliberately simple and
    NOT swept — ranking is a tie-break among already-qualified setups, not a new
    edge, so it must not become another overfit knob. R:R is constant here (1.5)
    so it can't discriminate and is omitted."""
    ind = dec.indicators or {}
    roc = ind.get("roc") or 0.0
    close = ind.get("close") or 0.0
    ema_s = ind.get("ema_slow") or 0.0
    ema_gap = (close - ema_s) / close if close else 0.0
    return float(roc) + float(ema_gap)


def _manage_position_exit(key: str, base: str, tf: int, window: list[dict],
                          bar: dict, state: dict, strat: SwingStrategy,
                          dlog: DecisionLog, notify=None) -> None:
    """Exit management for an OPEN slot on one closed bar (stop / target /
    trend-break). Unchanged logic, extracted so the entry path can be deferred
    and ranked separately."""
    pos = state["positions"][key]
    label = f"{base} {tf}m"
    exit_price = exit_reason = None
    if bar["l"] <= pos["stop"]:
        # A stop is a market order: if the bar GAPS open below the stop, you fill
        # at the (worse) open, not the stop price.
        exit_price, exit_reason = min(pos["stop"], bar["o"]), "stop"
    elif bar["h"] >= pos["target"]:
        exit_price, exit_reason = pos["target"], "target"   # limit fills at limit
    else:
        dec = strat.evaluate(window, position_open=True)
        dlog.evaluation(dec)
        if dec.action == "EXIT":
            exit_price, exit_reason = bar["c"], "trend_break"
    if exit_price is None:
        return
    size = pos["size_usd"]
    ret = (exit_price - pos["entry"]) / pos["entry"]
    net = size * ret - size * ROUND_TRIP_COST_FRAC
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "tf": tf, "entry_ts": pos["entry_ts"],
           "exit_ts": str(bar["t"]), "entry": pos["entry"],
           "exit": exit_price, "size_usd": size, "pnl": net,
           "pnl_pct": ret * 100, "reason": exit_reason, "won": net > 0,
           "equity_after": round(state["equity"], 2)}
    for k in _CARRY_KEYS:                     # carry entry research context forward
        if k in pos:
            rec[k] = pos[k]
    state["closed"].append(rec)
    del state["positions"][key]
    dlog.closed(base, pos["entry_ts"], str(bar["t"]), pos["entry"],
                exit_price, size, net, ret * 100, exit_reason, 0)
    if notify:
        notify(f"SWING CLOSE {label}: {exit_reason} net=${net:+.2f} ({ret*100:+.1f}%)")


def _build_entry_candidate(key: str, base: str, tf: int, window: list[dict],
                           bar: dict, state: dict, strat: SwingStrategy,
                           dlog: DecisionLog, notify=None,
                           live_price: float | None = None, is_latest: bool = False,
                           cot_bias: str = "n/a") -> dict | None:
    """Evaluate the FLAT entry path for one closed bar. Runs the locked 5-gate
    strategy plus the hard event-blackout and (optional) hard session vetoes, and
    if a valid entry survives, returns a fully-built CANDIDATE (position dict +
    open-log payload + conviction) WITHOUT opening it. The caller ranks all
    candidates and commits the best under the window/position caps. Returns None
    when there is no entry (or it was vetoed)."""
    label = f"{base} {tf}m"
    dec = strat.evaluate(window, position_open=False)
    dlog.evaluation(dec)
    if not dec.is_enter:
        return None

    # ── event blackout: veto NEW entries near scheduled high-impact events ──
    entry_dt = datetime.fromtimestamp(bar["t"] + tf * 60, tz=timezone.utc)
    blk = blackout_reason(entry_dt)
    if blk:
        dlog._write("skip_event", {"symbol": base, "tf": tf, "ts": str(bar["t"]),
                                   "reason": f"event_blackout: {blk}"})
        if notify:
            notify(f"SWING SKIP {label}: event blackout ({blk})")
        return None

    # ── session / time-of-day edge gate (measure-first; hard only on opt-in) ──
    entry_hour = entry_dt.hour
    sess_verdict = SessionEdge.from_state(state).verdict_for_hour(entry_hour)
    if sess_verdict == "UNFAVORABLE" and SESSION_FILTER_HARD:
        sess = SessionEdge.session_of(entry_hour)
        dlog._write("skip_session", {"symbol": base, "tf": tf, "ts": str(bar["t"]),
                                     "reason": f"unfavorable session ({sess} hour {entry_hour})"})
        if notify:
            notify(f"SWING SKIP {label}: unfavorable session ({sess})")
        return None

    # ── fill realism: the latest bar fills at the live price, not the close. ──
    fill = dec.price
    if is_latest and live_price and live_price > 0:
        fill = live_price
    delta = fill - dec.price
    stop, target = dec.stop_price + delta, dec.target_price + delta

    vp_ctx = _vp_context(window, fill)          # research annotation, never gated
    # TD Sequential state at entry — ANNOTATION ONLY (never gates). Computed from
    # the bars already in hand; proof_scorecard breaks P&L down "by TD signal" so
    # the ledger can prove whether TD alignment adds edge before it's ever a gate.
    td = td_state(window)
    entry_atr_pct = (round(dec.target_pct / strat.atr_target_mult * 100.0, 3)
                     if strat.atr_target_mult else 0.0)
    conviction = _conviction(dec)
    position = {
        "symbol": base, "tf": tf,
        "entry": fill, "stop": stop, "target": target,
        "entry_ts": str(bar["t"]), "size_usd": TRADE_SIZE, "rr": dec.rr,
        "cot_bias": cot_bias, "entry_hour": entry_hour,
        "entry_atr_pct": entry_atr_pct,
        "entry_session": SessionEdge.session_of(entry_hour),
        "session_verdict": sess_verdict,
        "entry_window": window_of_hour(entry_hour),
        "entry_day": entry_dt.strftime("%Y-%m-%d"),
        "conviction": round(conviction, 6),
        "td_signal": td["signal"], "td_buy_setup": td["buy_setup"],
        "td_sell_setup": td["sell_setup"], "td_buy_countdown": td["buy_countdown"],
        "td_sell_countdown": td["sell_countdown"], **vp_ctx}
    return {"key": key, "base": base, "tf": tf, "entry_dt": entry_dt,
            "conviction": conviction, "position": position,
            "fill": fill, "signal_close": dec.price, "delta": delta,
            "stop": stop, "target": target, "rr": dec.rr, "reason": dec.reason,
            "vp_ctx": vp_ctx}


def _window_opens_today(state: dict, day_str: str, window: str) -> int:
    """How many entries have ALREADY been initiated in this UTC date + window
    (counts both still-open positions and trades already closed today). This is
    the per-window budget meter — it caps NEW entries, never forces them."""
    cnt = 0
    for p in list(state["positions"].values()) + state.get("closed", []):
        if p.get("entry_day") == day_str and p.get("entry_window") == window:
            cnt += 1
    return cnt


def _commit_entry(cand: dict, state: dict, dlog: DecisionLog, notify=None) -> bool:
    """Open a ranked candidate IF it fits the caps: concurrent-position cap first,
    then the per-window (day/night) trade budget. Skips (with a logged reason) are
    the cap doing its job — never a forced trade. Returns True if opened."""
    base, tf, key = cand["base"], cand["tf"], cand["key"]
    label = f"{base} {tf}m"

    if _is_killed():
        dlog._write("skip_kill_switch", {"symbol": base, "tf": tf,
                    "ts": cand["position"]["entry_ts"],
                    "reason": "master kill switch engaged"})
        return False

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        dlog._write("skip_max_positions", {"symbol": base, "tf": tf,
                    "ts": cand["position"]["entry_ts"],
                    "reason": f"at max open positions ({MAX_OPEN_POSITIONS})"})
        return False

    entry_dt = cand["entry_dt"]
    window = window_of_hour(entry_dt.hour)
    day_str = entry_dt.strftime("%Y-%m-%d")
    budget = MAX_TRADES_NIGHT if window == "night" else MAX_TRADES_DAY
    if _window_opens_today(state, day_str, window) >= budget:
        dlog._write("skip_window_budget", {"symbol": base, "tf": tf,
                    "ts": cand["position"]["entry_ts"],
                    "reason": f"{window} budget full ({budget}/window) on {day_str}"})
        if notify:
            notify(f"SWING SKIP {label}: {window} budget full ({budget})")
        return False

    state["positions"][key] = cand["position"]
    dlog.opened(base, cand["position"]["entry_ts"], cand["fill"], TRADE_SIZE,
                cand["stop"], cand["target"], cand["rr"], cand["reason"])
    dlog._write("open_context", {"symbol": base, "tf": tf,
                "ts": cand["position"]["entry_ts"], "fill": cand["fill"],
                "signal_close": cand["signal_close"], **cand["vp_ctx"]})
    if notify:
        slip = (f" (filled {cand['fill']:.2f} vs close {cand['signal_close']:.2f})"
                if cand["delta"] else "")
        notify(f"SWING OPEN {label} @ {cand['fill']:.2f} stop {cand['stop']:.2f} "
               f"target {cand['target']:.2f} (R:R {cand['rr']:.1f}, "
               f"{cand['vp_ctx']['vp_zone']}, conv {cand['conviction']:+.3f}){slip} "
               f"— {cand['reason']}")
    return True


def process_symbol(key: str, base: str, tf: int, closed_bars: list[dict],
                   state: dict, strat: SwingStrategy, dlog: DecisionLog,
                   notify=None, live_price: float | None = None,
                   cot_bias: str = "n/a") -> tuple[int, dict | None]:
    """Process every newly-closed bar since last run for one (symbol, timeframe)
    slot. Manages exits + advances the slot clock INLINE (unchanged, forward-
    only); for the FLAT entry path it returns the freshest entry CANDIDATE (or
    None) for the caller to rank and commit under the caps. First-ever run per
    slot only sets the baseline (no replay). Returns (#bars processed, candidate)."""
    if not closed_bars:
        return 0, None
    last_t = state["last_bar_t"].get(key)
    if last_t is None:                       # baseline: start the clock from now
        state["last_bar_t"][key] = closed_bars[-1]["t"]
        return 0, None
    new = [b for b in closed_bars if b["t"] > last_t]
    candidate = None
    for i, bar in enumerate(new):
        idx = closed_bars.index(bar)
        win = closed_bars[: idx + 1]
        for b in win:
            b["symbol"] = base
        if state["positions"].get(key):
            _manage_position_exit(key, base, tf, win, bar, state, strat, dlog, notify)
        else:
            cand = _build_entry_candidate(
                key, base, tf, win, bar, state, strat, dlog, notify,
                live_price=live_price, is_latest=(i == len(new) - 1), cot_bias=cot_bias)
            if cand is not None:
                candidate = cand             # keep the freshest signal for this slot
        state["last_bar_t"][key] = bar["t"]
    return len(new), candidate


def main():
    strat = SwingStrategy()
    dlog = DecisionLog(path=Path("data/swing_decisions.jsonl"))
    state = _load_state()

    # COT macro context (research): logged once per run and stamped on each entry
    # so we can later MEASURE whether leveraged-fund extremes skew swing outcomes.
    # Best-effort — never blocks the run.
    cot = cot_signal()
    cot_bias = cot.bias if cot else "n/a"
    if cot:
        dlog._write("macro_context", {
            "source": "COT-CME-BTC", "date": cot.date, "lev_net": cot.lev_net,
            "lev_net_pctile": cot.lev_net_pctile, "asset_mgr_net": cot.asset_mgr_net,
            "extreme": cot.extreme, "bias": cot.bias})

    # ── Phase 1: collect ──────────────────────────────────────────────────────
    # Manage exits + advance clocks inline; gather flat-path entry CANDIDATES
    # across the whole universe so the window budget can buy the BEST ones.
    total_new = 0
    candidates: list[dict] = []
    for base, pair in KRAKEN_PAIRS.items():
        # One live-price read per symbol for entry-fill realism (shared across
        # this symbol's timeframes). None on failure → fills fall back to close.
        live_price = fetch_ticker(pair)
        for tf in INTERVALS:
            key = f"{base}@{tf}"
            try:
                bars = fetch_closed_bars(pair, tf)
            except Exception as e:
                print(f"{key}: fetch failed - {e}")
                continue
            n, cand = process_symbol(key, base, tf, bars, state, strat, dlog,
                                     live_price=live_price, cot_bias=cot_bias)
            total_new += n
            if cand is not None:
                candidates.append(cand)

    # ── Phase 2: rank + commit under caps ─────────────────────────────────────
    # Highest conviction first, so a binding window/position cap keeps the best
    # setups. Each commit re-checks the live caps (earlier opens count).
    candidates.sort(key=lambda c: c["conviction"], reverse=True)
    opened = sum(_commit_entry(c, state, dlog, notify=None) for c in candidates)

    _save_state(state)
    open_n = len(state["positions"])
    closed_n = len(state["closed"])
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    print(f"[swing_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"trade_size=${TRADE_SIZE:.0f} universe={len(KRAKEN_PAIRS)}x{len(INTERVALS)}tf "
          f"new_bars={total_new} candidates={len(candidates)} opened={opened} "
          f"open={open_n}/{MAX_OPEN_POSITIONS} closed={closed_n} "
          f"budget=day{MAX_TRADES_DAY}/night{MAX_TRADES_NIGHT} "
          f"open_slots={list(state['positions'])}")


if __name__ == "__main__":
    main()
