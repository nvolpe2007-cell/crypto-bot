#!/usr/bin/env python3
"""
Lev-perp v1: MA10/40-cross entry x 8-coin universe x v1's OWN fixed-TP exit.

PRE-REGISTERED, k=1. This is not a new idea invented this session -- it is the
standing follow-on written down at the end of
`RESEARCH_2026-07-26_lev_perp_v1_frequency.md` ("Standing follow-on"):

    "DSR 0.887 at 8 coins is close enough to the 0.95 bar that it's worth
     checking, next time this comes up, whether combining this session's
     universe widening WITH PR #85's better entry signal (MA10/40-cross,
     which already beat raw SMA-50 on Sharpe in the 3-coin round) clears the
     bar outright -- that specific combination (MA10/40-cross entry x 8-coin
     universe x v1's own fixed-TP exit, as opposed to v2's chandelier) has
     not been tested yet."

Because the hypothesis was named in writing BEFORE this run, this is judged as
ONE candidate (k=1 -> Sidak t-bar 2.00), not as a sweep. That is the whole
point: `proof_scorecard._family_t_bar`'s own docstring warns that a t>2 read
off the BEST of k strategies is far weaker evidence than a pre-registered t>2.
If this fails, the honest move is to record the null -- NOT to start varying
the fast/slow MA lengths, which would silently push k up and raise the bar.

WHY THIS COMBINATION IS GENUINELY UNTESTED
  - PR #85 `lev_perp_universe_research.py` ran MA10/40-cross on 8 coins, but
    with **v2's chandelier/ATR-trailing exit** (`v2mod._check_exit`).
  - PR #87 `lev_perp_v1_universe_research.py` ran **v1's fixed-TP exit** on 8
    coins, but with **v1's own SMA-50 direction signal**.
  - Nobody has crossed the better signal with the better exit.

METHOD -- production code reused UNMODIFIED
  Exactly one thing changes versus `lev_perp_v1_universe_research.py`: the
  direction decision. Everything downstream is the real production machinery,
  imported and called, not reimplemented:
      lp._entry_filter        RSI / trend-age / volume / ADX quality gates
      lp._effective_leverage  vol-targeted leverage
      lp._open / lp._close    margin, correlation cap, fees, funding accrual
      lp._check_exit          liquidation > stop > take-profit ordering
  The per-symbol walk mirrors `lp.process_symbol`'s three-step bar loop
  (exit -> flip -> re-enter) so the comparison is apples-to-apples. The
  live-only `_news_halted()` branch and the print/notify side effects are the
  only omissions.

CONTROL
  The SMA-50 8-coin arm is re-run through this SAME loop. It is a harness
  validation, not a second candidate: it must reproduce PR #87's published
  numbers (856 trades, 52.8% WR, DSR 0.887). If it does not, the harness is
  wrong and the MA-cross number means nothing.

CLUSTERING
  Judged under BOTH dependence structures, and required to clear under BOTH:
    - by symbol  (matches PR #87's published 0.887 -- cross-sectional)
    - by week    (captures the time dependence of crypto moving together)
  Requiring both is deliberately stricter than picking the friendlier one.

SAFETY: read-only. Never touches data/lev_perp_state.json or the live book.
"""
from __future__ import annotations

import builtins
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lev_perp_paper as lp  # noqa: E402
from proof_scorecard import (  # noqa: E402
    _stats, _family_t_bar, _deflated_sharpe, DSR_MIN, N_MIN,
)
from scripts.lev_perp_entry_signal_research import sig_ma_cross  # noqa: E402

CACHE = ROOT / "data" / "research_cache_lev_perp_universe8.json"
OUT = ROOT / "data" / "lev_perp_v1_macross_universe_results.json"
UNIVERSE_8 = ["BTC", "ETH", "SOL", "ADA", "XRP", "DOT", "AVAX", "LINK"]
WARMUP = lp.SMA_N + 5  # same as PR #87, so both signals see identical history


def week_cluster(ts: str) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


# ── direction signals ────────────────────────────────────────────────────────
def sig_sma50(closes: list[float]) -> int | None:
    """v1's production direction signal, expressed as a closes->side function."""
    s = lp._sma(closes, lp.SMA_N)
    if s is None:
        return None
    return lp._target_side(closes[-1], s)


def sig_macross(closes: list[float]) -> int | None:
    """PR #85's MA10/40 cross — the only thing that differs in the candidate."""
    return sig_ma_cross(closes)


