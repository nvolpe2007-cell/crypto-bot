"""Telegram alert on every arm's trade close — profit/loss + running per-arm total.

Centralized and generic: scans every data/*_state.json (the same files
src/dashboard_data.py reads for the dashboard) for new entries in "closed",
rather than hooking each arm's own notify code. New arms show up automatically;
no per-arm wiring needed.

Run as its own short cron tick (e.g. every 5 min), independent of each arm's own
tick cadence, so a close is caught promptly regardless of which arm wrote it.
Dedup state (how many closed trades we've already alerted on, per arm) lives in
data/notify_seen.json so re-runs never double-post and a first-run backlog of
old trades doesn't spam — only NEW closes since the last seen count fire.

Sends synchronously (not via the bot's async/queued notifier, which is meant for
the long-running process and would get killed before it flushes in a one-shot
script like this).
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dashboard_data import _DISPLAY_NAMES, _closed_pnls, _data_dir, _load_json

SEEN_FILE = "notify_seen.json"
# Owner request (2026-07-01): only lev_perp and pairs_paper trade closes get a
# Telegram alert — every other arm is allowed to run (or not) without pinging.
# Allow-list rather than a skip-list so a newly added arm's state file doesn't
# silently start alerting again.
_ALLOWED_STEMS = {"lev_perp", "pairs_paper"}


def _load_seen(dd: str) -> dict:
    d = _load_json(os.path.join(dd, SEEN_FILE))
    return d if d else {}


def _save_seen(dd: str, seen: dict) -> None:
    path = os.path.join(dd, SEEN_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, indent=2)
    os.replace(tmp, path)


def _sync_notifier():
    """A TelegramNotifier that sends inline (no background thread), so a one-shot
    cron script actually waits for the HTTP call before the process exits."""
    if os.getenv("CRYPTO_TELEGRAM_MUTE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    if not token or not chat_id or not enabled:
        return None
    from src.notifications import TelegramNotifier

    return TelegramNotifier(token, chat_id, enabled, async_safe=False)


def main() -> int:
    dd = _data_dir(None)
    seen = _load_seen(dd)
    notifier = None
    sent = 0

    for path in sorted(glob.glob(os.path.join(dd, "*_state.json"))):
        stem = os.path.basename(path)[: -len("_state.json")]
        if stem not in _ALLOWED_STEMS:
            continue
        d = _load_json(path)
        if d is None or "starting_equity" not in d:
            continue
        closed = d.get("closed")
        if not isinstance(closed, list):
            continue
        pnls = _closed_pnls(closed)
        n_prev = int(seen.get(stem, {}).get("n_closed", 0))
        if len(pnls) <= n_prev:
            seen[stem] = {"n_closed": len(pnls)}
            continue

        name = _DISPLAY_NAMES.get(stem, stem)
        running_total = 0.0
        for i, pnl in enumerate(pnls):
            running_total += pnl
            if i < n_prev:
                continue
            if notifier is None:
                notifier = _sync_notifier() or False
            if notifier:
                icon = "✅" if pnl >= 0 else "❌"
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                total_str = f"+${running_total:.2f}" if running_total >= 0 else f"-${abs(running_total):.2f}"
                msg = (
                    f"{icon} <b>{name}</b> closed trade: <b>{pnl_str}</b>\n"
                    f"Arm total: <b>{total_str}</b> ({i + 1} trades)"
                )
                if notifier.send_message(msg):
                    sent += 1
        seen[stem] = {"n_closed": len(pnls)}

    _save_seen(dd, seen)
    print(f"[trade_close_notifier] {sent} alert(s) sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
