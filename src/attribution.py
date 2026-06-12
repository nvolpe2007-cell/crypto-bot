"""
Cross-arm P&L attribution ledger.

ONE place that answers "where is each dollar going, per arm?" — gross edge vs
fee drag vs slippage drag vs net, for every arm in the bot:

    directional        — the intraday scalper (paper_trading.py loop)
    funding_aggr       — Funding Arb (aggressive)
    funding_majors     — Funding Arb (majors)
    funding_kraken     — Funding Arb (Kraken)
    triarb             — triangular-arb scanner (paper)

Today that data is fragmented and not comparable side by side: the directional
side lives in trade_journal.{json,csv} (arm-blind), each FundingArbPaperSim
keeps its own private rollup, and the triarb scanner has its own counters. The
heartbeat under-reports because it never sums the arm rollups (memory
funding_arb_kraken_bleed). This module is the unifier.

Storage: SQLite at data/attribution.db. Append-only in practice — one row per
realised event (a closed directional trade, a closed funding position, a
recorded triarb cycle). Writes are BEST-EFFORT and must NEVER break trade flow:
every public method swallows exceptions (mirrors TradeJournal.append_csv).

Design notes:
  - The bot is a single asyncio process (run_all_bots → asyncio.gather) but the
    funding scanner and the main loop are separate tasks, and executors may run
    in threads later. We hold one connection in WAL mode, autocommit
    (isolation_level=None), guarded by a threading.Lock. Volume is tiny (a few
    writes/min) so this is plenty fast and crash-safe.
  - Slippage in paper mode: fills are at the reference price, so intended==fill
    and slippage≈0 until an arm passes a modelled or real fill that diverges. We
    store BOTH intended_price and fill_price so the column becomes meaningful the
    instant they differ (e.g. when the maker-only / live executor lands).
  - "gross" is P&L before fees+slippage; "net" is after everything. Callers may
    pass either and we derive the other when the pieces are present.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "attribution.db"
)

# Canonical arm identifiers. Not enforced (record() accepts any string) but
# documented here so call sites and the scorecard agree on spelling.
ARM_DIRECTIONAL    = "directional"
ARM_FUNDING_AGGR   = "funding_aggr"
ARM_FUNDING_MAJORS = "funding_majors"
ARM_FUNDING_KRAKEN = "funding_kraken"
ARM_TRIARB         = "triarb"

# Order arms appear in the scorecard (live-money arms first).
_ARM_ORDER = [
    ARM_DIRECTIONAL,
    ARM_FUNDING_AGGR,
    ARM_FUNDING_MAJORS,
    ARM_FUNDING_KRAKEN,
    ARM_TRIARB,
]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,   -- ISO8601 UTC when the row was recorded (≈ exit time)
    arm            TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT,            -- buy | sell | long | short | cycle
    intended_price REAL,
    fill_price     REAL,
    size_usd       REAL,
    fees_paid      REAL DEFAULT 0,  -- total round-trip fees ($)
    slippage_cost  REAL DEFAULT 0,  -- signed cost ($): positive = hurt us
    gross_pnl      REAL,            -- before fees + slippage
    net_pnl        REAL,            -- after everything
    reason         TEXT,            -- exit reason / note
    signal         TEXT,            -- JSON: signal values at entry
    meta           TEXT,            -- JSON: arm-specific extras
    opened_at      TEXT,
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_arm ON fills(arm);
CREATE INDEX IF NOT EXISTS idx_fills_ts  ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_arm_ts ON fills(arm, ts);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slippage_from_prices(side: Optional[str], intended: Optional[float],
                          fill: Optional[float], size_usd: Optional[float]) -> float:
    """Signed slippage *cost* in dollars (positive = it hurt us).

    For a buy/long we pay; a higher fill than intended is a cost.
    For a sell/short we receive; a lower fill than intended is a cost.
    Neutral/cycle legs (funding, triarb) have no directional fill price here —
    callers pass slippage_cost explicitly, so this returns 0.
    """
    if intended is None or fill is None or not size_usd or intended <= 0:
        return 0.0
    units = size_usd / intended
    s = (side or "").lower()
    if s in ("buy", "long"):
        return (fill - intended) * units
    if s in ("sell", "short"):
        return (intended - fill) * units
    return 0.0


class AttributionLedger:
    """SQLite-backed cross-arm attribution ledger.

    Open once, reuse. All public methods are exception-safe: a failure to write
    is logged at WARNING and never propagates into the trading path.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path, check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            # WAL: concurrent readers (dashboard / daily summary) don't block the
            # writer, and a crash mid-write can't corrupt the DB.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            self._conn.executescript(_SCHEMA)
        except Exception as exc:
            logger.warning("[ATTRIB] init failed (%s) — ledger disabled", exc)
            self._conn = None

    # ── Write ────────────────────────────────────────────────────────────────

    def record(self, arm: str, symbol: str, *,
               side: Optional[str] = None,
               intended_price: Optional[float] = None,
               fill_price: Optional[float] = None,
               size_usd: Optional[float] = None,
               fees_paid: float = 0.0,
               slippage_cost: Optional[float] = None,
               gross_pnl: Optional[float] = None,
               net_pnl: Optional[float] = None,
               reason: str = "",
               signal: Optional[Dict[str, Any]] = None,
               meta: Optional[Dict[str, Any]] = None,
               opened_at: Optional[str] = None,
               closed_at: Optional[str] = None,
               ts: Optional[str] = None) -> bool:
        """Record one realised event. Returns True on success, False otherwise.

        Derivations when fields are omitted:
          - slippage_cost from (side, intended_price, fill_price, size_usd)
          - net_pnl  = gross_pnl - fees_paid - slippage_cost   (if gross given)
          - gross_pnl = net_pnl + fees_paid + slippage_cost    (if net given)
        """
        if self._conn is None:
            return False
        try:
            fees_paid = float(fees_paid or 0.0)
            if slippage_cost is None:
                slippage_cost = _slippage_from_prices(
                    side, intended_price, fill_price, size_usd)
            slippage_cost = float(slippage_cost or 0.0)

            if gross_pnl is None and net_pnl is not None:
                gross_pnl = float(net_pnl) + fees_paid + slippage_cost
            if net_pnl is None and gross_pnl is not None:
                net_pnl = float(gross_pnl) - fees_paid - slippage_cost

            row = (
                ts or _utc_now_iso(),
                str(arm),
                str(symbol),
                side,
                _f(intended_price),
                _f(fill_price),
                _f(size_usd),
                fees_paid,
                slippage_cost,
                _f(gross_pnl),
                _f(net_pnl),
                reason or "",
                json.dumps(signal, default=str) if signal else None,
                json.dumps(meta, default=str) if meta else None,
                opened_at,
                closed_at,
            )
            with self._lock:
                self._conn.execute(
                    "INSERT INTO fills (ts, arm, symbol, side, intended_price, "
                    "fill_price, size_usd, fees_paid, slippage_cost, gross_pnl, "
                    "net_pnl, reason, signal, meta, opened_at, closed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
            return True
        except Exception as exc:
            logger.warning("[ATTRIB] record failed for %s/%s: %s", arm, symbol, exc)
            return False

    # ── Read / aggregate ───────────────────────────────────────────────────────

    def summary_by_arm(self, since: Optional[str] = None,
                        until: Optional[str] = None) -> Dict[str, Dict[str, float]]:
        """Per-arm aggregates over [since, until). Bounds are ISO strings compared
        lexicographically against `ts` (ISO8601 sorts correctly). None = unbounded.

        Each arm maps to: n, gross, fees, slippage, net, wins, win_rate.
        """
        if self._conn is None:
            return {}
        where, params = self._time_where(since, until)
        sql = (
            "SELECT arm, "
            "COUNT(*) AS n, "
            "COALESCE(SUM(gross_pnl),0) AS gross, "
            "COALESCE(SUM(fees_paid),0) AS fees, "
            "COALESCE(SUM(slippage_cost),0) AS slippage, "
            "COALESCE(SUM(net_pnl),0) AS net, "
            "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),0) AS wins "
            f"FROM fills {where} GROUP BY arm"
        )
        out: Dict[str, Dict[str, float]] = {}
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall()
            for r in rows:
                n = int(r["n"]) or 0
                wins = int(r["wins"]) or 0
                out[r["arm"]] = {
                    "n": n,
                    "gross": round(float(r["gross"]), 4),
                    "fees": round(float(r["fees"]), 4),
                    "slippage": round(float(r["slippage"]), 4),
                    "net": round(float(r["net"]), 4),
                    "wins": wins,
                    "win_rate": round(wins / n * 100, 1) if n else 0.0,
                }
        except Exception as exc:
            logger.warning("[ATTRIB] summary_by_arm failed: %s", exc)
        return out

    def totals(self, since: Optional[str] = None,
               until: Optional[str] = None) -> Dict[str, float]:
        """Roll the per-arm summary up to a single all-arms total."""
        per = self.summary_by_arm(since, until)
        tot = {"n": 0, "gross": 0.0, "fees": 0.0, "slippage": 0.0,
               "net": 0.0, "wins": 0}
        for v in per.values():
            tot["n"] += v["n"]
            tot["gross"] += v["gross"]
            tot["fees"] += v["fees"]
            tot["slippage"] += v["slippage"]
            tot["net"] += v["net"]
            tot["wins"] += v["wins"]
        for k in ("gross", "fees", "slippage", "net"):
            tot[k] = round(tot[k], 4)
        tot["win_rate"] = round(tot["wins"] / tot["n"] * 100, 1) if tot["n"] else 0.0
        return tot

    def daily_scorecard(self, day: Optional[date] = None) -> Dict[str, Any]:
        """Per-arm attribution for one UTC calendar day (default: today UTC)."""
        day = day or datetime.now(timezone.utc).date()
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        since = start.isoformat()
        until = start.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
        return {
            "day": day.isoformat(),
            "by_arm": self.summary_by_arm(since, until),
            "total": self.totals(since, until),
        }

    def _time_where(self, since: Optional[str], until: Optional[str]):
        clauses, params = [], []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ── Telegram formatting ─────────────────────────────────────────────────────

def format_scorecard(scorecard: Dict[str, Any]) -> str:
    """Render a daily scorecard as a Telegram-ready HTML message (monospace)."""
    by_arm = scorecard.get("by_arm", {})
    total = scorecard.get("total", {})
    day = scorecard.get("day", "")

    # Stable, readable order: known arms first, then any extras alphabetically.
    arms = [a for a in _ARM_ORDER if a in by_arm]
    arms += sorted(a for a in by_arm if a not in _ARM_ORDER)

    header = f"{'arm':<15}{'n':>3} {'gross':>8} {'fees':>8} {'slip':>7} {'net':>8}"
    lines = [header, "-" * len(header)]
    for a in arms:
        v = by_arm[a]
        lines.append(
            f"{a:<15}{v['n']:>3} {v['gross']:>+8.3f} {v['fees']:>8.3f} "
            f"{v['slippage']:>+7.3f} {v['net']:>+8.3f}"
        )
    if total:
        lines.append("-" * len(header))
        lines.append(
            f"{'TOTAL':<15}{total.get('n',0):>3} {total.get('gross',0):>+8.3f} "
            f"{total.get('fees',0):>8.3f} {total.get('slippage',0):>+7.3f} "
            f"{total.get('net',0):>+8.3f}"
        )
    body = "\n".join(lines)
    net = total.get("net", 0.0)
    emoji = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
    return (
        f"{emoji} <b>Per-arm attribution — {day} UTC</b>\n"
        f"<pre>{body}</pre>\n"
        f"<i>gross = edge before costs · net = after fees+slippage</i>"
    )


# ── Module-level singleton (so call sites are one-liners) ────────────────────

_LEDGER: Optional[AttributionLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_ledger(db_path: Optional[str] = None) -> AttributionLedger:
    """Process-wide singleton ledger. First call wins the db_path."""
    global _LEDGER
    if _LEDGER is None:
        with _LEDGER_LOCK:
            if _LEDGER is None:
                _LEDGER = AttributionLedger(db_path)
    return _LEDGER


def record(arm: str, symbol: str, **kw) -> bool:
    """Convenience: record against the singleton ledger."""
    return get_ledger().record(arm, symbol, **kw)


def _f(x) -> Optional[float]:
    """Coerce to float or None — keeps NULLs in the DB instead of 0.0 sentinels."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
