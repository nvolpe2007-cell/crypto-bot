#!/usr/bin/env python3
"""
BRAIN OVERSEER — a portfolio-level risk review across EVERY arm.

This is option A: the brain "thinks about all the arms" without driving any of
them. It reads each arm's live book from disk (own-book paper arms + the
attribution ledger for the funding/directional arms), asks the brain for a
RISK REVIEW (concentration, fighting-the-tape, drawdown, cost discipline), and
posts it to Telegram. It is OBSERVABILITY ONLY: it executes nothing and changes
no positions.

  python brain_overseer.py        # gather books -> review -> Telegram -> exit

FAIL-SAFE: no ANTHROPIC_API_KEY or any API/IO error -> logs and exits cleanly,
touching no arm. Reuses src.trade_brain.TradeBrain (same lazy client / fail-safe
pattern as brain_paper.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DATA = Path(os.getenv("BRAIN_OVERSEER_DATA_DIR", "data"))
STATE_FILE = Path(os.getenv("BRAIN_OVERSEER_STATE_FILE",
                            str(DATA / "brain_overseer_state.json")))
ATTRIB_DB = Path(os.getenv("ATTRIBUTION_DB", str(DATA / "attribution.db")))

# Own-book paper arms: file -> friendly name. These store equity/positions/closed.
OWN_BOOK_ARMS = {
    "brain_paper_state.json": "AI Brain (discretionary)",
    "regime_arm_state.json": "Regime intraday (2-sided)",
    "tsmom_fast_state.json": "TSMOM fast (SMA50)",
    "tsmom_paper_state.json": "TSMOM slow (SMA200)",
    "tsmom_ls_state.json": "TSMOM long/short perp",
    "swing_paper_state.json": "Swing 4h majors",
    "conf_paper_state.json": "Confluence trend (long-only)",
    "lev_perp_state.json": "Leveraged perp 3x",
}


def _positions_brief(pos) -> list[dict]:
    """Normalise positions (dict-keyed-by-symbol or list) to a short list."""
    items = pos.items() if isinstance(pos, dict) else enumerate(pos or [])
    out = []
    for _, p in items:
        if not isinstance(p, dict):
            continue
        side = p.get("side")
        out.append({
            "symbol": p.get("symbol"),
            "dir": ("long" if (side or 0) > 0 else "short" if (side or 0) < 0 else "?"),
            "entry": p.get("entry") or p.get("entry_price"),
            "size_usd": p.get("size_usd"),
        })
    return out


def _own_book_summary(path: Path, name: str) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except Exception:
        return None
    start = d.get("starting_equity")
    eq = d.get("equity_mtm", d.get("equity"))
    closed = d.get("closed", []) or []
    recent = []
    for c in (closed[-3:] if isinstance(closed, list) else []):
        if isinstance(c, dict):
            recent.append({"symbol": c.get("symbol"),
                           "pnl": c.get("pnl", c.get("net_pnl")),
                           "reason": c.get("reason")})
    return {
        "arm": name,
        "equity_mtm": round(eq, 2) if isinstance(eq, (int, float)) else eq,
        "starting_equity": start,
        "pnl": (round(eq - start, 2)
                if isinstance(eq, (int, float)) and isinstance(start, (int, float))
                else None),
        "halted": d.get("halted", False),
        "open_positions": _positions_brief(d.get("positions", {})),
        "closed_count": len(closed) if isinstance(closed, list) else None,
        "recent_closed": recent,
    }


def _ledger_summary() -> list[dict]:
    """Per-arm net from the unified attribution ledger (funding + directional arms)."""
    if not ATTRIB_DB.exists():
        return []
    try:
        c = sqlite3.connect(f"file:{ATTRIB_DB}?mode=ro", uri=True)
        rows = c.execute(
            "select arm, count(*), round(sum(gross_pnl),2), "
            "round(sum(fees_paid+slippage_cost),2), round(sum(net_pnl),2) "
            "from fills group by arm order by sum(net_pnl)").fetchall()
        c.close()
    except Exception:
        return []
    return [{"arm": f"ledger:{r[0]}", "fills": r[1], "gross": r[2],
             "cost": r[3], "net": r[4]} for r in rows]


def gather_portfolio() -> dict:
    arms = []
    for fname, name in OWN_BOOK_ARMS.items():
        s = _own_book_summary(DATA / fname, name)
        if s is not None:
            arms.append(s)
    return {"own_book_arms": arms, "attribution_ledger": _ledger_summary()}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"reviews": [], "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


_SEV_ICON = {"info": "ℹ️", "warn": "⚠️", "critical": "🚨"}
_RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _format_telegram(result, now) -> str:
    lines = [f"🧠🛡️ <b>AI Brain — Portfolio Risk Review</b>",
             f"{_RISK_ICON.get(result.overall_risk, '⚪')} Overall risk: "
             f"<b>{result.overall_risk.upper()}</b>  "
             f"<i>{now:%Y-%m-%d %H:%M} UTC</i>",
             "",
             result.portfolio_note]
    if result.flags:
        lines.append("")
        for f in result.flags:
            icon = _SEV_ICON.get(f.get("severity", "info"), "•")
            lines.append(f"{icon} <b>{f.get('arm', '?')}</b>: {f.get('note', '')}")
    if result.suggestions:
        lines.append("")
        lines.append("<i>Non-binding observations (not executed):</i>")
        for s in result.suggestions:
            lines.append(f"  • {s}")
    return "\n".join(lines)


def main() -> int:
    from src.trade_brain import TradeBrain
    state = _load_state()
    brain = TradeBrain()
    if not brain.available():
        print("[brain_overseer] no ANTHROPIC_API_KEY — overseer idle (no review).")
        return 0

    portfolio = gather_portfolio()
    if not portfolio["own_book_arms"] and not portfolio["attribution_ledger"]:
        print("[brain_overseer] no arm books found — nothing to review.")
        return 0

    now = datetime.now(timezone.utc)
    result = brain.review(portfolio, now)
    if not result.ok:
        print(f"[brain_overseer] review unavailable ({result.error}) — skipped.")
        return 0

    msg = _format_telegram(result, now)
    if os.getenv("BRAIN_OVERSEER_NOTIFY", "1") == "1":
        try:
            from src.notifications import create_notifier_from_env
            create_notifier_from_env().send_message(msg)
        except Exception as e:
            print(f"[brain_overseer] telegram skipped: {e}")

    state["reviews"].append({
        "ts": now.isoformat(), "overall_risk": result.overall_risk,
        "portfolio_note": result.portfolio_note, "flags": result.flags,
        "suggestions": result.suggestions,
        "tokens": [result.input_tokens, result.output_tokens]})
    state["reviews"] = state["reviews"][-50:]
    _save_state(state)
    print(f"[brain_overseer] {now:%Y-%m-%d %H:%M} UTC risk={result.overall_risk} "
          f"flags={len(result.flags)} arms={len(portfolio['own_book_arms'])} "
          f"model={result.model} tok={result.input_tokens}/{result.output_tokens}")
    print(result.portfolio_note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
