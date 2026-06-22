"""
Time-of-day / session edge filter.

RESEARCH_strategies_and_filters.md §3.3 / §7.1 names this the one remaining,
evidence-backed, *unbuilt* gate: "liquidity, participation and news flow are
cyclical and predictable; win rates measurably drop in certain windows. Log
per-hour win rate, disable the worst windows. Cheapest edge available."

This module reads the bot's OWN realised record (the trade journal and the swing
forward-test ledger) and rates each UTC session by realised expectancy + a
Wilson lower bound on win-rate. It NEVER fabricates an edge — with too few
trades a session is NEUTRAL (fail-open), exactly like _spread_normal / _vpin_safe
pass on "no baseline". An UNFAVORABLE verdict means the bot has *measured itself*
losing money in that window over a real sample.

Consumers:
  - src/entry_checklist.py  → soft check `_session_favorable` (hard via env)
  - swing_paper.py          → measure-first gate (hard only when SESSION_FILTER_HARD)
  - scripts/weekly_report.py → "Session edge" table

Verdicts: "FAVORABLE" | "NEUTRAL" | "UNFAVORABLE".
"""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Session buckets — IDENTICAL to proof_scorecard._session and weekly_report so
# the whole system speaks one vocabulary for time-of-day.
#   Asia 0–7h · EU 8–15h · US 16–23h  (UTC)
SESSIONS = ("Asia", "EU", "US")

# Tunables (env-overridable, mirroring the rest of the gate knobs).
MIN_SAMPLES = int(os.getenv("SESSION_MIN_SAMPLES", "20"))      # below this → NEUTRAL
WINRATE_FLOOR = float(os.getenv("SESSION_WINRATE_FLOOR", "0.40"))  # Wilson-LB floor

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_JOURNAL = _ROOT / "data" / "trade_journal.csv"
_DEFAULT_SWING = _ROOT / "data" / "swing_paper_state.json"


def session_of_hour(hour: Optional[int]) -> Optional[str]:
    """Map a UTC hour (0-23) to its session bucket. None on bad input."""
    if hour is None:
        return None
    try:
        h = int(hour) % 24
    except (TypeError, ValueError):
        return None
    return "Asia" if h < 8 else ("EU" if h < 16 else "US")


def window_of_hour(hour: Optional[int]) -> Optional[str]:
    """Collapse the session bucket into the owner's day/night split:
    "night" = Asia (0-7 UTC), "day" = EU+US (8-23 UTC). Used by the swing
    runner's per-window trade budget. None on bad input."""
    s = session_of_hour(hour)
    if s is None:
        return None
    return "night" if s == "Asia" else "day"


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval for a win-rate. Penalises
    small samples — a 3/4 streak does not certify a high win-rate."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


