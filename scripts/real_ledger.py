#!/usr/bin/env python3
"""Real-money trade ledger for the manually-executed Trend Signal (Honest) rule.

WHY THIS EXISTS
    Every other arm in this repo is a paper simulation whose fills we invent.
    This one records trades the owner actually placed with real money, so that
    in 12-18 months `proof_scorecard.py` can judge them against the SAME
    pre-registered bar (n>=30 & expectancy>0 & clustered t>2) instead of
    against memory. Memory of trading is systematically flattering; a file is
    not.

WHAT IT MEASURES THAT NO PAPER ARM CAN
    1. EXECUTION DRAG. Every trade stores both the signal price (the confirmed
       daily close that fired the alert) and the price actually filled. The
       gap is real slippage from being a human who acts hours after the close.
       The backtest assumed 0.54% round-trip; `report` prints what was really
       paid, so the assumption gets audited rather than trusted.
    2. DISCIPLINE. The rule only works if EVERY signal is taken and positions
       are exited only on a SELL. Two things break it, both recorded here:
         - `skip`: a signal that fired and was not taken.
         - exit_reason != 'signal': a position closed on a hunch.
       The measured failure mode (see the 12/12 result in
       pine/trend_signal_honest.pine) is that discretion RAISES win rate while
       destroying expectancy, so `report` shows the discretionary subset's P&L
       separately. If skipped/discretionary trades are where the money went,
       the record is measuring the owner, not the rule.

USAGE
    python scripts/real_ledger.py buy  --symbol BTC-USD --venue kraken \
        --signal-date 2026-09-05 --signal-price 62000 --fill-price 62150 \
        --usd 500 --fee 2.70
    python scripts/real_ledger.py sell --id t0001 \
        --signal-date 2026-11-02 --signal-price 71000 --fill-price 70880 --fee 3.10
    python scripts/real_ledger.py skip --symbol ETH-USD --side BUY \
        --signal-date 2026-09-14 --signal-price 2400 --reason "was travelling"
    python scripts/real_ledger.py status
    python scripts/real_ledger.py report

The file is plain JSON (`data/real_money_ledger.json`) and is meant to be
hand-editable when a fill was mistyped. `net_usd` is recomputed on every read,
so correcting a price or fee is enough -- never hand-edit a derived total.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
LEDGER = DATA / 'real_money_ledger.json'

SCHEMA = 1
RULE = 'trend_signal_honest: close vs SMA(100) with 2% hysteresis band, daily confirmed close'
# The cost the backtest charged. `report` compares realised drag against it.
ASSUMED_ROUND_TRIP_PCT = 0.54

EXIT_REASONS = ('signal', 'discretionary', 'other')


# -- storage ------------------------------------------------------------------

def _empty() -> dict:
    return {'schema': SCHEMA, 'rule': RULE, 'open': [], 'closed': [], 'skipped': []}


def load() -> dict:
    if not LEDGER.exists():
        return _empty()
    d = json.loads(LEDGER.read_text())
    for key in ('open', 'closed', 'skipped'):
        d.setdefault(key, [])
    d.setdefault('schema', SCHEMA)
    d.setdefault('rule', RULE)
    return d


def save(d: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, indent=2, sort_keys=False))
    tmp.replace(LEDGER)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _next_id(d: dict) -> str:
    nums = []
    for t in d['open'] + d['closed']:
        u = str(t.get('id', ''))
        if u.startswith('t'):
            try:
                nums.append(int(u[1:]))
            except ValueError:
                continue
    return 't%04d' % ((max(nums) + 1) if nums else 1)


# -- derived numbers ----------------------------------------------------------

def slippage_bps(signal_price: float, fill_price: float, side: str) -> float:
    """Cost of the fill vs the signal close, in basis points, signed so that
    POSITIVE always means it cost money (bought higher / sold lower)."""
    signal_price = float(signal_price or 0.0)
    if signal_price <= 0:
        return 0.0
    raw = (float(fill_price) - signal_price) / signal_price
    return (raw if side == 'BUY' else -raw) * 10_000.0


def net_usd(t: dict) -> float:
    """Realised net P&L of a closed trade from ACTUAL fills and ACTUAL fees.

    Long-only spot: nothing is modelled here, which is the whole point."""
    qty = float(t.get('qty', 0.0))
    gross = qty * (float(t.get('exit_fill_price', 0.0)) - float(t.get('entry_fill_price', 0.0)))
    fees = float(t.get('entry_fee_usd', 0.0)) + float(t.get('exit_fee_usd', 0.0))
    return gross - fees


def round_trip_cost_pct(t: dict) -> float:
    """Total realised round-trip cost -- fees AND slippage -- as a % of notional.
    This is the number to compare against the backtest's assumed 0.54%."""
    notional = float(t.get('qty', 0.0)) * float(t.get('entry_fill_price', 0.0))
    if notional <= 0:
        return 0.0
    fees = float(t.get('entry_fee_usd', 0.0)) + float(t.get('exit_fee_usd', 0.0))
    slip_in = slippage_bps(t.get('entry_signal_price', 0.0),
                           t.get('entry_fill_price', 0.0), 'BUY') / 10_000.0
    slip_out = slippage_bps(t.get('exit_signal_price', 0.0),
                            t.get('exit_fill_price', 0.0), 'SELL') / 10_000.0
    return (fees / notional + slip_in + slip_out) * 100.0


