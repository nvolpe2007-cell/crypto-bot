"""
Strategy Advisor Agent
Runs alongside the trading bot, learns from every trade, and sends
Telegram summaries with pattern analysis and strategic recommendations.

Tracks:
  - Daily wins / losses / P&L
  - Confidence score accuracy (does high confidence actually win more?)
  - Best/worst signals (OFI, lead-lag, regime, RSI)
  - Best/worst symbol and hour
  - Streak detection and advice
"""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from .trade_journal import TradeJournal, TradeRecord
from .notifications import TelegramNotifier, create_notifier_from_env
from .state import read_state

logger = logging.getLogger(__name__)

# How often the advisor checks in (seconds)
HOURLY_INTERVAL   = 3600
SUMMARY_HOUR_UTC  = 21   # send end-of-day report at 21:00 UTC (trading close)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(n, d):
    return (n / d * 100) if d else 0.0


def _grade(win_rate: float) -> str:
    if win_rate >= 65: return "🟢 Excellent"
    if win_rate >= 55: return "🟡 Good"
    if win_rate >= 45: return "🟠 Marginal"
    return "🔴 Poor"


def _conf_label(c: float) -> str:
    if c >= 85: return "Very High"
    if c >= 70: return "High"
    if c >= 55: return "Moderate"
    return "Low"


# ── Core analysis ─────────────────────────────────────────────────────────────

def _analyse_trades(records: List[TradeRecord]) -> dict:
    """
    Full pattern analysis on a list of trade records.
    Returns a dict of insights the advisor uses for messaging.
    """
    if not records:
        return {}

    wins   = [r for r in records if r.won]
    losses = [r for r in records if not r.won]
    total  = len(records)

    total_pnl  = sum(r.pnl for r in records)
    win_rate   = _pct(len(wins), total)
    avg_win    = (sum(r.pnl for r in wins)   / len(wins))   if wins   else 0
    avg_loss   = (sum(r.pnl for r in losses) / len(losses)) if losses else 0

    avg_conf_win  = (sum(r.confidence for r in wins)   / len(wins))   if wins   else 0
    avg_conf_loss = (sum(r.confidence for r in losses) / len(losses)) if losses else 0

    # Signal component correlation with wins
    signals = {
        'OFI':      ('ofi_score',      lambda r: r.ofi_score      > 15),
        'Lead-Lag': ('lead_lag_score', lambda r: r.lead_lag_score  > 15),
        'Regime':   ('regime_score',   lambda r: r.regime_score    > 8),
    }
    signal_wr = {}
    for name, (_, condition) in signals.items():
        triggered = [r for r in records if condition(r)]
        if len(triggered) >= 3:
            signal_wins = sum(1 for r in triggered if r.won)
            signal_wr[name] = _pct(signal_wins, len(triggered))

    # Win rate by symbol
    by_symbol = defaultdict(list)
    for r in records:
        by_symbol[r.symbol].append(r)
    sym_wr = {s: _pct(sum(1 for r in rs if r.won), len(rs))
              for s, rs in by_symbol.items() if len(rs) >= 2}

    # Win rate by hour (UTC)
    by_hour = defaultdict(list)
    try:
        for r in records:
            h = datetime.fromisoformat(r.closed_at.replace('Z','+00:00')).hour
            by_hour[h].append(r)
    except Exception:
        pass
    hour_wr = {h: _pct(sum(1 for r in rs if r.won), len(rs))
               for h, rs in by_hour.items() if len(rs) >= 2}

    # Win rate by regime
    by_regime = defaultdict(list)
    for r in records:
        by_regime[r.regime].append(r)
    regime_wr = {rg: _pct(sum(1 for r in rs if r.won), len(rs))
                 for rg, rs in by_regime.items() if len(rs) >= 2}

    # Confidence calibration: split into buckets
    conf_buckets = {'<55': [], '55-70': [], '70-85': [], '>85': []}
    for r in records:
        c = r.confidence
        if c < 55:   conf_buckets['<55'].append(r)
        elif c < 70: conf_buckets['55-70'].append(r)
        elif c < 85: conf_buckets['70-85'].append(r)
        else:        conf_buckets['>85'].append(r)
    conf_cal = {k: _pct(sum(1 for r in rs if r.won), len(rs))
                for k, rs in conf_buckets.items() if rs}

    # Best / worst trades
    best  = max(records, key=lambda r: r.pnl, default=None)
    worst = min(records, key=lambda r: r.pnl, default=None)

    return {
        'total':          total,
        'wins':           len(wins),
        'losses':         len(losses),
        'win_rate':       win_rate,
        'total_pnl':      total_pnl,
        'avg_win':        avg_win,
        'avg_loss':       avg_loss,
        'avg_conf_win':   avg_conf_win,
        'avg_conf_loss':  avg_conf_loss,
        'signal_wr':      signal_wr,
        'sym_wr':         sym_wr,
        'hour_wr':        hour_wr,
        'regime_wr':      regime_wr,
        'conf_calibration': conf_cal,
        'best':           best,
        'worst':          worst,
    }


