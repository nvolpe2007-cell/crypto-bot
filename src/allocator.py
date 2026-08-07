"""Proof-gated regime allocator (stage 3) — the "switch strategies on its own"
layer, done honestly.

It is a PAPER fund-of-funds over the existing forward arms. It allocates a
notional book across arms by ONE rule:

  • PROMOTE (give weight)  ⟺  the arm clears the pre-registered proof bar
      (executable & n>=30 & expectancy>0 & clustered-t over the Šidák family bar)
      — the SAME verdict proof_scorecard.py uses, imported, not reinvented.
  • DEMOTE (pull to cash)  ⟺  the arm draws down past its cap (immediate) or its
      edge decays back under the bar (persistence-gated).
  • RE-WEIGHT by regime    ⟺  tilt ONLY among already-proven arms (regime never
      promotes an unproven arm; it just shifts the mix).
  • PERSISTENCE-GATE every change so one noisy snapshot can't churn the book
      (rebalancing costs money — turnover is charged the real cost leg).

CRUCIAL honest property: when nothing is proven (today's reality — 0 arms clear
the bar), the correct allocation is 100% CASH and the book sits flat. That is the
feature, not a bug. This allocates PAPER only and judges the allocation logic
itself on its own forward equity curve.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from proof_scorecard import (  # the real, pre-registered bar
    _family_t_bar, _stats, _verdict,
    _deflated_sharpe, _expected_max_sharpe, DSR_MIN,
)

# Arm registry: (display name, state file, cluster granularity, executable-today,
# strategy family). Executable = runnable on a US Kraken-spot account right now
# (long-only); the perp/short arms are paper-only until Kraken US perps land.
ARMS: list[tuple[str, str, str, bool, str]] = [
    ("swing",       "swing_paper_state.json", "week",   True,  "trend"),
    ("tsmom_50",    "tsmom_paper_state.json", "week",   True,  "trend"),
    ("tsmom_fast",  "tsmom_fast_state.json",  "week",   True,  "trend"),
    ("conf_trend",  "conf_paper_state.json",  "week",   True,  "trend"),
    ("btc_trend",   "btc_trend_state.json",   "week",   True,  "trend"),
    ("kelly_trend", "kelly_trend_state.json", "week",   True,  "trend"),
    ("brain",       "brain_paper_state.json", "week",   True,  "discretionary"),
    ("tsmom_ls",    "tsmom_ls_state.json",    "week",   False, "trend"),
    ("regime_arm",  "regime_arm_state.json",  "day",    False, "trend"),
    ("lev_perp",    "lev_perp_state.json",    "week",   False, "leverage"),
    ("pairs",       "pairs_paper_state.json", "week",   False, "neutral"),
    ("micro",       "micro_paper_state.json", "minute", True,  "scalp"),
    ("flash_arb",   "flash_arb_state.json",   "week",   False, "arbitrage"),
]

# Regime → per-family preference multiplier (applied among proven arms only) and a
# gross-exposure scalar (risk-off regimes hold more cash).
_REGIME_FAMILY_PREF = {
    "TRENDING_UP":   {"trend": 1.0, "leverage": 1.0, "discretionary": 1.0, "neutral": 0.5, "scalp": 0.7},
    "TRENDING_DOWN": {"trend": 0.4, "leverage": 0.3, "discretionary": 0.8, "neutral": 1.0, "scalp": 0.6},
    "RANGING":       {"trend": 0.5, "leverage": 0.4, "discretionary": 0.8, "neutral": 1.0, "scalp": 0.8},
    "VOLATILE":      {"trend": 0.6, "leverage": 0.3, "discretionary": 0.7, "neutral": 0.8, "scalp": 0.5},
    "CRASH":         {"trend": 0.2, "leverage": 0.0, "discretionary": 0.5, "neutral": 0.7, "scalp": 0.3},
}
_REGIME_GROSS = {"TRENDING_UP": 1.0, "TRENDING_DOWN": 0.6, "RANGING": 0.8,
                 "VOLATILE": 0.5, "CRASH": 0.25}


@dataclass
class AllocConfig:
    max_gross: float = 1.0        # never lever the paper book
    per_arm_cap: float = 0.40     # no single arm over 40% of the book
    confirm_ticks: int = 3        # a weight change must persist this many updates
    arm_dd_cap: float = 0.25      # demote an arm whose own equity draws down >25%
    cooldown_ticks: int = 6       # ticks an arm stays benched after a dd-demote
    switch_cost_frac: float = 0.0025  # one-way cost leg charged on rebalance turnover
    executable_only: bool = True  # only fund arms runnable on Kraken spot today
    start_equity: float = 1000.0


def _iso(ts, gran: str) -> str:
    try:
        dt = datetime.fromtimestamp(int(float(ts)), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "unknown"
    if gran == "day":
        return dt.strftime("%Y-%m-%d")
    if gran == "minute":
        return dt.strftime("%Y-%m-%dT%H:%M")
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def read_arm_record(data_dir: str, fname: str, gran: str) -> dict | None:
    """Read one arm's state file → nets, clusters, equity. None if absent/empty."""
    path = os.path.join(data_dir, fname)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or "starting_equity" not in d:
        return None
    closed = d.get("closed") or []
    closed = sorted(closed, key=lambda p: p.get("exit_ts") or "") if isinstance(closed, list) else []
    nets, clusters, entry_ts = [], [], []
    for p in closed:
        if not isinstance(p, dict):
            continue
        v = p.get("pnl", p.get("net", p.get("net_pnl")))
        if v is None:
            continue
        try:
            nets.append(float(v))
        except (TypeError, ValueError):
            continue
        clusters.append(_iso(p.get("entry_ts"), gran))
        try:
            entry_ts.append(int(float(p.get("entry_ts"))))
        except (TypeError, ValueError):
            pass
    equity = float(d.get("equity_mtm") if d.get("equity_mtm") is not None else d.get("equity") or 0.0)
    start = float(d.get("starting_equity") or 0.0)
    return {"nets": nets, "clusters": clusters, "equity": equity, "start": start,
            "entry_ts": sorted(entry_ts)}