def entry_week(t: dict) -> str:
    """ISO year-week of the SIGNAL date -- the clustering key. Positions opened
    the same week across correlated majors are not independent bets, which is
    what proof_scorecard's effective-n correction needs."""
    raw = str(t.get('entry_signal_date') or '')[:10]
    try:
        iso = datetime.strptime(raw, '%Y-%m-%d').isocalendar()
        return '%d-W%02d' % (iso[0], iso[1])
    except ValueError:
        return 'unknown'


# -- commands -----------------------------------------------------------------

def cmd_buy(a) -> int:
    d = load()
    if a.qty is None and a.usd is None:
        print('error: give --qty or --usd', file=sys.stderr)
        return 2
    if a.fill_price <= 0:
        print('error: --fill-price must be positive', file=sys.stderr)
        return 2
    qty = a.qty if a.qty is not None else a.usd / a.fill_price
    t = {
        'id': _next_id(d),
        'symbol': a.symbol.upper(),
        'venue': a.venue,
        'entry_signal_date': a.signal_date,
        'entry_signal_price': a.signal_price,
        'entry_fill_price': a.fill_price,
        'entry_ts': a.fill_ts or _now_iso(),
        'qty': qty,
        'notional_usd': qty * a.fill_price,
        'entry_fee_usd': a.fee,
        'notes': a.notes or '',
    }
    d['open'].append(t)
    save(d)
    slip = slippage_bps(a.signal_price, a.fill_price, 'BUY')
    print("recorded BUY %s  %s qty=%.8g @ %g ($%.2f)"
          % (t['id'], t['symbol'], qty, a.fill_price, t['notional_usd']))
    print("  entry slippage vs signal close: %+.1f bps" % slip)
    return 0


