"""Unit tests for scripts/meme_cohort.py

The collector's whole value is that its cohort is not survivorship-biased. Every
test here guards one of the rules that makes that true, because each failure
mode is silent -- a corrupted cohort still produces clean-looking numbers, and
the error would only surface as a wrong base rate months later, in a study
nobody could re-run without re-collecting the sample.

Covered:

Death accounting (the critical invariants)
- a FAILED api call never counts as a miss, and never kills a pool
- a CONFIRMED absence from a successful response does count
- death requires DEATH_MISSES consecutive confirmed absences
- pools present in the response are untouched by their neighbours' deaths
- a reappearing pool resets its miss counter
- terminal ('gone') pools are never re-polled

Drained accounting
- reserve below DRAINED_USD marks 'drained'
- 'drained' is reversible and keeps being polled (it is not terminal)

Parsing / scheduling helpers
- _f / _i / _iso_to_ms coerce junk to None rather than raising
- birth_record captures discovery lag and required identity fields
- snapshot_row carries the buyers/sellers unique-wallet fields
- _interval_min implements the age tiers
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import meme_cohort as mc  # noqa: E402


# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path, monkeypatch):
    """Never let a test write into the real cohort -- it is irreplaceable."""
    monkeypatch.setattr(mc, "DATA_DIR", tmp_path)
    return tmp_path


def _registry(n=3, age_h=1.0):
    now = mc._now_ms()
    born = now - int(age_h * 3_600_000)
    return {
        "version": 1,
        "pools": {
            "P%d" % i: {
                "pool": "P%d" % i, "created_ms": born, "first_seen_ms": born,
                "status": "alive", "miss_count": 0, "last_alive_ms": born,
                "first_miss_ms": None, "drained_since_ms": None,
                "last_poll_ms": None, "snapshots": 0,
            }
            for i in range(n)
        },
    }


class FakeThrottle:
    """Returns scripted (payload, error) tuples in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.throttled = 0

    def get(self, url):
        self.calls += 1
        return self.script.pop(0) if self.script else (None, "exhausted")


def _ok(pools_present, reserve=5000.0):
    """A successful multi-pool response containing exactly `pools_present`."""
    return ({"data": [
        {"attributes": {
            "address": p,
            "base_token_price_usd": "1.0",
            "reserve_in_usd": str(reserve),
            "fdv_usd": "1000",
            "transactions": {"h24": {"buys": 10, "sells": 4,
                                     "buyers": 7, "sellers": 3}},
            "volume_usd": {"h24": "1234.5"},
            "price_change_percentage": {"h24": "12.5"},
        }} for p in pools_present]}, None)


def _redue(reg):
    """Make every pool due again, so refresh() can be called repeatedly."""
    for node in reg["pools"].values():
        node["last_poll_ms"] = None


# ── death accounting ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("error", ["http 429", "http 500", "URLError", "timeout"])
def test_failed_call_is_not_a_death(error):
    """The invariant that matters most: an API failure is not evidence.

    Without this, a single rate-limit window reads as a mass extinction and the
    cohort's mortality curve becomes a picture of our own network reliability.
    """
    reg = _registry()
    for _ in range(mc.DEATH_MISSES + 2):
        mc.refresh(reg, FakeThrottle([(None, error)]), 1)
        _redue(reg)

    for node in reg["pools"].values():
        assert node["miss_count"] == 0
        assert node["status"] == "alive"
        assert node["first_miss_ms"] is None


def test_failed_batch_is_reported_but_not_counted():
    reg = _registry()
    stats = mc.refresh(reg, FakeThrottle([(None, "http 429")]), 1)
    assert stats["failed_batches"] == 1
    assert stats["missed"] == 0
    assert stats["died"] == 0


def test_confirmed_absence_kills_after_threshold():
    reg = _registry()
    for i in range(mc.DEATH_MISSES):
        assert reg["pools"]["P2"]["status"] != "gone", "died too early (i=%d)" % i
        mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
        _redue(reg)

    assert reg["pools"]["P2"]["miss_count"] == mc.DEATH_MISSES
    assert reg["pools"]["P2"]["status"] == "gone"


def test_neighbours_survive_a_death():
    reg = _registry()
    for _ in range(mc.DEATH_MISSES):
        mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
        _redue(reg)
    assert reg["pools"]["P0"]["status"] == "alive"
    assert reg["pools"]["P1"]["status"] == "alive"


