"""Read-only data layer for the live dashboard.

Pure functions that snapshot every paper arm's state from the on-disk files the
arms already write (``data/*_state.json``) plus the cross-arm attribution ledger
(``data/attribution.db``). No network, no mutation — safe to call from a web
handler on the VPS while the bot is running.

Design notes:
- Each forward arm writes ``data/<name>_state.json`` with at least
  ``starting_equity`` and ``equity`` (some also ``equity_mtm``, ``positions``,
  ``closed``). Shapes vary slightly per arm, so every read is defensive.
- "Proof status" here is a *lightweight* honest label (idle / building / a quick
  t-stat read), NOT a substitute for ``proof_scorecard.py``'s pre-registered bar.
  The dashboard is for visibility; the scorecard remains the arbiter of proof.
"""

from __future__ import annotations

import glob
import json
import math
import os
import sqlite3
from typing import Any

# Map raw state-file stems → friendly display names. Anything not listed falls
# back to the stem itself, so new arms show up automatically.
_DISPLAY_NAMES = {
    "brain_paper": "brain",
    "btc_trend": "btc_trend",
    "conf_paper": "conf_trend",
    "kelly_trend": "kelly_trend",
    "lev_perp": "lev_perp (3x)",
    "pairs_paper": "pairs (mkt-neutral)",
    "swing_paper": "swing (4h majors)",
    "tsmom_ls": "tsmom L/S",
    "tsmom_paper": "tsmom_50",
}


def _data_dir(data_dir: str | None) -> str:
    return data_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _load_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _closed_pnls(closed: Any) -> list[float]:
    """Extract per-trade net PnL from a 'closed' list, tolerant of key naming."""
    out: list[float] = []
    if not isinstance(closed, list):
        return out
    for c in closed:
        if not isinstance(c, dict):
            continue
        for k in ("net", "net_pnl", "pnl", "pnl_usd", "realized_pnl", "profit"):
            if k in c and isinstance(c[k], (int, float)):
                out.append(float(c[k]))
                break
    return out


def _n_open(positions: Any) -> int:
    if isinstance(positions, dict):
        return len(positions)
    if isinstance(positions, list):
        return len(positions)
    return 0


def _proof_status(pnls: list[float]) -> tuple[str, float | None]:
    """Honest, lightweight read. Returns (label, t_stat_or_None).

    - 0 trades            → "idle"
    - 1..29 trades        → "building n<30"  (below the n>=30 proof floor)
    - >=30 trades         → t-stat of mean/sem; PROMISING if t>2 else "flat"
    This is NOT the proof bar (no cost/correlation/Šidák correction) — it only
    tells you at a glance whether an arm is even eligible to be judged yet.
    """
    n = len(pnls)
    if n == 0:
        return "idle", None
    if n < 30:
        return f"building n={n}<30", None
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
    sem = math.sqrt(var / n) if var > 0 else 0.0
    if sem == 0:
        return ("flat", 0.0)
    t = mean / sem
    if t > 2.0:
        return "PROMISING t>2", round(t, 2)
    if t < -2.0:
        return "LOSING t<-2", round(t, 2)
    return "no edge yet", round(t, 2)


def collect_arms(data_dir: str | None = None) -> list[dict[str, Any]]:
    """Snapshot every ``*_state.json`` arm. Returns rows sorted by net PnL desc."""
    dd = _data_dir(data_dir)
    rows: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(dd, "*_state.json"))):
        stem = os.path.basename(path)[: -len("_state.json")]
        d = _load_json(path)
        if d is None or "starting_equity" not in d:
            continue
        start = float(d.get("starting_equity") or 0.0)
        equity = float(d.get("equity_mtm") if d.get("equity_mtm") is not None else d.get("equity") or 0.0)
        pnls = _closed_pnls(d.get("closed"))
        wins = sum(1 for p in pnls if p > 0)
        status, t = _proof_status(pnls)
        rows.append(
            {
                "name": _DISPLAY_NAMES.get(stem, stem),
                "stem": stem,
                "equity": round(equity, 2),
                "start": round(start, 2),
                "pnl": round(equity - start, 2),
                "pnl_pct": round((equity - start) / start * 100, 2) if start else 0.0,
                "trades": len(pnls),
                "wins": wins,
                "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0.0,
                "open": _n_open(d.get("positions")),
                "status": status,
                "t_stat": t,
                "started_at": d.get("started_at"),
            }
        )
    rows.sort(key=lambda r: r["pnl"], reverse=True)
    return rows


def collect_attribution(data_dir: str | None = None) -> list[dict[str, Any]]:
    """Per-arm gross/fees/slippage/net from the attribution ledger."""
    dd = _data_dir(data_dir)
    db = os.path.join(dd, "attribution.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        cur = con.execute(
            "SELECT arm, COUNT(*), "
            "ROUND(COALESCE(SUM(gross_pnl),0),2), ROUND(COALESCE(SUM(fees_paid),0),2), "
            "ROUND(COALESCE(SUM(slippage_cost),0),2), ROUND(COALESCE(SUM(net_pnl),0),2) "
            "FROM fills GROUP BY arm ORDER BY 6 DESC"
        )
        out = [
            {"arm": r[0], "fills": r[1], "gross": r[2], "fees": r[3], "slippage": r[4], "net": r[5]}
            for r in cur.fetchall()
        ]
    except sqlite3.OperationalError:
        out = []
    finally:
        con.close()
    return out


def collect_tournament(data_dir: str | None = None, top: int = 30) -> dict[str, Any]:
    """Read the latest tournament leaderboard (data/tournament.json) if present.

    Returns the multiple-testing summary + the top-N candidates by Sharpe. Empty
    payload if the tournament hasn't run yet — the dashboard degrades gracefully.
    """
    dd = _data_dir(data_dir)
    d = _load_json(os.path.join(dd, "tournament.json"))
    if d is None:
        return {"summary": {}, "candidates": [], "generated_at": None, "coins": []}
    cands = d.get("candidates") or []
    return {
        "summary": d.get("summary") or {},
        "candidates": cands[:top],
        "n_total": len(cands),
        "generated_at": d.get("generated_at"),
        "coins": d.get("coins") or [],
        "n_bars": d.get("n_bars"),
    }


def snapshot(data_dir: str | None = None) -> dict[str, Any]:
    """Full dashboard payload: arms + attribution + tournament + portfolio totals."""
    arms = collect_arms(data_dir)
    attrib = collect_attribution(data_dir)
    total_equity = round(sum(a["equity"] for a in arms), 2)
    total_start = round(sum(a["start"] for a in arms), 2)
    return {
        "arms": arms,
        "attribution": attrib,
        "tournament": collect_tournament(data_dir),
        "totals": {
            "equity": total_equity,
            "start": total_start,
            "pnl": round(total_equity - total_start, 2),
            "pnl_pct": round((total_equity - total_start) / total_start * 100, 2) if total_start else 0.0,
            "n_arms": len(arms),
            "active": sum(1 for a in arms if a["open"] > 0),
        },
    }