def cmd_sell(a) -> int:
    d = load()
    match = [t for t in d['open'] if t['id'] == a.id]
    if not match:
        print('error: no open trade with id %s. Open: %s'
              % (a.id, [t['id'] for t in d['open']] or 'none'), file=sys.stderr)
        return 2
    t = match[0]
    t.update({
        'exit_signal_date': a.signal_date,
        'exit_signal_price': a.signal_price,
        'exit_fill_price': a.fill_price,
        'exit_ts': a.fill_ts or _now_iso(),
        'exit_fee_usd': a.fee,
        'exit_reason': a.reason,
    })
    if a.notes:
        t['notes'] = (t.get('notes', '') + ' | ' + a.notes).strip(' |')
    t['net_usd'] = net_usd(t)
    d['open'].remove(t)
    d['closed'].append(t)
    save(d)
    print("closed %s  %s  net=$%+.2f  round-trip cost %.2f%% (backtest assumed %.2f%%)"
          % (t['id'], t['symbol'], t['net_usd'], round_trip_cost_pct(t),
             ASSUMED_ROUND_TRIP_PCT))
    if a.reason != 'signal':
        print("  WARNING: exit_reason is not 'signal'. Discretionary exits are "
              "tracked separately in `report` -- the 12/12 result says they cost money.")
    return 0


def cmd_skip(a) -> int:
    d = load()
    d['skipped'].append({
        'symbol': a.symbol.upper(),
        'side': a.side.upper(),
        'signal_date': a.signal_date,
        'signal_price': a.signal_price,
        'reason': a.reason,
        'logged_ts': _now_iso(),
    })
    save(d)
    print('recorded SKIPPED %s %s on %s' % (a.side.upper(), a.symbol.upper(), a.signal_date))
    print('  %d signal(s) skipped so far. Every skip makes the live record a '
          'different strategy from the one that was measured.' % len(d['skipped']))
    return 0


def cmd_status(a) -> int:
    d = load()
    print('rule: %s' % d['rule'])
    print('ledger: %s' % LEDGER)
    if d['open']:
        print('\nOPEN (%d):' % len(d['open']))
        for t in d['open']:
            print('  %s  %-10s qty=%.8g @ %g  $%.2f  since %s'
                  % (t['id'], t['symbol'], t['qty'], t['entry_fill_price'],
                     t['notional_usd'], t['entry_signal_date']))
    else:
        print('\nOPEN: none (all cash)')
    print('CLOSED: %d   SKIPPED SIGNALS: %d' % (len(d['closed']), len(d['skipped'])))
    return 0