def score_arms(data_dir: str, cfg: AllocConfig | None = None) -> list[dict]:
    """Score every registered arm on the real proof bar. Returns one row per arm
    that has a state file, each with: name, family, executable, equity, n,
    expectancy, t_clustered, sharpe, proven (clears both the family-wise t-bar
    AND the Deflated Sharpe bar — matching proof_scorecard.main()'s final verdict)."""
    cfg = cfg or AllocConfig()
    raw = []
    for name, fname, gran, execu, family in ARMS:
        rec = read_arm_record(data_dir, fname, gran)
        if rec is None:
            continue
        s = _stats(rec["nets"], rec["clusters"])
        raw.append((name, family, execu, rec, s))
    k = max(1, len(raw))                # family-wise correction over arms judged
    t_family = _family_t_bar(k)
    sr0 = _expected_max_sharpe([s["sharpe"] for _, _, _, _, s in raw if s["n"] >= 2])
    rows = []
    for name, family, execu, rec, s in raw:
        a = dict(label=name, executable=execu, **s)
        dsr = _deflated_sharpe(
            s["sharpe"], s.get("eff_n", s["n"]),
            s.get("skew", 0.0), s.get("kurt", 3.0), sr0,
        )
        proven = _verdict(a, t_family, k).startswith("PROVEN ✓") and dsr > DSR_MIN
        rows.append({
            "name": name, "family": family, "executable": execu, "equity": rec["equity"],
            "start": rec["start"], "n": s["n"], "expectancy": round(s["expectancy"], 5),
            "t_clustered": round(s["t_clustered"], 2), "sharpe": round(s["sharpe"], 3),
            "max_dd": round(s["max_dd"], 4), "proven": proven, "t_family": round(t_family, 2),
            "dsr": round(dsr, 3), "sr0": round(sr0, 3),
        })
    return rows


def target_weights(scored: list[dict], regime: str | None, cfg: AllocConfig) -> dict[str, float]:
    """Target allocation. Eligible = proven (and executable, if executable_only).
    Among eligible: inverse-vol base × regime-family preference, normalized to the
    regime gross scalar, per-arm capped. Everything else → cash. Empty → all cash."""
    eligible = [a for a in scored if a["proven"] and (a["executable"] or not cfg.executable_only)]
    if not eligible:
        return {}
    pref = _REGIME_FAMILY_PREF.get(regime or "", {})
    gross = cfg.max_gross * (_REGIME_GROSS.get(regime or "", 1.0))
    raw = {}
    for a in eligible:
        inv_vol = 1.0 / (abs(a["max_dd"]) + 0.05)         # steadier arms get more
        raw[a["name"]] = max(0.0, inv_vol * pref.get(a["family"], 1.0))
    tot = sum(raw.values())
    if tot <= 0:
        return {}
    weights = {n: min(cfg.per_arm_cap, w / tot * gross) for n, w in raw.items()}
    # renormalize if per-arm caps removed mass but gross headroom remains
    s = sum(weights.values())
    if s > gross and s > 0:
        weights = {n: w * gross / s for n, w in weights.items()}
    return {n: round(w, 4) for n, w in weights.items() if w > 1e-4}


