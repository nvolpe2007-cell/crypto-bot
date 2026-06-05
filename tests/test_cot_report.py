"""
Tests for src/cot_report.py — the CME-Bitcoin COT macro signal (pure logic).
"""
from src.cot_report import compute_signal


def _wk(date, lev_long, lev_short, am_long=100, am_short=100):
    return {"date": date, "lev_long": lev_long, "lev_short": lev_short,
            "am_long": am_long, "am_short": am_short}


def _ramp(latest_net):
    # 12 weeks of leveraged-fund net climbing 0..550, then `latest_net` last.
    h = [_wk(f"2026-01-{i+1:02d}", 1000 + i * 50, 1000) for i in range(12)]
    h.append(_wk("2026-02-01", 1000 + latest_net, 1000))
    return h


def test_needs_minimum_history():
    assert compute_signal([_wk("2026-01-01", 100, 100)] * 4) is None


def test_crowded_long_when_net_at_top():
    sig = compute_signal(_ramp(2000))        # far above the 0..550 ramp
    assert sig.extreme == "crowded_long"
    assert sig.bias == "caution_long"
    assert sig.lev_net_pctile >= 0.9


def test_crowded_short_when_net_at_bottom():
    sig = compute_signal(_ramp(-2000))       # far below the ramp
    assert sig.extreme == "crowded_short"
    assert sig.bias == "favor_long"          # contrarian tailwind for longs
    assert sig.lev_net_pctile <= 0.1


def test_neutral_in_the_middle():
    sig = compute_signal(_ramp(300))         # inside the ramp range
    assert sig.extreme == "none"
    assert sig.bias == "neutral"


def test_signal_reports_net_and_asset_mgr():
    sig = compute_signal(_ramp(100))
    assert sig.lev_net == 100                # 1000+100 long − 1000 short
    assert sig.n_weeks == 13