def _today_records(journal: TradeJournal) -> List[TradeRecord]:
    today = datetime.now(timezone.utc).date()
    out = []
    for r in journal.records:
        try:
            dt = datetime.fromisoformat(r.closed_at.replace('Z', '+00:00'))
            if dt.date() == today:
                out.append(r)
        except Exception:
            pass
    return out


def _streak(journal: TradeJournal) -> tuple[int, str]:
    """Return (streak_length, 'win'|'loss'|'none')."""
    records = sorted(journal.records,
                     key=lambda r: r.closed_at, reverse=True)
    if not records:
        return 0, 'none'
    kind = 'win' if records[0].won else 'loss'
    count = 0
    for r in records:
        if r.won == (kind == 'win'):
            count += 1
        else:
            break
    return count, kind


# ── Message builders ──────────────────────────────────────────────────────────

def _hourly_message(journal: TradeJournal) -> str:
    today  = _today_records(journal)
    stats  = _analyse_trades(today)
    state  = read_state()
    acc    = state.get('account', {})
    equity = acc.get('total_equity', 0)
    pnl    = acc.get('total_pnl', 0)

    if not today:
        return (
            "📊 <b>Advisor Check-In</b>\n\n"
            "No trades closed yet today.\n"
            f"Account: <b>${equity:,.2f}</b>\n"
            "Watching for signals..."
        )

    streak_n, streak_k = _streak(journal)
    grade = _grade(stats['win_rate'])

    lines = [
        "📊 <b>Advisor Hourly Check-In</b>",
        "",
        f"Trades today: <b>{stats['total']}</b>  ({stats['wins']}W / {stats['losses']}L)",
        f"Win rate:     <b>{stats['win_rate']:.0f}%</b>  {grade}",
        f"P&L today:    <b>${stats['total_pnl']:+.2f}</b>",
        f"Account:      <b>${equity:,.2f}</b>",
    ]

    if streak_n >= 3:
        icon = "🔥" if streak_k == 'win' else "⚠️"
        lines.append(f"\n{icon} <b>{streak_n}-{streak_k} streak</b>")

    # Quick signal insight
    if stats.get('signal_wr'):
        best_sig = max(stats['signal_wr'], key=stats['signal_wr'].get)
        bwr = stats['signal_wr'][best_sig]
        if bwr >= 60:
            lines.append(f"\n✅ <b>{best_sig}</b> is your strongest signal today ({bwr:.0f}% WR)")

    return "\n".join(lines)