N_MIN_TRADES = 30  # the proof-bar sample floor (matches proof_scorecard.N_MIN)


def switch_readiness(data_dir: str, cfg: AllocConfig | None = None) -> list[dict]:
    """Per-arm 'how close is this to being switched on, and when' — the human view
    of the proof gate. For each arm: its trade count vs the n>=30 floor, whether its
    edge is positive, its clustered-t vs the family bar, an ETA to n>=30 at its
    observed trade pace, and a plain-language status. Sorted most-ready first.

    The honest split this surfaces:
      • positive edge, n<30  → TIME switches it on (ETA shown).
      • negative edge        → time will NEVER switch it on; needs a better strategy.
    """
    cfg = cfg or AllocConfig()
    scored = score_arms(data_dir, cfg)
    fmap = {n: f for n, f, _g, _e, _fam in ARMS}
    gmap = {n: g for n, _f, g, _e, _fam in ARMS}
    out = []
    for a in scored:
        rec = read_arm_record(data_dir, fmap[a["name"]], gmap.get(a["name"], "week"))
        ts = (rec or {}).get("entry_ts") or []
        span_wk = (ts[-1] - ts[0]) / 604800.0 if len(ts) >= 2 else 0.0
        cadence = round(a["n"] / span_wk, 2) if span_wk > 0.2 else 0.0
        to_n = max(0, N_MIN_TRADES - a["n"])
        positive = a["expectancy"] > 0
        eta_days = None
        if a["proven"]:
            status = "PROVEN — eligible to fund"
        elif a["n"] >= 3 and not positive:
            status = "losing — needs a better edge, not more time"
        elif a["n"] == 0:
            status = "no closed trades yet"
        elif positive and to_n == 0:
            status = "edge holding — at the t-bar now"
        elif positive and cadence > 0:
            eta_days = int(round(to_n / cadence * 7))
            status = f"on track — {to_n} more trades to prove it"
        elif positive:
            status = f"positive so far — {to_n} more trades to prove it"
        else:
            status = "building sample"
        out.append({
            "name": a["name"], "family": a["family"], "executable": a["executable"],
            "n": a["n"], "need_more": to_n, "expectancy": a["expectancy"],
            "t_clustered": a["t_clustered"], "t_bar": a["t_family"],
            "positive": positive, "proven": a["proven"],
            "cadence_wk": cadence, "eta_days": eta_days, "status": status,
        })
    out.sort(key=lambda r: (not r["proven"], not r["positive"], r["need_more"], -r["t_clustered"]))
    return out


