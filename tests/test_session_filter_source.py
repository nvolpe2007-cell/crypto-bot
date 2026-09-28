"""The session gate must rate sessions from the REAL journal, not the polluted CSV.

Found 2026-09-07. `src/session_filter.py` read `data/trade_journal.csv`, which by
then held 3665 rows of which 3290 were synthetic seed/backtest rows, and where
EVERY row carried `hour_utc=12`. Two consequences, both live:

  * Asia and US could never be populated (n=0 -> NEUTRAL, permanently), so the
    time-of-day gate was structurally incapable of finding a time-of-day edge.
  * The one session it could see was rated FAVORABLE at a 59.9% win rate, while
    the real record (`data/trade_journal.json`, 228 trades spanning all 24 hours)
    says EU is UNFAVORABLE at 1.8% — the gate reported the inverse of the truth.

`proof_scorecard._directional()` already dropped exactly these synthetic ids; the
session gate did not. These tests pin the corrected behaviour.
"""
import json

import pytest

from src.session_filter import SessionEdge, _is_synthetic


# -- synthetic-id screening ---------------------------------------------------

@pytest.mark.parametrize('tid,expected', [
    ('id_0001', True),
    ('BTC_170000000', True),
    ('BTC_17000', True),
    ('real_trade_42', False),
    ('', False),
    (None, False),
])
def test_synthetic_id_detection(tid, expected):
    assert _is_synthetic(tid) is expected


def _csv(tmp_path, rows, name='j.csv'):
    p = tmp_path / name
    hdr = 'trade_id,hour_utc,pnl,won\n'
    p.write_text(hdr + ''.join(rows), encoding='utf-8')
    return p


def test_csv_loader_drops_synthetic_rows(tmp_path):
    p = _csv(tmp_path, [
        'id_1,9,5.0,true\n',            # synthetic -> dropped
        'BTC_17000abc,9,5.0,true\n',    # synthetic -> dropped
        'real_1,9,-1.0,false\n',        # kept
    ])
    recs = SessionEdge._load_journal(p)
    assert len(recs) == 1
    assert recs[0]['pnl'] == -1.0


def test_csv_loader_keeps_real_rows_unchanged(tmp_path):
    p = _csv(tmp_path, ['r1,3,2.5,true\n', 'r2,20,-1.5,false\n'])
    recs = SessionEdge._load_journal(p)
    assert [(r['hour'], r['won'], r['pnl']) for r in recs] == [
        (3, True, 2.5), (20, False, -1.5)]


# -- the JSON loader ----------------------------------------------------------

def _json(tmp_path, recs, name='j.json'):
    p = tmp_path / name
    p.write_text(json.dumps(recs), encoding='utf-8')
    return p


def test_json_loader_reads_the_real_record(tmp_path):
    p = _json(tmp_path, [
        {'trade_id': 'r1', 'hour_utc': 3, 'pnl': 1.0, 'won': True},
        {'trade_id': 'r2', 'hour_utc': 14, 'pnl': -2.0, 'won': False},
    ])
    recs = SessionEdge._load_journal_json(p)
    assert [(r['hour'], r['won'], r['pnl']) for r in recs] == [
        (3, True, 1.0), (14, False, -2.0)]


def test_json_loader_drops_synthetic_and_hourless(tmp_path):
    p = _json(tmp_path, [
        {'trade_id': 'id_9', 'hour_utc': 3, 'pnl': 9.0},     # synthetic
        {'trade_id': 'r1', 'pnl': 1.0},                      # no hour
        {'trade_id': 'r2', 'hour_utc': 5, 'pnl': 1.0},       # kept
    ])
    assert len(SessionEdge._load_journal_json(p)) == 1


def test_json_loader_infers_won_from_pnl_when_absent(tmp_path):
    p = _json(tmp_path, [{'trade_id': 'r', 'hour_utc': 5, 'pnl': 3.0}])
    assert SessionEdge._load_journal_json(p)[0]['won'] is True


def test_json_loader_is_fail_safe(tmp_path):
    assert SessionEdge._load_journal_json(tmp_path / 'missing.json') == []
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    assert SessionEdge._load_journal_json(bad) == []


def test_json_loader_accepts_a_wrapped_list(tmp_path):
    p = _json(tmp_path, {'trades': [{'trade_id': 'r', 'hour_utc': 5, 'pnl': 1.0}]})
    assert len(SessionEdge._load_journal_json(p)) == 1


# -- source preference --------------------------------------------------------

def test_default_source_prefers_json_and_never_merges(monkeypatch, tmp_path):
    """Both files exist and describe the SAME trades. Merging would double every
    n on this gate, so the JSON must win outright."""
    import src.session_filter as sf
    j = _json(tmp_path, [{'trade_id': 'r1', 'hour_utc': 3, 'pnl': 1.0}])
    c = _csv(tmp_path, ['r1,3,1.0,true\n', 'r2,4,1.0,true\n'])
    monkeypatch.setattr(sf, '_DEFAULT_JOURNAL_JSON', j)
    monkeypatch.setattr(sf, '_DEFAULT_JOURNAL', c)
    recs = SessionEdge._load_default_journal()
    assert len(recs) == 1, 'JSON must win outright, not be merged with the CSV'


def test_falls_back_to_csv_only_when_json_is_absent(monkeypatch, tmp_path):
    import src.session_filter as sf
    c = _csv(tmp_path, ['r1,3,1.0,true\n', 'r2,4,1.0,true\n'])
    monkeypatch.setattr(sf, '_DEFAULT_JOURNAL_JSON', tmp_path / 'nope.json')
    monkeypatch.setattr(sf, '_DEFAULT_JOURNAL', c)
    assert len(SessionEdge._load_default_journal()) == 2


# -- the failure mode itself --------------------------------------------------

def test_a_constant_hour_column_can_only_populate_one_session(tmp_path):
    """Regression for the actual bug: every row stamped the same hour means two
    of three sessions are permanently n=0, and the gate cannot measure anything."""
    p = _csv(tmp_path, [f'r{i},12,1.0,true\n' for i in range(200)])
    stats = SessionEdge(SessionEdge._load_journal(p)).session_stats()
    populated = [s for s, b in stats.items() if b['n'] > 0]
    assert populated == ['EU']
    assert stats['Asia']['verdict'] == 'NEUTRAL'
    assert stats['US']['verdict'] == 'NEUTRAL'


def test_synthetic_rows_can_invert_a_verdict(tmp_path):
    """Why the screening matters: enough winning synthetic rows flip a losing
    session to FAVORABLE. This is what the live gate was doing."""
    losers = [f'r{i},9,-1.0,false\n' for i in range(30)]
    winners = [f'id_{i},9,5.0,true\n' for i in range(300)]

    unscreened = SessionEdge([
        {'hour': 9, 'won': ln.split(',')[3].strip() == 'true',
         'pnl': float(ln.split(',')[2])} for ln in losers + winners])
    assert unscreened.session_stats()['EU']['verdict'] == 'FAVORABLE'

    screened = SessionEdge(SessionEdge._load_journal(_csv(tmp_path, losers + winners)))
    assert screened.session_stats()['EU']['verdict'] == 'UNFAVORABLE'
