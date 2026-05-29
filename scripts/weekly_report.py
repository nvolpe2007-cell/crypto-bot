"""
Weekly health-check report for the crypto-bot.

Runs on the VPS via systemd timer (deploy/weekly_report.timer). Reads the
journal + bot log + funding-arb state, builds a breakdown of the past 7 days,
sends a Telegram summary with concrete tuning recommendations. Read-only —
this script never modifies bot config, never applies changes. Its job is to
make the bot's behavior visible so the operator can decide what to tune.

Sections in the report:
  1. Trades executed (count, win rate, net P&L, by symbol)
  2. Skip reasons (which gate killed the most setups)
  3. Triangular-arb opportunities (count, best edge, paper P&L)
  4. Calibration status (active? Brier raw vs cal?)
  5. Funding-arb Kraken arm (cumulative P&L, open positions)
  6. Tuning recommendations (rule-based)

Run manually:
  cd /opt/crypto-bot && ./venv/bin/python scripts/weekly_report.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make the script runnable from /opt/crypto-bot regardless of cwd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.notifications import TelegramNotifier   # noqa: E402

# Load .env from the project root so TELEGRAM_* are available
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env(ROOT / ".env")

WINDOW_DAYS = int(os.getenv("WEEKLY_REPORT_WINDOW_DAYS", "7"))
NOW         = datetime.now(timezone.utc)
WINDOW_START = NOW - timedelta(days=WINDOW_DAYS)

JOURNAL_CSV       = ROOT / "data" / "trade_journal.csv"
BOT_LOG           = ROOT / "logs" / "bot.log"
CALIBRATION_JSON  = ROOT / "logs" / "calibration.json"
FUNDING_KRAKEN    = ROOT / "data" / "funding_arb_kraken_state.json"


# ── Section 1 + 2 helpers ─────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # CSV may have "2026-05-22T23:45:04.875605+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def load_recent_trades() -> List[dict]:
    if not JOURNAL_CSV.exists():
        return []
    rows: List[dict] = []
    with JOURNAL_CSV.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            closed = _parse_iso(row.get("closed_at", ""))
            if closed is None or closed < WINDOW_START:
                continue
            rows.append(row)
    return rows


# Match lines like: "2026-05-28 03:23:13,675 - ... - INFO - [SKIP BUY] BTC/USD — spread 0.123% = 1.8× median 0.069%"
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})(?:,\d+)?\s.*?(?P<body>\[(?:SKIP\s\w+|TRIARB|CALIB|FUNNEL|DUAL-(?:FLIP|REJECT))\].*)$"
)
_SKIP_REASON = re.compile(r"\[SKIP\s\w+\][^—-]*[—-]\s*(?P<reason>.+?)(?:\s+\(|$)")


def scan_log_window() -> dict:
    """Walk bot.log, bucket SKIP reasons + count TRIARB opportunities within window."""
    skips: Counter = Counter()
    triarb_edges: List[float] = []
    triarb_count = 0
    funnel_seen: Counter = Counter()
    dual_flip = 0
    dual_reject = 0
    if not BOT_LOG.exists():
        return {"skips": skips, "triarb_count": 0, "triarb_best_bps": None,
                "triarb_total_pnl": 0.0, "funnel": funnel_seen,
                "dual_flip": 0, "dual_reject": 0}

    # bot.log can be hundreds of MB; we walk line-by-line and stop early if a line's
    # parsed timestamp is older than WINDOW_START AFTER having seen at least one
    # in-window line (avoids cost of a full read).
    in_window_seen = False
    triarb_pnl = 0.0
    with BOT_LOG.open(errors="ignore") as fh:
        for raw in fh:
            m = _LOG_LINE.search(raw)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts < WINDOW_START:
                # Logs are append-only / mostly chronological; once we're past the
                # window we can break — but only after having seen in-window data,
                # otherwise an early-log range would short-circuit.
                if in_window_seen:
                    break
                continue
            in_window_seen = True
            body = m.group("body")
            if body.startswith("[SKIP "):
                rm = _SKIP_REASON.search(body)
                reason = rm.group("reason").strip() if rm else "unknown"
                # Bucket by the first word(s) so vpin/spread/atr/etc collapse
                bucket = _bucket_skip(reason)
                skips[bucket] += 1
            elif body.startswith("[TRIARB]"):
                # Each TRIARB log line may include 1+ cycles separated by "|"
                for chunk in body.split("|"):
                    em = re.search(r"edge=([\+\-]?\d+\.?\d*)bps.*?paper_pnl=\$([\+\-]?\d+\.?\d*)", chunk)
                    if em:
                        triarb_count += 1
                        edge = float(em.group(1))
                        pnl  = float(em.group(2))
                        triarb_edges.append(edge)
                        triarb_pnl += pnl
            elif body.startswith("[FUNNEL]"):
                funnel_seen[body[:80]] += 1
            elif body.startswith("[DUAL-FLIP]"):
                dual_flip += 1
            elif body.startswith("[DUAL-REJECT]"):
                dual_reject += 1
    return {
        "skips": skips,
        "triarb_count": triarb_count,
        "triarb_best_bps": max(triarb_edges) if triarb_edges else None,
        "triarb_total_pnl": triarb_pnl,
        "funnel": funnel_seen,
        "dual_flip": dual_flip,
        "dual_reject": dual_reject,
    }


def _bucket_skip(reason: str) -> str:
    r = reason.lower()
    if "noisy tape" in r or "both directions pass" in r: return "dual_noisy"
    if "no retail shorting" in r: return "spot_short_blocked"
    if "vpin" in r:       return "vpin_safe"
    if "spread" in r:     return "spread_normal"
    if "atr/px" in r or "atr_alive" in r: return "atr_alive"
    if "tier=" in r and "below floor" in r: return "tier_floor"
    if r.startswith("p=") and "<" in r:   return "calibrated_p"
    if "kill" in r:       return "kill_filter"
    if "cooldown" in r:   return "cooldown"
    if "correlated" in r: return "correlated"
    if "size below floor" in r: return "size_floor"
    if "ws stale" in r:   return "ws_stale"
    if "ofi=" in r:       return "ofi_aligned"
    if "rsi" in r:        return "rsi_overbought_oversold"
    return reason.split()[0] if reason else "unknown"


# ── Section 4: calibration ───────────────────────────────────────────────────

def calibration_status(trades: List[dict]) -> dict:
    if not CALIBRATION_JSON.exists():
        return {"active": False, "reason": "no calibration.json on disk"}
    try:
        st = json.loads(CALIBRATION_JSON.read_text())
    except Exception as e:
        return {"active": False, "reason": f"unreadable: {e}"}
    knots_x = st.get("x") or []
    knots_y = st.get("y") or []
    active = len(knots_x) >= 2 and len(knots_y) >= 2
    out = {
        "active":  active,
        "n_fit":   st.get("n_fit", 0),
        "shrink":  st.get("shrink", 0.0),
        "n_seen":  st.get("n_seen", 0),
    }
    if not active or not trades:
        return out
    # Compute in-window Brier raw vs calibrated. Calibrated probability is
    # piecewise-linear interpolation of (knots_x, knots_y) clamped at ends.
    import numpy as np
    raw_probs = []
    cals      = []
    wons      = []
    kx = np.asarray(knots_x, dtype=float)
    ky = np.asarray(knots_y, dtype=float)
    for row in trades:
        try:
            p = float(row.get("prob_win") or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        if p <= 0:
            continue
        try:
            w = 1.0 if str(row.get("won", "")).lower() == "true" else 0.0
        except Exception:
            continue
        raw_probs.append(p)
        cals.append(float(np.interp(p, kx, ky)))
        wons.append(w)
    if not raw_probs:
        return out
    raw_arr = np.asarray(raw_probs)
    cal_arr = np.asarray(cals)
    won_arr = np.asarray(wons)
    out["brier_raw"]      = float(((raw_arr - won_arr) ** 2).mean())
    out["brier_cal"]      = float(((cal_arr - won_arr) ** 2).mean())
    out["brier_lift_pct"] = round((1 - out["brier_cal"] / out["brier_raw"]) * 100, 1) if out["brier_raw"] else None
    out["resolved_in_window"] = len(raw_probs)
    # Degenerate-fit detection: when all observed outcomes are the same value
    # (all losers or all winners), Brier improvement is trivially achievable by
    # collapsing every prediction to that value. The calibrator has learned
    # "predict X always" — not a real edge. Flag so the recs engine doesn't
    # treat the +99% Brier headline as a green light to tighten.
    win_rate_in_window = float(won_arr.mean())
    out["win_rate_in_window"] = round(win_rate_in_window, 3)
    cal_mean = float(cal_arr.mean())
    cal_std  = float(cal_arr.std())
    out["calibrator_degenerate"] = (
        # Outcomes are all one-sided
        (win_rate_in_window <= 0.05 or win_rate_in_window >= 0.95)
        # AND the calibrator collapses inputs to a near-constant
        and cal_std < 0.05
    )
    return out


# ── Section 5: funding-arb Kraken arm ─────────────────────────────────────────

def funding_kraken_status() -> dict:
    if not FUNDING_KRAKEN.exists():
        return {"available": False}
    try:
        st = json.loads(FUNDING_KRAKEN.read_text())
    except Exception as e:
        return {"available": False, "reason": str(e)}
    opens   = st.get("open", {})
    closed  = st.get("closed", [])
    cum_net = 0.0
    closed_in_window = 0
    for p in closed:
        ct = _parse_iso(p.get("close_time_iso", ""))
        funding   = float(p.get("funding_collected") or 0.0)
        entry_cost = float(p.get("entry_cost") or 0.0)
        cum_net += funding - entry_cost
        if ct and ct >= WINDOW_START:
            closed_in_window += 1
    # Open positions: add running funding minus already-paid entry cost
    open_unrealized = 0.0
    for _, p in opens.items():
        open_unrealized += float(p.get("funding_collected") or 0.0) - float(p.get("entry_cost") or 0.0)
    return {
        "available":         True,
        "open_count":        len(opens),
        "closed_total":      len(closed),
        "closed_in_window":  closed_in_window,
        "cum_net_pnl":       round(cum_net, 4),
        "open_unrealized":   round(open_unrealized, 4),
    }


# ── Recommendations engine ───────────────────────────────────────────────────

def recommendations(trades: List[dict], log_stats: dict, calib: dict, kraken: dict) -> List[str]:
    recs: List[str] = []
    skips = log_stats["skips"]
    total_skips = sum(skips.values())
    n_trades    = len(trades)

    # 1. Trade volume sanity
    if n_trades == 0 and total_skips == 0:
        recs.append("⚠️ No trades AND no skips logged this week — bot may not be running. Check `systemctl status crypto-bot`.")
    elif n_trades == 0:
        recs.append(f"No trades executed (gates rejected {total_skips}). Working as designed if calibration is still warming up.")

    # 2. Dominant skip reason
    if total_skips > 50:
        top = skips.most_common(3)
        top_label = top[0][0]
        top_share = top[0][1] / total_skips
        if top_share > 0.6:
            recs.append(
                f"🎯 {top_share*100:.0f}% of skips are `{top_label}` ({top[0][1]} of {total_skips}). "
                f"Inspect this gate — either it's correctly the binding constraint or it's too tight."
            )
        if "vpin_safe" in skips and skips["vpin_safe"] / total_skips > 0.4:
            recs.append("VPIN is rejecting >40% of setups — consider raising `VPIN_TOXIC_THRESHOLD` to 0.65 if you want more trade volume.")
        if "calibrated_p" in skips and skips["calibrated_p"] / total_skips > 0.5:
            recs.append("Calibrated-P floor is the dominant rejection — gates are tight (good for precision). If trade count is too low, drop `PROB_GATE_MIN_P` to 0.62.")

    # 3. Calibration
    if calib.get("active"):
        if calib.get("calibrator_degenerate"):
            wr = calib.get("win_rate_in_window", 0.0)
            recs.append(
                f"⚠️ Calibration is DEGENERATE — fit on one-sided outcomes "
                f"(win_rate_in_window={wr*100:.0f}%). It now predicts ~constant for all inputs, "
                f"which means the gate will reject everything regardless of setup quality. "
                f"Do NOT tighten `PROB_GATE_MIN_P` based on the Brier number. "
                f"Once fresh post-gate trades accumulate ≥40 resolved with mixed outcomes, "
                f"delete `logs/calibration.json` to force a refit."
            )
        else:
            lift = calib.get("brier_lift_pct")
            if lift is not None:
                if lift > 5:
                    recs.append(f"✅ Calibration helping: Brier improved {lift:+.1f}% vs raw. Safe to tighten `PROB_GATE_MIN_P` toward 0.70.")
                elif lift < -2:
                    recs.append(f"⚠️ Calibration *hurting*: Brier {lift:+.1f}% worse than raw. The gate may be overfit to old trades — consider deleting `logs/calibration.json` so it refits on fresh data.")
                else:
                    recs.append(f"Calibration in noise band ({lift:+.1f}% Brier change). Keep accumulating trades before tuning.")
    else:
        recs.append(f"Calibration not active (n_fit={calib.get('n_fit', 0)}). Needs ~40 resolved trades; gate currently runs on RAW stacked probability.")

    # 4. Triangular arb
    if log_stats["triarb_count"] == 0:
        recs.append("No `[TRIARB]` opportunities cleared the 5bp gate this week. Real edges may not exist at retail fees — execution code is not worth building yet.")
    else:
        best = log_stats["triarb_best_bps"]
        pnl  = log_stats["triarb_total_pnl"]
        recs.append(f"📈 {log_stats['triarb_count']} triarb opps logged (best {best:+.1f}bps, paper P&L ${pnl:+.4f}). If this stays positive, next step is building IOC execution.")

    # 5. Funding-arb Kraken arm
    if kraken.get("available"):
        cum   = kraken["cum_net_pnl"]
        unrl  = kraken["open_unrealized"]
        nopen = kraken["open_count"]
        if cum > 0:
            recs.append(f"💰 Kraken funding arm cumulative net: ${cum:+.4f} ({nopen} open, unrealized ${unrl:+.4f}). Consider lowering `FUNDING_ARB_KRAKEN_COST_FRAC` to 0.0050 if maker fills are realistic.")
        elif cum < -1:
            recs.append(f"⚠️ Kraken funding arm net negative: ${cum:+.4f}. Costs likely understated — raise `FUNDING_ARB_KRAKEN_COST_FRAC` to 0.010.")

    if not recs:
        recs.append("No tuning recommended — keep accumulating data.")
    return recs


# ── Build + send ─────────────────────────────────────────────────────────────

def render_report(trades: List[dict], log_stats: dict, calib: dict, kraken: dict) -> str:
    n = len(trades)
    wins = sum(1 for t in trades if str(t.get("won", "")).lower() == "true")
    losses = n - wins
    win_rate = (wins / n * 100) if n else 0.0
    pnl_total = sum(float(t.get("pnl") or 0.0) for t in trades)

    by_sym: Dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        s = t.get("symbol", "?")
        by_sym[s]["n"] += 1
        if str(t.get("won", "")).lower() == "true":
            by_sym[s]["wins"] += 1
        by_sym[s]["pnl"] += float(t.get("pnl") or 0.0)

    skips = log_stats["skips"]
    total_skips = sum(skips.values())

    lines: List[str] = []
    lines.append(f"<b>📊 Weekly bot report — {WINDOW_DAYS}d ending {NOW.strftime('%Y-%m-%d %H:%M UTC')}</b>\n")

    # 1. Trades
    lines.append(f"<b>1. Trades</b>: {n} executed | {wins}W/{losses}L | win {win_rate:.1f}% | net ${pnl_total:+.4f}")
    for sym, s in sorted(by_sym.items(), key=lambda kv: -kv[1]["n"])[:5]:
        wr = (s["wins"] / s["n"] * 100) if s["n"] else 0.0
        lines.append(f"  • {sym}: {s['n']} | win {wr:.0f}% | ${s['pnl']:+.4f}")

    # 2. Skips
    lines.append(f"\n<b>2. Skip reasons</b>: {total_skips} total")
    for reason, count in skips.most_common(8):
        share = (count / total_skips * 100) if total_skips else 0.0
        lines.append(f"  • {reason}: {count} ({share:.1f}%)")

    # 3. Triangular arb
    lines.append(f"\n<b>3. Triangular arb</b>: {log_stats['triarb_count']} opps | "
                 f"best {(log_stats['triarb_best_bps'] or 0):+.1f}bps | "
                 f"paper P&amp;L ${log_stats['triarb_total_pnl']:+.4f}")

    # 3b. Dual-direction probe activity
    df = log_stats.get("dual_flip", 0)
    dr = log_stats.get("dual_reject", 0)
    if df or dr:
        lines.append(f"\n<b>3b. Dual-direction probe</b>: {df} signal flips | {dr} noisy-tape rejects")

    # 4. Calibration
    if calib.get("active"):
        b_raw = calib.get("brier_raw")
        b_cal = calib.get("brier_cal")
        if b_raw is not None and b_cal is not None:
            lines.append(f"\n<b>4. Calibration</b>: ACTIVE | n_fit={calib['n_fit']} shrink={calib['shrink']:.2f} | "
                         f"Brier raw {b_raw:.4f} → cal {b_cal:.4f} ({calib.get('brier_lift_pct', 0):+.1f}%)")
        else:
            lines.append(f"\n<b>4. Calibration</b>: ACTIVE | n_fit={calib['n_fit']} shrink={calib['shrink']:.2f} | no resolved-in-window trades to score")
    else:
        lines.append(f"\n<b>4. Calibration</b>: INACTIVE | n_seen={calib.get('n_seen', 0)} (need ≥40 resolved)")

    # 5. Funding arb Kraken
    if kraken.get("available"):
        lines.append(f"\n<b>5. Funding arb (Kraken)</b>: {kraken['open_count']} open | "
                     f"{kraken['closed_in_window']} closed this week | "
                     f"cum net ${kraken['cum_net_pnl']:+.4f} | unrealized ${kraken['open_unrealized']:+.4f}")
    else:
        lines.append("\n<b>5. Funding arb (Kraken)</b>: state file missing")

    # 6. Recommendations
    lines.append("\n<b>6. Recommendations</b>:")
    for r in recommendations(trades, log_stats, calib, kraken):
        lines.append(f"  • {r}")

    return "\n".join(lines)


def main() -> int:
    trades    = load_recent_trades()
    log_stats = scan_log_window()
    calib     = calibration_status(trades)
    kraken    = funding_kraken_status()
    report    = render_report(trades, log_stats, calib, kraken)

    # Print to stdout for systemd journal
    print(report.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[weekly_report] TELEGRAM_BOT_TOKEN/CHAT_ID missing — not sending Telegram", file=sys.stderr)
        return 0
    notifier = TelegramNotifier(token, chat_id)
    ok = notifier.send_message(report)
    if not ok:
        print("[weekly_report] Telegram send returned False", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