@dataclass
class MetaAllocator:
    cfg: AllocConfig = field(default_factory=AllocConfig)
    equity: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)   # current adopted weights
    pending: dict[str, list] = field(default_factory=dict)    # name -> [target, count]
    last_equity: dict[str, float] = field(default_factory=dict)
    peak: dict[str, float] = field(default_factory=dict)      # per-arm equity peak
    cooldown: dict[str, int] = field(default_factory=dict)
    history: list = field(default_factory=list)
    started_at: str = ""
    equity_curve: list = field(default_factory=list)

    def __post_init__(self):
        if self.equity == 0.0:
            self.equity = self.cfg.start_equity
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()

    def update(self, scored: list[dict], regime: str | None) -> dict:
        cfg = self.cfg
        by_name = {a["name"]: a for a in scored}
        target = target_weights(scored, regime, cfg)

        # 1) Drawdown demote (immediate, bypasses persistence) + cooldown bench.
        demoted = []
        for a in scored:
            eq = a["equity"]
            pk = max(self.peak.get(a["name"], eq), eq)
            self.peak[a["name"]] = pk
            if pk > 0 and (eq / pk - 1.0) <= -cfg.arm_dd_cap:
                self.cooldown[a["name"]] = cfg.cooldown_ticks
                if a["name"] in target:
                    demoted.append(a["name"])
        for n in list(self.cooldown):
            if self.cooldown[n] > 0:
                target[n] = 0.0
                self.cooldown[n] -= 1
            else:
                self.cooldown.pop(n, None)

        # 2) Persistence-gate adoption of each arm's target (demotion to 0 from a
        #    cooldown is already immediate above).
        new_weights = dict(self.weights)
        for name in set(list(target) + list(self.weights)):
            tgt = target.get(name, 0.0)
            cur = self.weights.get(name, 0.0)
            if abs(tgt - cur) <= 1e-4:
                self.pending.pop(name, None)
                continue
            p = self.pending.get(name)
            if p and abs(p[0] - tgt) <= 1e-4:
                p[1] += 1
            else:
                p = [tgt, 1]
            self.pending[name] = p
            if p[1] >= cfg.confirm_ticks or (tgt == 0.0 and name in self.cooldown):
                new_weights[name] = tgt
                self.pending.pop(name, None)
        new_weights = {n: w for n, w in new_weights.items() if w > 1e-4}

        # 3) Realize the period: arms' returns over the period at the OLD weights,
        #    then pay turnover cost to rebalance to the new weights.
        port_ret = 0.0
        for name, w in self.weights.items():
            a = by_name.get(name)
            if not a:
                continue
            le = self.last_equity.get(name)
            if le and le > 0:
                port_ret += w * (a["equity"] / le - 1.0)
        self.equity *= (1.0 + port_ret)
        turnover = sum(abs(new_weights.get(n, 0.0) - self.weights.get(n, 0.0))
                       for n in set(list(new_weights) + list(self.weights)))
        self.equity *= (1.0 - cfg.switch_cost_frac * turnover)

        # 4) Commit.
        self.weights = new_weights
        for a in scored:
            self.last_equity[a["name"]] = a["equity"]
        cash = round(max(0.0, 1.0 - sum(self.weights.values())), 4)
        decision = {
            "ts": int(time.time()), "regime": regime or "unknown",
            "equity": round(self.equity, 2), "cash_pct": round(cash * 100, 1),
            "n_proven": sum(1 for a in scored if a["proven"]),
            "weights": dict(sorted(self.weights.items(), key=lambda kv: -kv[1])),
            "demoted": demoted, "turnover": round(turnover, 4),
            "port_ret_pct": round(port_ret * 100, 3),
        }
        self.history.append(decision)
        self.history = self.history[-200:]
        self.equity_curve.append({"t": decision["ts"], "v": round(self.equity, 2)})
        self.equity_curve = self.equity_curve[-2000:]
        return decision

    # ── persistence + dashboard shape ──────────────────────────────────────────
    def to_state(self) -> dict:
        cash = round(max(0.0, 1.0 - sum(self.weights.values())), 4)
        return {
            "starting_equity": self.cfg.start_equity,
            "equity": round(self.equity, 2),
            "equity_mtm": round(self.equity, 2),
            "started_at": self.started_at,
            "positions": dict(self.weights),     # arm -> weight (dashboard shows count)
            "closed": [],                         # meta-arm: continuous, no discrete trades
            "cash_pct": round(cash * 100, 1),
            "decisions": self.history[-50:],
            "equity_curve": self.equity_curve[-500:],
            "_alloc": {  # internal carry-over for the next run
                "weights": self.weights, "pending": self.pending,
                "last_equity": self.last_equity, "peak": self.peak,
                "cooldown": self.cooldown,
            },
        }

    @classmethod
    def from_state(cls, d: dict | None, cfg: AllocConfig | None = None) -> "MetaAllocator":
        cfg = cfg or AllocConfig()
        if not d:
            return cls(cfg=cfg)
        carry = d.get("_alloc") or {}
        return cls(
            cfg=cfg,
            equity=float(d.get("equity") or cfg.start_equity),
            weights=dict(carry.get("weights") or {}),
            pending={k: list(v) for k, v in (carry.get("pending") or {}).items()},
            last_equity=dict(carry.get("last_equity") or {}),
            peak=dict(carry.get("peak") or {}),
            cooldown=dict(carry.get("cooldown") or {}),
            history=list(d.get("decisions") or []),
            started_at=str(d.get("started_at") or ""),
            equity_curve=list(d.get("equity_curve") or []),
        )
