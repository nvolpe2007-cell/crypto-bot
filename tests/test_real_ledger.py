"""Tests for the real-money ledger (scripts/real_ledger.py) and its proof arm.

The point of this ledger is that it does NOT model anything, so the tests check
the two things a modelled arm can't get wrong but this one can: arithmetic on
actual fills, and the discipline flags that mark the live record as having
drifted off the measured rule.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module(tmp_path, monkeypatch):
    """Import real_ledger with its DATA dir pointed at a temp folder."""
    spec = importlib.util.spec_from_file_location(
        'real_ledger_under_test', ROOT / 'scripts' / 'real_ledger.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DATA = tmp_path
    mod.LEDGER = tmp_path / 'real_money_ledger.json'
    return mod


@pytest.fixture()
def rl(tmp_path, monkeypatch):
    return _load_module(tmp_path, monkeypatch)


# -- arithmetic on real fills -------------------------------------------------

def test_net_uses_actual_fills_and_both_fees(rl):
    t = {'qty': 2.0, 'entry_fill_price': 100.0, 'exit_fill_price': 110.0,
         'entry_fee_usd': 0.5, 'exit_fee_usd': 0.6}
    # gross 2*(110-100)=20, minus 1.10 of fees
    assert rl.net_usd(t) == pytest.approx(18.9)


def test_slippage_is_signed_so_positive_always_means_it_cost_money(rl):
    # bought ABOVE the signal close -> cost
    assert rl.slippage_bps(100.0, 101.0, 'BUY') == pytest.approx(100.0)
    # sold BELOW the signal close -> also a cost, also positive
    assert rl.slippage_bps(100.0, 99.0, 'SELL') == pytest.approx(100.0)
    # favourable fills come out negative
    assert rl.slippage_bps(100.0, 99.0, 'BUY') == pytest.approx(-100.0)
    assert rl.slippage_bps(100.0, 101.0, 'SELL') == pytest.approx(-100.0)


def test_slippage_fails_safe_on_missing_signal_price(rl):
    assert rl.slippage_bps(0.0, 101.0, 'BUY') == 0.0
    assert rl.slippage_bps(None, 101.0, 'BUY') == 0.0


def test_round_trip_cost_counts_fees_and_slippage(rl):
    t = {'qty': 1.0, 'entry_signal_price': 100.0, 'entry_fill_price': 100.0,
         'exit_signal_price': 110.0, 'exit_fill_price': 110.0,
         'entry_fee_usd': 0.25, 'exit_fee_usd': 0.25}
    # no slippage, $0.50 of fees on $100 notional = 0.50%
    assert rl.round_trip_cost_pct(t) == pytest.approx(0.50)
    # add 10bps of adverse slippage on each side -> +0.20pp
    t['entry_fill_price'] = 100.10
    t['exit_fill_price'] = 109.89
    assert rl.round_trip_cost_pct(t) == pytest.approx(0.70, abs=0.02)


def test_round_trip_cost_zero_notional_does_not_divide_by_zero(rl):
    assert rl.round_trip_cost_pct({'qty': 0.0, 'entry_fill_price': 0.0}) == 0.0


# -- the CLI round trip -------------------------------------------------------

def test_buy_then_sell_records_a_closed_trade(rl, capsys):
    assert rl.main(['buy', '--symbol', 'btc-usd', '--signal-date', '2026-09-05',
                    '--signal-price', '100', '--fill-price', '100',
                    '--usd', '500', '--fee', '1.35']) == 0
    d = rl.load()
    assert len(d['open']) == 1 and d['open'][0]['id'] == 't0001'
    assert d['open'][0]['symbol'] == 'BTC-USD'
    assert d['open'][0]['qty'] == pytest.approx(5.0)

    assert rl.main(['sell', '--id', 't0001', '--signal-date', '2026-11-02',
                    '--signal-price', '120', '--fill-price', '120',
                    '--fee', '1.62']) == 0
    d = rl.load()
    assert d['open'] == []
    assert len(d['closed']) == 1
    # 5 * (120-100) = 100 gross, minus 2.97 fees
    assert d['closed'][0]['net_usd'] == pytest.approx(97.03)


def test_ids_do_not_collide_after_a_close(rl):
    rl.main(['buy', '--symbol', 'BTC', '--signal-date', '2026-09-05',
             '--signal-price', '100', '--fill-price', '100', '--usd', '100'])
    rl.main(['sell', '--id', 't0001', '--signal-date', '2026-09-20',
             '--signal-price', '90', '--fill-price', '90'])
    rl.main(['buy', '--symbol', 'ETH', '--signal-date', '2026-09-21',
             '--signal-price', '50', '--fill-price', '50', '--usd', '100'])
    d = rl.load()
    assert d['open'][0]['id'] == 't0002'


def test_sell_on_unknown_id_is_an_error_not_a_crash(rl):
    assert rl.main(['sell', '--id', 'nope', '--signal-date', '2026-09-05',
                    '--signal-price', '1', '--fill-price', '1']) == 2


def test_buy_requires_a_size(rl):
    assert rl.main(['buy', '--symbol', 'BTC', '--signal-date', '2026-09-05',
                    '--signal-price', '100', '--fill-price', '100']) == 2


def test_skip_is_recorded(rl):
    rl.main(['skip', '--symbol', 'eth-usd', '--side', 'BUY',
             '--signal-date', '2026-09-14', '--signal-price', '2400',
             '--reason', 'travelling'])
    d = rl.load()
    assert len(d['skipped']) == 1
    assert d['skipped'][0]['symbol'] == 'ETH-USD'


def test_report_and_status_run_on_an_empty_ledger(rl):
    assert rl.main(['status']) == 0
    assert rl.main(['report']) == 0


def test_entry_week_clusters_by_signal_date(rl):
    assert rl.entry_week({'entry_signal_date': '2026-09-05'}) == '2026-W36'
    assert rl.entry_week({'entry_signal_date': 'garbage'}) == 'unknown'
    assert rl.entry_week({}) == 'unknown'


# -- the proof_scorecard arm --------------------------------------------------

def _scorecard():
    sys.path.insert(0, str(ROOT))
    import proof_scorecard
    return proof_scorecard


def _write_ledger(tmp_path, closed, skipped=()):
    (tmp_path / 'real_money_ledger.json').write_text(json.dumps(
        {'schema': 1, 'rule': 'test', 'open': [], 'closed': closed,
         'skipped': list(skipped)}))


def _trade(i, net, week_day='05', exit_reason='signal'):
    """A closed trade whose net works out to `net` on 1 unit of quantity."""
    return {'id': f't{i:04d}', 'symbol': 'BTC-USD', 'qty': 1.0,
            'entry_signal_date': f'2026-09-{week_day}',
            'entry_fill_price': 100.0, 'exit_fill_price': 100.0 + net,
            'entry_fee_usd': 0.0, 'exit_fee_usd': 0.0,
            'exit_ts': f'2026-10-{i:02d}T00:00:00+00:00',
            'exit_reason': exit_reason}


def test_arm_is_absent_until_there_are_closed_trades(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    assert ps._real_money_forward() is None          # no file
    _write_ledger(tmp_path, [])
    assert ps._real_money_forward() is None          # file, but no closed trades


def test_arm_recomputes_net_from_fills_not_a_stored_total(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    t = _trade(1, 10.0)
    t['net_usd'] = 999999.0          # a stale/hand-edited total that must be ignored
    _write_ledger(tmp_path, [t])
    arm = ps._real_money_forward()
    assert arm['n'] == 1
    assert arm['total'] == pytest.approx(10.0)


def test_arm_is_pre_registered_and_excluded_from_k(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    _write_ledger(tmp_path, [_trade(i, 5.0) for i in range(1, 4)])
    arm = ps._real_money_forward()
    assert arm['pre_registered'] is True
    assert arm['executable'] is True

    # k counts only non-pre-registered arms, so the family bar for everyone else
    # does not move just because real trading started.
    paper = dict(label='paper', executable=True, n=1, total=0.0, win_rate=0.0,
                 expectancy=0.0, t_stat=0.0, t_clustered=0.0, eff_n=1.0,
                 sharpe=0.0, max_dd=0.0, skew=0.0, kurt=3.0)
    k_without = sum(1 for a in [paper] if not a.get('pre_registered'))
    k_with = sum(1 for a in [paper, arm] if not a.get('pre_registered'))
    assert k_with == k_without == 1


def test_pre_registered_arm_is_judged_at_the_single_arm_bar(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    # An arm that clears t>2 but not a tight family bar.
    arm = dict(label='real', executable=True, pre_registered=True, n=ps.N_MIN,
               total=100.0, win_rate=0.6, expectancy=1.0, t_stat=2.5,
               t_clustered=2.5, eff_n=float(ps.N_MIN), sharpe=0.4, max_dd=-5.0,
               skew=0.0, kurt=3.0, discretionary_exits=0, skipped_signals=0)
    assert ps._verdict(arm, t_family=3.5, k=20).startswith('PROVEN ✓')
    # Same numbers WITHOUT the pre-registration are only a candidate.
    selected = dict(arm)
    selected.pop('pre_registered')
    assert ps._verdict(selected, t_family=3.5, k=20).startswith('PROVEN (single)')


@pytest.mark.parametrize('flags', [
    {'skipped_signals': 1, 'discretionary_exits': 0},
    {'skipped_signals': 0, 'discretionary_exits': 1},
])
def test_drift_blocks_the_verdict_entirely(flags):
    ps = _scorecard()
    arm = dict(label='real', executable=True, pre_registered=True, n=ps.N_MIN,
               total=100.0, win_rate=0.6, expectancy=1.0, t_stat=9.0,
               t_clustered=9.0, eff_n=float(ps.N_MIN), sharpe=2.0, max_dd=-1.0,
               skew=0.0, kurt=3.0, **flags)
    # Even with an overwhelming t-stat, an off-rule record is not judged.
    assert ps._verdict(arm).startswith('NOT JUDGED')


def test_discipline_counters_come_from_the_ledger(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    _write_ledger(tmp_path,
                  [_trade(1, 5.0), _trade(2, 5.0, exit_reason='discretionary')],
                  skipped=[{'symbol': 'ETH-USD', 'side': 'BUY',
                            'signal_date': '2026-09-14'}])
    arm = ps._real_money_forward()
    assert arm['discretionary_exits'] == 1
    assert arm['skipped_signals'] == 1
    assert ps._verdict(arm).startswith('NOT JUDGED')


def test_clean_record_is_judged_normally(tmp_path, monkeypatch):
    ps = _scorecard()
    monkeypatch.setattr(ps, 'DATA', tmp_path)
    _write_ledger(tmp_path, [_trade(i, 5.0) for i in range(1, 4)])
    arm = ps._real_money_forward()
    # 3 trades is well under N_MIN, so the honest verdict is "not enough yet".
    assert ps._verdict(arm).startswith('NOT PROVEN — only 3 trades')
