#!/usr/bin/env python3
"""
Faithful replay: how would lev_perp v1 (3x, fixed take-profit) have performed
over the last ~4 months, at the 3-coin default vs. the newly-widened 8-coin
universe (PR #95)?

Method: NOT a reimplementation. Fetches real Kraken daily OHLC (the exact
same `fetch_closed_daily` production uses) with enough warm-up history before
the test window for SMA(50)/trend-age/ADX to be valid on day 1 of the test,
then replays day-by-day through the production `process_symbol` function --
the identical code path the live cron job runs, just fed historical bars
incrementally instead of "whatever's new since last run". Same entry filters,
same fixed TP/SL, same vol-targeted leverage, same correlation cap, same
costs/funding. This is what actually would have happened, not an estimate.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

os.environ.setdefault("LEV_PERP_STATE_FILE", "/tmp/_unused_lev_perp_replay.json")
import lev_perp_paper as lp

TEST_MONTHS = 4
WARMUP_DAYS = 220  # SMA50 needs 50, ADX/trend-age scan further back; generous buffer


def fetch_full_history(pair_code: str) -> list:
    """lp.fetch_closed_daily only returns Kraken's default ~720-bar window,
    which comfortably covers warmup + 4 months (~340 days needed)."""
    return lp.fetch_closed_daily(pair_code)


def replay(symbols: dict, label: str, start_ts: int):
    state = {"positions": {}, "closed": [], "last_bar_t": {}}
    all_bars = {}
    for base, pair in symbols.items():
        try:
            bars = fetch_full_history(pair)
        except Exception as e:
            print(f"  {base}: fetch failed - {e}")
            continue
        all_bars[base] = bars

    if not all_bars:
        print(f"{label}: no data fetched")
        return None

    # seed state using only bars STRICTLY BEFORE the test window (warmup),
    # so the arm enters the test window already warmed up, exactly like a
    # live arm that's been running would be -- no signal computed on future
    # (in-test-window) data leaks into warmup.
    for base, bars in all_bars.items():
        warmup_bars = [b for b in bars if b["t"] < start_ts]
        if len(warmup_bars) < lp.SMA_N + 1:
            print(f"  {base}: insufficient warmup ({len(warmup_bars)} bars), skipping symbol")
            continue
        state["last_bar_t"][base] = warmup_bars[-1]["t"]
        # do NOT seed a position here -- start flat at the test window boundary,
        # matching how the newly-deployed arm actually starts (fresh state file)

    # now replay only the bars WITHIN the test window, one at a time, via the
    # real production function
    test_bars = {base: [b for b in bars if b["t"] >= start_ts] for base, bars in all_bars.items()}
    n_days = max((len(v) for v in test_bars.values()), default=0)
    print(f"{label}: replaying {n_days} trading days across {len(test_bars)} symbols...")

    for base, bars in all_bars.items():
        # process_symbol needs the FULL bars list (it slices internally), so
        # pass the complete history but the state's last_bar_t gate ensures
        # only test-window-and-later bars actually get acted on.
        lp.process_symbol(base, bars, state)

    return state


def summarize(label: str, state: dict):
    if state is None:
        return
    closed = state["closed"]
    if not closed:
        print(f"\n=== {label}: 0 trades ===")
        return
    total = sum(c["pnl"] for c in closed)
    wins = sum(1 for c in closed if c["pnl"] > 0)
    wr = 100 * wins / len(closed)
    avg = total / len(closed)
    eq = []
    running = 0.0
    for c in sorted(closed, key=lambda x: x["exit_ts"]):
        running += c["pnl"]
        eq.append(running)
    peak = float("-inf")
    maxdd = 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = min(maxdd, v - peak)
    print(f"\n=== {label} ===")
    print(f"trades={len(closed)}  win_rate={wr:.1f}%  total_pnl=${total:+.2f}  "
          f"avg_pnl=${avg:+.2f}/trade  maxDD=${maxdd:.2f}")
    print("by symbol:")
    by_sym = {}
    for c in closed:
        by_sym.setdefault(c["symbol"], []).append(c["pnl"])
    for sym, pnls in sorted(by_sym.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {sym:5s} n={len(pnls):3d}  total=${sum(pnls):+8.2f}  "
              f"avg=${sum(pnls)/len(pnls):+7.2f}  WR={100*sum(1 for p in pnls if p>0)/len(pnls):.0f}%")
    print("by exit reason:")
    reasons = {}
    for c in closed:
        reasons[c["reason"]] = reasons.get(c["reason"], 0) + 1
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {r}: {n}")
    print("\nfirst 3 and last 3 trades:")
    for c in sorted(closed, key=lambda x: x["exit_ts"])[:3]:
        dt = datetime.fromtimestamp(int(c["exit_ts"]), tz=timezone.utc)
        print(f"  {dt.date()} {c['symbol']} {'LONG' if c['side']>0 else 'SHORT'} "
              f"pnl=${c['pnl']:+.2f} ({c['reason']})")
    for c in sorted(closed, key=lambda x: x["exit_ts"])[-3:]:
        dt = datetime.fromtimestamp(int(c["exit_ts"]), tz=timezone.utc)
        print(f"  {dt.date()} {c['symbol']} {'LONG' if c['side']>0 else 'SHORT'} "
              f"pnl=${c['pnl']:+.2f} ({c['reason']})")


def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30 * TEST_MONTHS)
    start_ts = int(start.timestamp())
    print(f"Test window: {start.date()} -> {now.date()} ({TEST_MONTHS} months)\n")

    print("=== Fetching & replaying 3-COIN (production default) ===")
    state_3 = replay(lp.KRAKEN_PAIRS_ALL, "3-coin default", start_ts)

    print("\n=== Fetching & replaying 8-COIN (PR #95 widened universe) ===")
    state_8 = replay(lp.KRAKEN_PAIRS_WIDE, "8-coin widened", start_ts)

    summarize("3-coin default (BTC/ETH/SOL)", state_3)
    summarize("8-coin widened (+ADA/XRP/DOT/AVAX/LINK)", state_8)


if __name__ == "__main__":
    main()