def cmd_report(a) -> int:
    d = load()
    closed = sorted(d['closed'], key=lambda t: t.get('exit_ts') or '')
    if not closed:
        print('No closed trades yet. Nothing to judge -- that is the correct '
              'state until signals actually fire.')
        return cmd_status(a)

    nets = [net_usd(t) for t in closed]
    n = len(nets)
    total = sum(nets)
    wins = sum(1 for x in nets if x > 0)
    srt = sorted(nets)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2

    print('=' * 72)
    print('REAL-MONEY LEDGER  --  Trend Signal (Honest)')
    print('=' * 72)
    print('trades=%d  net=$%+.2f  win=%.0f%%  expectancy=$%+.2f/trade'
          % (n, total, wins / n * 100, total / n))
    print('median trade=$%+.2f   (the backtest median LOSES -- a negative median '
          'here is expected, not a bug)' % median)

    gross_win = sum(x for x in nets if x > 0)
    gross_loss = -sum(x for x in nets if x < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
    best_share = (max(nets) / gross_win * 100) if gross_win > 0 else 0.0
    print('profit factor=%.2f   best trade=$%+.2f (%.0f%% of gross profit)'
          % (pf, max(nets), best_share))

    # Execution drag -- the number this ledger exists to measure.
    costs = [round_trip_cost_pct(t) for t in closed]
    avg_cost = sum(costs) / len(costs)
    print('\nEXECUTION DRAG')
    print('  realised round-trip cost: %.2f%% avg   assumed in backtest: %.2f%%'
          % (avg_cost, ASSUMED_ROUND_TRIP_PCT))
    if avg_cost > ASSUMED_ROUND_TRIP_PCT:
        gap = (avg_cost - ASSUMED_ROUND_TRIP_PCT) / 100.0
        drag = sum(float(t.get('notional_usd', 0.0)) for t in closed) * gap
        print('  WARNING: paying %.2fpp MORE than assumed, about $%.2f against a '
              '$%+.2f result' % (avg_cost - ASSUMED_ROUND_TRIP_PCT, drag, total))
    e_slip = [slippage_bps(t.get('entry_signal_price', 0), t.get('entry_fill_price', 0), 'BUY')
              for t in closed]
    x_slip = [slippage_bps(t.get('exit_signal_price', 0), t.get('exit_fill_price', 0), 'SELL')
              for t in closed]
    print('  avg slippage: entry %+.1f bps, exit %+.1f bps (positive = cost you money)'
          % (sum(e_slip) / n, sum(x_slip) / n))

    # Discipline -- the failure mode the rule is most vulnerable to.
    disc = [t for t in closed if t.get('exit_reason') != 'signal']
    print('\nDISCIPLINE')
    print('  signals skipped: %d' % len(d['skipped']))
    print('  discretionary exits: %d of %d' % (len(disc), n))
    if disc:
        dn = sum(net_usd(t) for t in disc)
        print('    on-signal exits:     %3d trades  net=$%+.2f' % (n - len(disc), total - dn))
        print('    discretionary exits: %3d trades  net=$%+.2f' % (len(disc), dn))
        print('    If the discretionary subset is where the money is, you are '
              'measuring yourself, not the rule.')
    if d['skipped']:
        print('    Skipped signals mean the live record is NOT the strategy that '
              'was backtested. Judge it accordingly.')
    if not disc and not d['skipped']:
        print('  clean -- every signal taken, every exit on a SELL. '
              'This record judges the rule.')

    print('\nweeks with entries: %d (clustering key used by proof_scorecard for '
          'effective-n)' % len(set(entry_week(t) for t in closed)))
    print('\nJudged against the pre-registered bar by: python proof_scorecard.py')
    print('=' * 72)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Real-money trade ledger for the Trend Signal (Honest) rule.')
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('buy', help='record a real BUY fill')
    b.add_argument('--symbol', required=True)
    b.add_argument('--venue', default='kraken')
    b.add_argument('--signal-date', required=True, help='YYYY-MM-DD of the confirmed bar')
    b.add_argument('--signal-price', type=float, required=True,
                   help='the close that fired the alert')
    b.add_argument('--fill-price', type=float, required=True, help='price you actually got')
    b.add_argument('--fill-ts', help='ISO timestamp of the fill (default: now)')
    b.add_argument('--qty', type=float)
    b.add_argument('--usd', type=float, help='notional; qty derived from fill price')
    b.add_argument('--fee', type=float, default=0.0, help='fee paid in USD')
    b.add_argument('--notes', default='')
    b.set_defaults(func=cmd_buy)

    s = sub.add_parser('sell', help='close a position at a real SELL fill')
    s.add_argument('--id', required=True)
    s.add_argument('--signal-date', required=True)
    s.add_argument('--signal-price', type=float, required=True)
    s.add_argument('--fill-price', type=float, required=True)
    s.add_argument('--fill-ts')
    s.add_argument('--fee', type=float, default=0.0)
    s.add_argument('--reason', choices=EXIT_REASONS, default='signal',
                   help="'signal' = the rule said so. Anything else is tracked separately.")
    s.add_argument('--notes', default='')
    s.set_defaults(func=cmd_sell)

    k = sub.add_parser('skip', help='record a signal you did NOT take')
    k.add_argument('--symbol', required=True)
    k.add_argument('--side', choices=('BUY', 'SELL', 'buy', 'sell'), required=True)
    k.add_argument('--signal-date', required=True)
    k.add_argument('--signal-price', type=float, default=0.0)
    k.add_argument('--reason', default='')
    k.set_defaults(func=cmd_skip)

    st = sub.add_parser('status', help='open positions and counts')
    st.set_defaults(func=cmd_status)

    r = sub.add_parser('report', help='P&L, execution drag, discipline')
    r.set_defaults(func=cmd_report)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == '__main__':
    raise SystemExit(main())