def test_death_is_recorded_as_an_interval():
    """We know it was alive at T1 and absent at T2 -- not the moment between."""
    reg = _registry()
    mc.refresh(reg, FakeThrottle([_ok(["P0", "P1", "P2"])]), 1)
    _redue(reg)
    alive_at = reg["pools"]["P2"]["last_alive_ms"]

    for _ in range(mc.DEATH_MISSES):
        mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
        _redue(reg)

    node = reg["pools"]["P2"]
    assert node["status"] == "gone"
    assert node["last_alive_ms"] == alive_at
    assert node["first_miss_ms"] is not None
    assert node["first_miss_ms"] >= alive_at


def test_reappearance_resets_the_counter():
    reg = _registry()
    mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
    _redue(reg)
    assert reg["pools"]["P2"]["miss_count"] == 1

    mc.refresh(reg, FakeThrottle([_ok(["P0", "P1", "P2"])]), 1)
    assert reg["pools"]["P2"]["miss_count"] == 0
    assert reg["pools"]["P2"]["first_miss_ms"] is None
    assert reg["pools"]["P2"]["status"] == "alive"


def test_gone_pools_are_never_repolled():
    reg = _registry()
    for _ in range(mc.DEATH_MISSES):
        mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
        _redue(reg)

    stats = mc.refresh(reg, FakeThrottle([_ok(["P0", "P1"])]), 1)
    assert stats["selected"] == 2


def test_pools_are_never_deleted():
    """Terminal states are recorded, not removed. The dead ARE the dataset."""
    reg = _registry()
    for _ in range(mc.DEATH_MISSES + 3):
        mc.refresh(reg, FakeThrottle([_ok([])]), 1)
        _redue(reg)
    assert len(reg["pools"]) == 3


# ── drained accounting ────────────────────────────────────────────────────────


def test_low_reserve_marks_drained():
    reg = _registry()
    mc.refresh(reg, FakeThrottle([_ok(["P0", "P1", "P2"], reserve=1.0)]), 1)
    for node in reg["pools"].values():
        assert node["status"] == "drained"
        assert node["drained_since_ms"] is not None


def test_drained_is_reversible_and_not_terminal():
    reg = _registry()
    mc.refresh(reg, FakeThrottle([_ok(["P0"], reserve=1.0)]), 1)
    assert reg["pools"]["P0"]["status"] == "drained"

    _redue(reg)
    stats = mc.refresh(reg, FakeThrottle([_ok(["P0", "P1", "P2"], reserve=9e3)]), 1)
    assert stats["selected"] == 3, "drained pools must keep being polled"
    assert reg["pools"]["P0"]["status"] == "alive"
    assert reg["pools"]["P0"]["drained_since_ms"] is None


# ── parsing helpers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("junk", [None, "", "abc", {}, [], True, float("nan"),
                                  float("inf"), float("-inf")])
def test_f_coerces_junk_to_none(junk):
    assert mc._f(junk) is None


def test_f_parses_the_string_numbers_the_api_returns():
    assert mc._f("1234.5") == pytest.approx(1234.5)
    assert mc._i("42") == 42


@pytest.mark.parametrize("junk", [None, "", "not-a-date", 12345, "2026-13-45"])
def test_iso_to_ms_survives_junk(junk):
    assert mc._iso_to_ms(junk) is None


def test_iso_to_ms_parses_api_format():
    """The API stamps Z-suffixed UTC; it must not be read as local time."""
    from datetime import datetime, timezone
    expected = int(datetime(2026, 8, 9, 6, 25, 4, tzinfo=timezone.utc)
                   .timestamp() * 1000)
    assert mc._iso_to_ms("2026-08-09T06:25:04Z") == expected


def test_iso_to_ms_is_timezone_aware():
    """Same instant, two spellings -- a naive parse would put them 2h apart."""
    assert (mc._iso_to_ms("2026-08-09T06:25:04Z")
            == mc._iso_to_ms("2026-08-09T08:25:04+02:00"))


def test_birth_record_captures_discovery_lag():
    now = mc._iso_to_ms("2026-08-09T06:30:04Z")
    item = {"attributes": {"address": "POOL", "name": "X / SOL",
                           "pool_created_at": "2026-08-09T06:25:04Z"},
            "relationships": {"dex": {"data": {"id": "pump-fun"}},
                              "base_token": {"data": {"id": "solana_abc"}},
                              "quote_token": {"data": {"id": "solana_sol"}}}}
    rec = mc.birth_record(item, now)
    assert rec["pool"] == "POOL"
    assert rec["dex"] == "pump-fun"
    assert rec["discovery_lag_s"] == pytest.approx(300.0)
    assert rec["status"] == "alive"