def _eod_message(journal: TradeJournal) -> str:
    today = _today_records(journal)
    all_  = journal.records
    stats_today = _analyse_trades(today)
    stats_all   = _analyse_trades(all_)

    state  = read_state()
    acc    = state.get('account', {})
    equity = acc.get('total_equity', 0)
    pnl    = acc.get('total_pnl', 0)

    lines = [
        "🧠 <b>End-of-Day Strategy Report</b>",
        f"<i>{datetime.now(timezone.utc).strftime('%A %b %d, %Y')}</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "<b>TODAY</b>",
    ]

    if today:
        grade = _grade(stats_today['win_rate'])
        lines += [
            f"  Trades:   {stats_today['total']}  ({stats_today['wins']}W / {stats_today['losses']}L)",
            f"  Win rate: {stats_today['win_rate']:.0f}%  {grade}",
            f"  P&L:      ${stats_today['total_pnl']:+.2f}",
            f"  Avg conf (wins):   {stats_today['avg_conf_win']:.0f}%",
            f"  Avg conf (losses): {stats_today['avg_conf_loss']:.0f}%",
        ]
        if stats_today['best']:
            b = stats_today['best']
            lines.append(f"  Best:  +${b.pnl:.2f} {b.symbol.split('/')[0]} (conf {b.confidence:.0f}%)")
        if stats_today['worst']:
            w = stats_today['worst']
            lines.append(f"  Worst: -${abs(w.pnl):.2f} {w.symbol.split('/')[0]} (conf {w.confidence:.0f}%)")
    else:
        lines.append("  No trades today.")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>ALL-TIME</b>  ({stats_all.get('total', 0)} trades)",
    ]
    if stats_all:
        lines += [
            f"  Win rate: {stats_all['win_rate']:.0f}%",
            f"  Total P&L: ${stats_all['total_pnl']:+.2f}",
            f"  Account: ${equity:,.2f}",
        ]

    # Signal report
    sig_wr = (stats_today if today else stats_all).get('signal_wr', {})
    if sig_wr:
        lines += ["", "<b>Signal Performance:</b>"]
        for sig, wr in sorted(sig_wr.items(), key=lambda x: -x[1]):
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            lines.append(f"  {sig:<10} {bar} {wr:.0f}%")

    # Symbol breakdown
    sym_wr = (stats_today if today else stats_all).get('sym_wr', {})
    if sym_wr:
        lines += ["", "<b>By Symbol:</b>"]
        for sym, wr in sorted(sym_wr.items(), key=lambda x: -x[1]):
            lines.append(f"  {sym.split('/')[0]}: {wr:.0f}% WR")

    # Confidence calibration
    conf_cal = (stats_today if today else stats_all).get('conf_calibration', {})
    if conf_cal:
        lines += ["", "<b>Confidence Calibration:</b>"]
        for bucket, wr in sorted(conf_cal.items()):
            lines.append(f"  {bucket}%: {wr:.0f}% WR")

    # Strategic advice
    advice = _strategic_advice(stats_today, stats_all, journal)
    if advice:
        lines += ["", "━━━━━━━━━━━━━━━━━━━", "<b>Advisor Recommendations:</b>"]
        for a in advice:
            lines.append(f"  {a}")

    return "\n".join(lines)