class SessionEdge:
    """Per-session realised-edge ratings derived from the bot's own trades.

    Construct from explicit normalised records (`records=[{hour,won,pnl}, ...]`),
    from a running swing state dict (`from_state`), or — with no args — from the
    default on-disk journal + swing ledger (`from_files` semantics).
    """

    def __init__(
        self,
        records: Optional[Iterable[dict]] = None,
        *,
        min_samples: int = MIN_SAMPLES,
        winrate_floor: float = WINRATE_FLOOR,
    ):
        self.min_samples = min_samples
        self.winrate_floor = winrate_floor
        if records is None:
            records = self._load_journal(_DEFAULT_JOURNAL) + self._load_swing(_DEFAULT_SWING)
        self._records: List[dict] = [r for r in records if r.get("hour") is not None]
        self._stats_cache: Optional[Dict[str, dict]] = None

    # ── constructors ──────────────────────────────────────────────────────────
    @classmethod
    def from_files(
        cls,
        journal_csv: Path | str = _DEFAULT_JOURNAL,
        swing_state: Path | str = _DEFAULT_SWING,
        **kw,
    ) -> "SessionEdge":
        recs = cls._load_journal(Path(journal_csv)) + cls._load_swing(Path(swing_state))
        return cls(recs, **kw)

    @classmethod
    def from_state(cls, state: dict, **kw) -> "SessionEdge":
        """Build from an in-memory swing/tsmom state dict (its `closed` list).
        Lets the live forward-runner rate sessions off the freshest ledger."""
        return cls(cls._records_from_closed(state.get("closed", [])), **kw)

    # ── loaders (normalise to {hour:int, won:bool, pnl:float}) ─────────────────
    @staticmethod
    def _records_from_closed(closed: Iterable[dict]) -> List[dict]:
        out: List[dict] = []
        for p in closed or []:
            hour = p.get("entry_hour")
            if hour is None:
                continue
            try:
                pnl = float(p.get("pnl", 0.0))
            except (TypeError, ValueError):
                continue
            won = p.get("won")
            won = (won is True) if won is not None else (pnl > 0)
            out.append({"hour": int(hour), "won": bool(won), "pnl": pnl})
        return out

    @staticmethod
    def _load_swing(path: Path) -> List[dict]:
        if not path.exists():
            return []
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        return SessionEdge._records_from_closed(d.get("closed", []))

    @staticmethod
    def _load_journal(path: Path) -> List[dict]:
        if not path.exists():
            return []
        out: List[dict] = []
        try:
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    hour = row.get("hour_utc")
                    if hour in (None, ""):
                        continue
                    try:
                        h = int(float(hour))
                        pnl = float(row.get("pnl") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    won_raw = str(row.get("won", "")).strip().lower()
                    won = won_raw == "true" if won_raw in ("true", "false") else pnl > 0
                    out.append({"hour": h, "won": won, "pnl": pnl})
        except OSError:
            return []
        return out

    # ── ratings ───────────────────────────────────────────────────────────────
    def session_stats(self) -> Dict[str, dict]:
        """Per-session {n, wins, win_rate, expectancy, wilson_lb, verdict}."""
        if self._stats_cache is not None:
            return self._stats_cache
        buckets: Dict[str, dict] = {s: {"n": 0, "wins": 0, "pnl": 0.0} for s in SESSIONS}
        for r in self._records:
            s = session_of_hour(r["hour"])
            if s is None:
                continue
            b = buckets[s]
            b["n"] += 1
            b["wins"] += 1 if r["won"] else 0
            b["pnl"] += r["pnl"]
        out: Dict[str, dict] = {}
        for s, b in buckets.items():
            n = b["n"]
            win_rate = (b["wins"] / n) if n else 0.0
            expectancy = (b["pnl"] / n) if n else 0.0
            wlb = _wilson_lower_bound(b["wins"], n)
            out[s] = {
                "n": n, "wins": b["wins"], "win_rate": win_rate,
                "expectancy": expectancy, "wilson_lb": wlb,
                "verdict": self._verdict(n, expectancy, wlb),
            }
        self._stats_cache = out
        return out

    def _verdict(self, n: int, expectancy: float, wilson_lb: float) -> str:
        """UNFAVORABLE requires BOTH a real sample that loses money on average
        AND a win-rate whose 95% lower bound sits below the floor — two
        independent confirmations, so a couple of unlucky big losers can't
        condemn a window. FAVORABLE needs positive realised expectancy on a
        real sample. Everything else (incl. warm-up) is NEUTRAL → fail-open."""
        if n < self.min_samples:
            return "NEUTRAL"
        if expectancy < 0 and wilson_lb < self.winrate_floor:
            return "UNFAVORABLE"
        if expectancy > 0:
            return "FAVORABLE"
        return "NEUTRAL"

    def verdict_for_hour(self, hour: Optional[int]) -> str:
        s = session_of_hour(hour)
        if s is None:
            return "NEUTRAL"
        return self.session_stats()[s]["verdict"]

    @staticmethod
    def session_of(hour: Optional[int]) -> Optional[str]:
        return session_of_hour(hour)