# ── harness ──────────────────────────────────────────────────────────────────
def run(bars_by_symbol: dict[str, list[dict]], symbols: list[str],
        signal_fn, label: str) -> dict:
    """Mirror of lp.process_symbol's bar loop with the direction signal swapped."""
    universe = {b: bars_by_symbol[b] for b in symbols if b in bars_by_symbol}
    n = len(universe)
    lp.MARGIN = round(lp.STARTING_EQUITY / max(1, n), 2)  # fair n-symbol split

    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "leverage": lp.LEVERAGE, "tp_price_frac": lp.TP_PRICE_FRAC,
             "liq_price_frac": lp.LIQ_PRICE_FRAC,
             "starting_equity": lp.STARTING_EQUITY, "equity": lp.STARTING_EQUITY,
             "started_at": ""}

    for base, bars in universe.items():
        if len(bars) < WARMUP + 10:
            continue
        closes_full = [b["c"] for b in bars]
        # Start one bar past the seed point, matching PR #87's seeding of
        # last_bar_t at bars[WARMUP] (process_symbol walks bars AFTER it).
        for idx in range(WARMUP + 1, len(bars)):
            bar = bars[idx]
            closes_to_idx = closes_full[: idx + 1]
            bars_to_idx = bars[: idx + 1]

            want = signal_fn(closes_to_idx)
            if want is None:
                continue

            # 1) exit on liquidation / stop / take-profit
            pos = state["positions"].get(base)
            if pos:
                ex = lp._check_exit(pos, bar)
                if ex:
                    price, reason = ex
                    lp._close(state, base, price, str(bar["t"]), reason)

            # 2) flip exit: direction reversed -> close at bar close
            pos = state["positions"].get(base)
            if pos and pos["side"] != want:
                lp._close(state, base, bar["c"], str(bar["t"]), "flip")

            # 3) re-enter only if the production quality gates pass
            if not state["positions"].get(base):
                skip: list = []
                if lp._entry_filter(bars_to_idx, closes_to_idx, skip):
                    lp._open(state, base, want, bar["c"], str(bar["t"]), closes_to_idx)

    return {"label": label, "closed": state["closed"], "equity": state["equity"]}


def judge(result: dict, t_bar: float) -> dict:
    nets = [c["pnl"] for c in result["closed"]]
    by_sym = [c["symbol"] for c in result["closed"]]
    by_week = [week_cluster(c["entry_ts"]) for c in result["closed"]]

    s_sym = _stats(nets, by_sym)
    s_week = _stats(nets, by_week)
    dsr_sym = _deflated_sharpe(s_sym["sharpe"], s_sym["eff_n"], s_sym["skew"],
                               s_sym["kurt"], sr0=0.0)
    dsr_week = _deflated_sharpe(s_week["sharpe"], s_week["eff_n"], s_week["skew"],
                                s_week["kurt"], sr0=0.0)

    # split-half robustness (same check PR #85 used)
    ordered = sorted(result["closed"], key=lambda c: int(c["entry_ts"]))
    half = len(ordered) // 2
    h1 = sum(c["pnl"] for c in ordered[:half]) if half else 0.0
    h2 = sum(c["pnl"] for c in ordered[half:]) if half else 0.0

    base_ok = s_sym["n"] >= N_MIN and s_sym["expectancy"] > 0
    clears = (base_ok
              and s_sym["t_clustered"] >= t_bar and dsr_sym > DSR_MIN
              and s_week["t_clustered"] >= t_bar and dsr_week > DSR_MIN)

    reasons = []
    for tag, st, dsr in (("symbol", s_sym, dsr_sym), ("week", s_week, dsr_week)):
        if st["t_clustered"] < t_bar:
            reasons.append(f"t_{tag}={st['t_clustered']:.2f}<{t_bar:.2f}")
        if dsr <= DSR_MIN:
            reasons.append(f"DSR_{tag}={dsr:.3f}<={DSR_MIN}")
    if s_sym["n"] < N_MIN:
        reasons.append(f"n={s_sym['n']}<{N_MIN}")
    if s_sym["expectancy"] <= 0:
        reasons.append(f"expectancy={s_sym['expectancy']:+.4f}<=0")

    by_reason: dict[str, int] = {}
    for c in result["closed"]:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    per_sym: dict[str, float] = {}
    for c in result["closed"]:
        per_sym[c["symbol"]] = round(per_sym.get(c["symbol"], 0.0) + c["pnl"], 2)

    return {
        "label": result["label"], "n": s_sym["n"], "total": s_sym["total"],
        "win_rate": s_sym["win_rate"], "expectancy": s_sym["expectancy"],
        "sharpe": s_sym["sharpe"], "max_dd": s_sym.get("max_dd"),
        "t_symbol": s_sym["t_clustered"], "eff_n_symbol": s_sym["eff_n"], "dsr_symbol": dsr_sym,
        "t_week": s_week["t_clustered"], "eff_n_week": s_week["eff_n"], "dsr_week": dsr_week,
        "half1": h1, "half2": h2, "close_reasons": by_reason, "per_symbol_net": per_sym,
        "CLEARS_BAR": clears, "blockers": reasons,
    }


