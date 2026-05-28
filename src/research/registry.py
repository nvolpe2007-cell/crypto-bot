"""
Hypothesis registry loader.

Reads `research/hypothesis_registry.yaml` — the structured record of every
strategy idea, its evidence, deployment gate, and status. Designed so that
"what have we tested and what was the result?" is a queryable artifact, not
buried in memory files or stale design docs.

Schema (per entry):
    id:               kebab-case slug, must be unique
    name:             human title
    family:           grouping ("altperp", "funding-arb", "market-neutral", …)
    hypothesis:       one-paragraph claim the strategy is testing
    code:             pointer to the implementation (file or directory)
    backtest:         pointer to the backtest entry-point, if any
    evidence:         list of dated measurements, each with {date, window?,
                      script?, method?, result}
    deployment_gate:  {requires: str, blocked_by?: str}
    status:           sit-out | paper-only | live-tiny | live | killed | infrastructure
    notes:            free-text (markdown ok)

Use the CLI in `bin/registry` (or `python -m src.research.registry`) to query.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml


VALID_STATUSES = {
    "sit-out",       # measured, no edge, do not deploy
    "paper-only",    # forward-walking, do not flip live
    "live-tiny",     # live with hard caps, monitoring
    "live",          # proven OOS, deployed
    "killed",        # disproven / decayed, archived
    "infrastructure",  # enabling layer, not a strategy itself
}

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "research" / "hypothesis_registry.yaml"


@dataclass
class Evidence:
    date: str
    result: str
    window: Optional[str] = None
    script: Optional[str] = None
    method: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        return cls(
            date=str(d.get("date", "")),
            result=str(d.get("result", "")),
            window=d.get("window"),
            script=d.get("script"),
            method=d.get("method"),
        )


@dataclass
class Hypothesis:
    id: str
    name: str
    family: str
    hypothesis: str
    code: str
    status: str
    deployment_gate: Dict[str, str]
    evidence: List[Evidence] = field(default_factory=list)
    backtest: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Hypothesis":
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            family=str(d.get("family", "")),
            hypothesis=str(d.get("hypothesis", "")),
            code=str(d.get("code", "")),
            status=str(d.get("status", "")),
            deployment_gate=dict(d.get("deployment_gate") or {}),
            evidence=[Evidence.from_dict(e) for e in (d.get("evidence") or [])],
            backtest=d.get("backtest"),
            notes=d.get("notes"),
        )


def load(path: Path = REGISTRY_PATH) -> List[Hypothesis]:
    """Load + validate the registry. Raises ValueError on schema issues."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = (raw or {}).get("hypotheses") or []
    out: List[Hypothesis] = []
    seen_ids: set[str] = set()
    for i, d in enumerate(entries):
        if not isinstance(d, dict):
            raise ValueError(f"entry {i}: must be a mapping, got {type(d).__name__}")
        for required in ("id", "name", "status"):
            if required not in d:
                raise ValueError(f"entry {i}: missing required field '{required}'")
        if d["id"] in seen_ids:
            raise ValueError(f"duplicate id: {d['id']!r}")
        seen_ids.add(d["id"])
        if d["status"] not in VALID_STATUSES:
            raise ValueError(
                f"entry {d['id']!r}: invalid status {d['status']!r}; "
                f"must be one of {sorted(VALID_STATUSES)}"
            )
        out.append(Hypothesis.from_dict(d))
    return out


def by_status(status: str, items: Optional[Iterable[Hypothesis]] = None) -> List[Hypothesis]:
    return [h for h in (items or load()) if h.status == status]


def by_family(family: str, items: Optional[Iterable[Hypothesis]] = None) -> List[Hypothesis]:
    return [h for h in (items or load()) if h.family == family]


# ── CLI ──────────────────────────────────────────────────────────────────────

_HELP = """\
Hypothesis registry — usage:
  python -m src.research.registry list                  # one-line per entry
  python -m src.research.registry list --status STATUS  # filter by status
  python -m src.research.registry list --family FAM     # filter by family
  python -m src.research.registry show ID               # full entry
  python -m src.research.registry families              # tally by family
  python -m src.research.registry statuses              # tally by status
  python -m src.research.registry validate              # parse-only sanity
"""


def _cmd_list(items: List[Hypothesis]) -> int:
    if not items:
        print("(no matching entries)")
        return 0
    width = max(len(h.id) for h in items)
    for h in items:
        last = h.evidence[-1] if h.evidence else None
        last_date = last.date if last else "—"
        print(f"  {h.id:<{width}}  [{h.status:<14}]  ({last_date})  {h.name}")
    return 0


def _cmd_show(reg: List[Hypothesis], target_id: str) -> int:
    h = next((x for x in reg if x.id == target_id), None)
    if not h:
        print(f"no entry with id={target_id!r}", file=sys.stderr)
        return 1
    print(f"id:         {h.id}")
    print(f"name:       {h.name}")
    print(f"family:     {h.family}")
    print(f"status:     {h.status}")
    print(f"code:       {h.code}")
    if h.backtest:
        print(f"backtest:   {h.backtest}")
    print(f"hypothesis: {h.hypothesis}")
    print()
    print(f"evidence ({len(h.evidence)} entries):")
    for e in h.evidence:
        prefix = f"  • {e.date}"
        if e.window:
            prefix += f" — window: {e.window}"
        if e.script:
            prefix += f" — script: {e.script}"
        if e.method:
            prefix += f" — method: {e.method}"
        print(prefix)
        print(f"      result: {e.result}")
    if h.deployment_gate:
        print()
        print("deployment gate:")
        for k, v in h.deployment_gate.items():
            print(f"  {k}: {v}")
    if h.notes:
        print()
        print("notes:")
        print(h.notes if isinstance(h.notes, str) else str(h.notes))
    return 0


def _cmd_tally(reg: List[Hypothesis], key: str) -> int:
    counts: Dict[str, int] = {}
    for h in reg:
        v = getattr(h, key)
        counts[v] = counts.get(v, 0) + 1
    width = max((len(k) for k in counts), default=0)
    for k in sorted(counts):
        print(f"  {k:<{width}}  {counts[k]}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # YAML body contains unicode (≥, ≤, ×, ±, →) that Windows' default cp1252
    # console can't encode. Re-open stdout/stderr as UTF-8 so the CLI is portable.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, Exception):
            pass

    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0

    cmd = argv[0]
    try:
        reg = load()
    except Exception as e:
        print(f"failed to load registry: {e}", file=sys.stderr)
        return 2

    if cmd == "validate":
        print(f"OK — {len(reg)} entries, all schema-valid")
        return 0

    if cmd == "list":
        items = reg
        # accept --status X and --family Y in any order
        i = 1
        while i < len(argv):
            if argv[i] == "--status" and i + 1 < len(argv):
                items = by_status(argv[i + 1], items)
                i += 2
            elif argv[i] == "--family" and i + 1 < len(argv):
                items = by_family(argv[i + 1], items)
                i += 2
            else:
                print(f"unknown flag: {argv[i]}", file=sys.stderr)
                return 2
        return _cmd_list(items)

    if cmd == "show":
        if len(argv) < 2:
            print("show requires an ID", file=sys.stderr)
            return 2
        return _cmd_show(reg, argv[1])

    if cmd == "families":
        return _cmd_tally(reg, "family")

    if cmd == "statuses":
        return _cmd_tally(reg, "status")

    print(_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
