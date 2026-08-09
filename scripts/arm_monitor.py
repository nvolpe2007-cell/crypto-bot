#!/usr/bin/env python3
"""
Arm monitor — event-driven Telegram alert when an arm crosses something
meaningful, instead of the weekly digest's periodic summary (complementary,
not a replacement for scripts/weekly_report.py).

Runs proof_scorecard.py's own build_arms()/_verdict() (imported, not
reimplemented — this is never a second opinion on what "proven" means,
only a diff against the last time it was checked) and alerts on:

  1. An arm's trade count crosses the proof-sample floor (n=30) for the
     first time -- the moment its verdict becomes a real statistical read
     instead of "insufficient sample."
  2. An arm's verdict CATEGORY changes (NOT_PROVEN / FAILED / PROVEN_SINGLE
     / PROVEN / FANTASY) -- most importantly NOT_PROVEN -> PROVEN, but any
     transition is worth a human looking at, including regressions
     (PROVEN_SINGLE -> FAILED if a losing streak erases an edge).
  3. A brand-new arm appears in the scorecard for the first time.

Silent (sends nothing) when nothing meaningful changed -- the whole point
is to avoid becoming noise that gets muted. State is a small JSON snapshot
keyed by arm label; first run establishes the baseline and alerts on
anything already crossed (so a fresh deploy doesn't hide history), later
runs only alert on genuine deltas since the last run.

    python scripts/arm_monitor.py              # check + alert if needed
    python scripts/arm_monitor.py --dry-run     # print what would fire, no Telegram
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proof_scorecard as ps  # noqa: E402
from src.notifications import create_notifier_from_env  # noqa: E402

STATE_FILE = Path(ROOT / "data" / "arm_monitor_state.json")
N_MIN = ps.N_MIN


def verdict_category(verdict: str) -> str:
    """Collapse _verdict()'s free-text into a small, stable set of categories
    so we diff on MEANING, not exact wording (which can change as the
    scorecard's messages get refined without that being a real transition)."""
    if verdict.startswith("FANTASY"):
        return "FANTASY"
    if verdict.startswith("NOT PROVEN"):
        return "NOT_PROVEN"
    if verdict.startswith("FAILED"):
        return "FAILED"
    if verdict.startswith("PROVEN (single)"):
        return "PROVEN_SINGLE"
    if verdict.startswith("PROVEN ✓"):
        return "PROVEN"
    return "UNKNOWN"


def snapshot_arms() -> dict:
    """{label: {n, category, verdict}} for every current arm."""
    arms, k, t_family = ps.build_arms()
    out = {}
    for a in arms:
        verdict = ps._verdict(a, t_family, k)
        out[a["label"]] = {
            "n": a["n"],
            "category": verdict_category(verdict),
            "verdict": verdict,
        }
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def diff(prev: dict, curr: dict) -> list[str]:
    """Returns human-readable alert lines for every meaningful transition."""
    lines = []
    for label, now in curr.items():
        before = prev.get(label)
        if before is None:
            # brand-new arm -- only worth a line if it already has trades
            # (an arm appearing at n=0 the moment it's wired isn't news)
            if now["n"] > 0:
                lines.append(f"🆕 <b>{label}</b> — new arm, n={now['n']}, {now['category']}")
            continue
        crossed_floor = before["n"] < N_MIN <= now["n"]
        if crossed_floor:
            lines.append(f"📊 <b>{label}</b> — crossed n={N_MIN} "
                         f"(now n={now['n']}) — verdict is now a real statistical read: "
                         f"{now['verdict']}")
        if now["category"] != before["category"] and not crossed_floor:
            arrow = "📈" if _rank(now["category"]) > _rank(before["category"]) else "📉"
            lines.append(f"{arrow} <b>{label}</b> — verdict changed "
                         f"{before['category']} → {now['category']} "
                         f"(n={now['n']}): {now['verdict']}")
    return lines


def _rank(category: str) -> int:
    order = {"FANTASY": -1, "FAILED": 0, "NOT_PROVEN": 1, "PROVEN_SINGLE": 2, "PROVEN": 3}
    return order.get(category, 1)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    curr = snapshot_arms()
    prev = load_state()
    alerts = diff(prev, curr)

    if not alerts:
        print("[arm_monitor] no meaningful changes since last check.")
        save_state(curr)
        return 0

    message = "🔔 <b>Arm monitor</b>\n" + "\n".join(alerts)
    print(message.replace("<b>", "").replace("</b>", ""))

    if not dry_run:
        try:
            notifier = create_notifier_from_env()
            notifier._async_safe = False  # one-shot script, force sync send
            notifier.send_message(message)
        except Exception as e:
            print(f"[arm_monitor] telegram send skipped: {e}", file=sys.stderr)

    save_state(curr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