def _strategic_advice(today: dict, alltime: dict, journal: TradeJournal) -> List[str]:
    advice = []
    streak_n, streak_k = _streak(journal)

    # Streak warnings
    if streak_n >= 4 and streak_k == 'loss':
        advice.append("🛑 4 losses in a row — consider pausing until market conditions improve")
    elif streak_n >= 3 and streak_k == 'loss':
        advice.append("⚠️ 3-loss streak — only take Very High confidence entries until it breaks")

    # Confidence calibration check (all-time)
    conf_cal = alltime.get('conf_calibration', {})
    if conf_cal.get('>85') and conf_cal.get('55-70'):
        high_wr = conf_cal['>85']
        low_wr  = conf_cal['55-70']
        if high_wr > low_wr + 10:
            advice.append(f"✅ High-confidence trades ({high_wr:.0f}% WR) are beating low-confidence ({low_wr:.0f}%) — raise your minimum confidence threshold")
        elif high_wr < low_wr - 5:
            advice.append("⚠️ Confidence score is not predicting wins well — ML may need more training data")

    # Signal advice
    sig_wr = alltime.get('signal_wr', {})
    if sig_wr:
        worst_sig = min(sig_wr, key=sig_wr.get)
        best_sig  = max(sig_wr, key=sig_wr.get)
        if sig_wr[worst_sig] < 40:
            advice.append(f"📉 {worst_sig} has a {sig_wr[worst_sig]:.0f}% WR — it may be adding noise, not signal")
        if sig_wr[best_sig] >= 65:
            advice.append(f"📈 {best_sig} is your best signal ({sig_wr[best_sig]:.0f}% WR) — weight it higher")

    # Symbol advice (all-time)
    sym_wr = alltime.get('sym_wr', {})
    if sym_wr:
        worst_sym = min(sym_wr, key=sym_wr.get)
        if sym_wr[worst_sym] < 40:
            advice.append(f"⚡ {worst_sym.split('/')[0]} only has {sym_wr[worst_sym]:.0f}% WR — consider skipping it")

    # Win rate overall health
    total = alltime.get('total', 0)
    if total >= 20:
        wr = alltime.get('win_rate', 0)
        if wr >= 55:
            advice.append(f"✅ {wr:.0f}% overall WR with {total} trades — strategy is working, consider increasing position size")
        elif wr < 45:
            advice.append(f"🔴 {wr:.0f}% WR over {total} trades — below breakeven. Review signal filters before scaling")

    if not advice:
        advice.append("📋 Not enough data for strong recommendations yet — keep trading and check back")

    return advice


# ── Advisor loop ──────────────────────────────────────────────────────────────

class StrategyAdvisor:
    """
    Runs as an asyncio task alongside the main bot.
    Sends hourly Telegram check-ins and an end-of-day strategy report.
    """

    def __init__(self, notifier: TelegramNotifier, journal: TradeJournal,
                 hourly_interval: int = HOURLY_INTERVAL):
        self.notifier  = notifier
        self.journal   = journal
        self.interval  = hourly_interval
        self._last_eod = None   # date of last EOD report sent

    async def start(self):
        logger.info("[Advisor] Starting strategy advisor loop")
        # Small initial delay so the bot is up first
        await asyncio.sleep(30)

        while True:
            try:
                now = datetime.now(timezone.utc)

                # End-of-day report at trading close
                if now.hour == SUMMARY_HOUR_UTC and self._last_eod != now.date():
                    self._last_eod = now.date()
                    msg = _eod_message(self.journal)
                    self.notifier.send_message(msg)
                    logger.info("[Advisor] Sent end-of-day report")
                else:
                    # Only send hourly check-ins during trading hours
                    if 12 <= now.hour < SUMMARY_HOUR_UTC and now.weekday() < 5:
                        msg = _hourly_message(self.journal)
                        self.notifier.send_message(msg)
                        logger.info("[Advisor] Sent hourly check-in")

            except Exception as e:
                logger.error(f"[Advisor] Error: {e}")

            await asyncio.sleep(self.interval)


# ── Standalone runner ─────────────────────────────────────────────────────────

async def run_advisor_standalone():
    """
    Run the advisor standalone (not embedded in the bot).
    Useful for testing or running separately on the VPS.
    """
    notifier = create_notifier_from_env()
    journal  = TradeJournal()

    notifier.send_message(
        "🧠 <b>Strategy Advisor Online</b>\n\n"
        "I'm now watching your trades and will send:\n"
        "  • Hourly check-ins (12–21 UTC)\n"
        "  • End-of-day report at 21:00 UTC\n"
        "  • Pattern analysis and recommendations\n\n"
        f"Loaded <b>{len(journal.records)}</b> historical trades for analysis."
    )

    advisor = StrategyAdvisor(notifier, journal)
    await advisor.start()