def show(j: dict) -> None:
    print(f"\n=== {j['label']} ===")
    print(f"  n={j['n']}  total=${j['total']:+.2f}  win={j['win_rate']*100:.1f}%  "
          f"expectancy=${j['expectancy']:+.4f}/trade  sharpe={j['sharpe']:+.4f}")
    print(f"  cluster=symbol : t={j['t_symbol']:+.2f}  eff_n={j['eff_n_symbol']:.1f}  DSR={j['dsr_symbol']:.3f}")
    print(f"  cluster=week   : t={j['t_week']:+.2f}  eff_n={j['eff_n_week']:.1f}  DSR={j['dsr_week']:.3f}")
    print(f"  split-halves   : {j['half1']:+.1f} / {j['half2']:+.1f}")
    print(f"  close reasons  : {j['close_reasons']}")
    print(f"  per-symbol net : {j['per_symbol_net']}")


def main() -> None:
    if not CACHE.exists():
        print(f"Cache {CACHE} not found — run scripts/lev_perp_universe_research.py first.")
        sys.exit(1)
    bars_by_symbol = json.loads(CACHE.read_text())
    print(f"Loaded cached daily bars for: {list(bars_by_symbol.keys())}")
    print(f"Universe: {UNIVERSE_8}   warmup={WARMUP} bars")

    t_bar = _family_t_bar(1)  # PRE-REGISTERED single candidate
    print(f"\nPre-registered k=1 -> Sidak family t-bar = {t_bar:.2f}, "
          f"DSR bar = {DSR_MIN}, n_min = {N_MIN}")

    arms = [
        (sig_sma50, "CONTROL  v1 SMA-50 x 8-coin (must reproduce PR #87)"),
        (sig_macross, "CANDIDATE  MA10/40-cross x 8-coin x v1 fixed-TP exit"),
    ]

    judged = []
    real_print = builtins.print
    for fn, label in arms:
        builtins.print = lambda *a, **k: None  # silence production log noise
        try:
            r = run(bars_by_symbol, UNIVERSE_8, fn, label)
        finally:
            builtins.print = real_print
        judged.append(judge(r, t_bar))
        show(judged[-1])

    ctrl, cand = judged[0], judged[1]
    print("\n" + "=" * 78)
    print("HARNESS VALIDATION — control vs PR #87's published 8-coin result")
    print("  published: n=856  win=52.8%  expectancy=+$0.161  t=1.21  DSR=0.887")
    print(f"  this run : n={ctrl['n']}  win={ctrl['win_rate']*100:.1f}%  "
          f"expectancy=${ctrl['expectancy']:+.3f}  t={ctrl['t_symbol']:.2f}  "
          f"DSR={ctrl['dsr_symbol']:.3f}")
    print("=" * 78)
    print(f"VERDICT (k=1, pre-registered; must clear under BOTH clusterings)")
    print(f"  {cand['label']}")
    print(f"  -> {'CLEARS THE BAR' if cand['CLEARS_BAR'] else 'DOES NOT CLEAR THE BAR'}")
    if not cand["CLEARS_BAR"]:
        print(f"     blockers: {', '.join(cand['blockers'])}")
    print("=" * 78)

    OUT.write_text(json.dumps(
        {"k": 1, "t_family": t_bar, "dsr_min": DSR_MIN, "n_min": N_MIN,
         "pre_registered_in": "RESEARCH_2026-07-26_lev_perp_v1_frequency.md (Standing follow-on)",
         "rows": judged}, indent=2, default=str))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
