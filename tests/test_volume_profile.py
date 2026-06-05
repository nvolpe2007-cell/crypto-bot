"""
Tests for src/volume_profile.py — the volume-by-price research signal.
"""
from src.volume_profile import volume_profile


def _bar(l, h, c, v):
    return {"l": l, "h": h, "c": c, "volume": v}


def test_poc_at_heaviest_price_band():
    # Most volume parked tightly around 100; thin tails out to 90/110.
    bars = ([_bar(99.5, 100.5, 100.0, 50.0) for _ in range(10)]
            + [_bar(89.5, 90.5, 90.0, 1.0)]
            + [_bar(109.5, 110.5, 110.0, 1.0)])
    vp = volume_profile(bars, n_bins=40)
    assert vp is not None
    assert 99.0 <= vp.poc <= 101.0          # POC sits in the heavy 100 band
    assert vp.val < vp.poc < vp.vah


def test_value_area_brackets_the_bulk_of_volume():
    bars = [_bar(95, 105, 100, 10.0) for _ in range(20)]
    vp = volume_profile(bars, n_bins=50, value_area_frac=0.70)
    # value area is a sub-range of the full profile, ordered correctly
    assert vp.lo <= vp.val < vp.vah <= vp.hi


def test_classify_above_below_and_inside():
    bars = [_bar(99, 101, 100, 100.0) for _ in range(20)]
    vp = volume_profile(bars, n_bins=20)
    assert vp.classify(vp.vah + 5) == "above_value"
    assert vp.classify(vp.val - 5) == "below_value"
    inside = vp.classify((vp.val + vp.vah) / 2.0)
    assert inside in ("in_value", "at_poc", "hvn", "lvn")


def test_lvn_marks_a_volume_gap():
    # Two volume shelves (around 100 and 120) with an empty gap at ~110.
    bars = ([_bar(99, 101, 100, 50.0) for _ in range(10)]
            + [_bar(119, 121, 120, 50.0) for _ in range(10)])
    vp = volume_profile(bars, n_bins=60)
    assert vp is not None
    # there should be at least one low-volume node in the 105–115 gap
    assert any(105 < p < 115 for p in vp.lvn)


def test_returns_none_on_degenerate_input():
    assert volume_profile([], n_bins=50) is None
    assert volume_profile([_bar(100, 100, 100, 0.0)], n_bins=50) is None  # no range/vol