def test_birth_record_rejects_addressless_item():
    assert mc.birth_record({"attributes": {}}, mc._now_ms()) is None


def test_birth_record_tolerates_missing_creation_time():
    rec = mc.birth_record({"attributes": {"address": "P"}}, mc._now_ms())
    assert rec["created_ms"] is None
    assert rec["discovery_lag_s"] is None


def test_snapshot_row_keeps_unique_wallet_counts():
    """buys vs buyers is the free wash-trade proxy -- it must survive parsing."""
    item = _ok(["P0"])[0]["data"][0]
    row = mc.snapshot_row(item, mc._now_ms())
    assert row["buys_h24"] == 10
    assert row["buyers_h24"] == 7
    assert row["sells_h24"] == 4
    assert row["sellers_h24"] == 3
    assert row["vol_h24"] == pytest.approx(1234.5)


def test_snapshot_row_fills_absent_windows_with_none():
    item = _ok(["P0"])[0]["data"][0]
    row = mc.snapshot_row(item, mc._now_ms())
    assert row["buys_m5"] is None
    assert row["vol_m5"] is None


# ── scheduling ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("age_h,expected", [
    (0.0, 15.0), (5.9, 15.0),
    (6.0, 60.0), (47.9, 60.0),
    (48.0, 360.0), (167.9, 360.0),
    (168.0, 1440.0), (10_000.0, 1440.0),
])
def test_age_tiers(age_h, expected):
    assert mc._interval_min(age_h) == expected


def test_unknown_age_uses_the_tightest_tier():
    """Unknown age means possibly newborn; sample it hard rather than assume old."""
    assert mc._interval_min(None) == mc.TIERS[0][1]


def test_never_polled_pool_is_immediately_due():
    node = {"status": "alive", "created_ms": mc._now_ms(), "last_poll_ms": None}
    due, _ = mc._due(node, mc._now_ms())
    assert due is True


def test_recently_polled_young_pool_is_not_due():
    now = mc._now_ms()
    node = {"status": "alive", "created_ms": now, "last_poll_ms": now - 60_000}
    due, _ = mc._due(node, now)
    assert due is False


def test_budget_caps_the_selection():
    reg = _registry(n=200)
    stats = mc.refresh(reg, FakeThrottle([(None, "http 500")] * 50), budget_calls=2)
    assert stats["selected"] == 2 * mc.MULTI_BATCH


class RecordingThrottle(FakeThrottle):
    """Captures each request URL so selection order can be asserted."""

    def __init__(self, script):
        super().__init__(script)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return super().get(url)


def _requested_pools(throttle):
    """Pool addresses from a pools/multi URL, in the order they were sent."""
    return throttle.urls[0].rsplit("/", 1)[-1].split(",")


def test_youngest_pools_are_prioritised():
    """Young pools carry both the price action and the mortality, so when the
    budget cannot cover everything they must be the ones that get covered."""
    now = mc._now_ms()
    reg = {"version": 1, "pools": {}}
    for name, age_h in (("old", 500.0), ("young", 1.0), ("mid", 100.0)):
        reg["pools"][name] = {
            "pool": name, "created_ms": now - int(age_h * 3_600_000),
            "first_seen_ms": now, "status": "alive", "miss_count": 0,
            "last_alive_ms": None, "first_miss_ms": None,
            "drained_since_ms": None, "last_poll_ms": None, "snapshots": 0,
        }
    th = RecordingThrottle([_ok(["young", "mid", "old"])])
    mc.refresh(reg, th, budget_calls=1)

    assert _requested_pools(th) == ["young", "mid", "old"]


def test_budget_shortfall_drops_the_oldest_not_the_youngest():
    now = mc._now_ms()
    reg = {"version": 1, "pools": {}}
    # 31 pools, one batch of budget -> exactly one must be left behind.
    for i in range(mc.MULTI_BATCH + 1):
        reg["pools"]["P%02d" % i] = {
            "pool": "P%02d" % i,
            "created_ms": now - int((i + 1) * 3_600_000),   # P00 youngest
            "first_seen_ms": now, "status": "alive", "miss_count": 0,
            "last_alive_ms": None, "first_miss_ms": None,
            "drained_since_ms": None, "last_poll_ms": None, "snapshots": 0,
        }
    th = RecordingThrottle([_ok([])])
    stats = mc.refresh(reg, th, budget_calls=1)

    sent = _requested_pools(th)
    assert stats["selected"] == mc.MULTI_BATCH
    assert "P00" in sent, "youngest must never be the one dropped"
    assert "P30" not in sent, "oldest should be the one dropped"
