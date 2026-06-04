"""
Decision log — the full, queryable record of what the bot thought and did.

"Track every trade, every gate, every thought process." This writes one JSON
line per event to data/swing_decisions.jsonl: every bar evaluation (including
the SKIPs, with which gate failed), every entry with its stop/target/R:R, and
every exit with the realized P&L. Nothing the strategy decided is hidden.

JSONL (one JSON object per line) so it's append-only, crash-safe, greppable, and
trivially loaded back into pandas/analysis. Use summarize() for a quick read of
why setups are dying — the same idea as the live [FUNNEL] log, but persisted and
per-decision instead of aggregated.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path("data/swing_decisions.jsonl")


class DecisionLog:
    def __init__(self, path: Optional[Path] = None, echo: bool = False):
        self.path = path or DEFAULT_PATH
        self.echo = echo                      # also print to stdout (backtest mode)
        self.path.parent.mkdir(exist_ok=True)

    def _write(self, kind: str, payload: dict):
        rec = {"logged_at": datetime.now(timezone.utc).isoformat(),
               "kind": kind, **payload}
        line = json.dumps(rec, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.echo:
            print(line)

    def evaluation(self, dec) -> None:
        """Log a per-bar decision (ENTER/HOLD/SKIP/EXIT) with full reasoning."""
        self._write("evaluation", {
            "symbol": dec.symbol, "ts": dec.ts, "price": dec.price,
            "action": dec.action, "reason": dec.reason,
            "gates": dec.gates, "indicators": dec.indicators,
            "stop_price": dec.stop_price, "target_price": dec.target_price,
            "rr": dec.rr,
        })

    def opened(self, symbol: str, ts: str, price: float, size_usd: float,
               stop: float, target: float, rr: float, reason: str) -> None:
        self._write("open", {"symbol": symbol, "ts": ts, "entry": price,
                             "size_usd": size_usd, "stop": stop, "target": target,
                             "rr": rr, "reason": reason})

    def closed(self, symbol: str, ts_in: str, ts_out: str, entry: float,
               exit_price: float, size_usd: float, pnl: float, pnl_pct: float,
               reason: str, bars_held: int) -> None:
        self._write("close", {"symbol": symbol, "ts_in": ts_in, "ts_out": ts_out,
                             "entry": entry, "exit": exit_price, "size_usd": size_usd,
                             "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
                             "won": pnl > 0, "bars_held": bars_held})

    # ── read-back ────────────────────────────────────────────────────────────

    def records(self, kind: Optional[str] = None) -> list:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or r.get("kind") == kind:
                out.append(r)
        return out

    def summarize(self) -> str:
        evals = self.records("evaluation")
        closes = self.records("close")
        if not evals:
            return "no decisions logged yet"
        actions = Counter(e["action"] for e in evals)
        # why did SKIPs die? tally each failed gate
        fails = Counter()
        for e in evals:
            if e["action"] == "SKIP":
                for g, ok in (e.get("gates") or {}).items():
                    if ok is False:
                        fails[g] += 1
        wins = sum(1 for c in closes if c["won"])
        n = len(closes)
        lines = [
            f"evaluations: {dict(actions)}",
            f"top skip reasons: {dict(fails.most_common(5))}",
            f"closed trades: {n}  win_rate: {wins/n*100:.0f}%" if n else "closed trades: 0",
        ]
        return "\n".join(lines)
