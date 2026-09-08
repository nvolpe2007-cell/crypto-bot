"""
Research script for the stablecoin-basis-stress-signal hypothesis.
See: vault note Notes/Hypotheses/stablecoin-basis-stress-signal.md

Data source: CoinGecko free market_chart/range endpoint (no key). Two windows:
  - hourly, ~89 days (free-tier hourly cap)
  - daily, ~364 days (free-tier no-key cap)
This is a real limitation, recorded honestly: it does NOT reach back to any known
stablecoin stress event (e.g. the March 2023 USDC/SVB depeg), so the pre-registered
sanity check ("does the detector fire on a known-real episode") cannot be run yet.
Everything below is measured on a calm-regime window and must be read as such.

Run: python scripts/stablecoin_basis_research.py
"""
import json
import math
import statistics
from pathlib import Path

RAW_PATH = Path(__file__).resolve().parent.parent / "scratch_stablecoin" / "raw.json"


def load_series(prices):
    # prices: list of [ms_timestamp, price]
    return [(p[0] / 1000.0, p[1]) for p in prices]


def align(*series_list):
    """Inner-join multiple (ts, val) series on nearest matching timestamp bucket."""
    # CoinGecko returns roughly-aligned timestamps for same-resolution calls, but not
    # guaranteed identical. Bucket to nearest 5 minutes and align on that key.
    def bucket(ts):
        return round(ts / 300) * 300

    maps = []
    for s in series_list:
        m = {}
        for ts, v in s:
            m[bucket(ts)] = v
        maps.append(m)

    common_keys = set(maps[0].keys())
    for m in maps[1:]:
        common_keys &= set(m.keys())
    common_keys = sorted(common_keys)

    aligned = [[m[k] for m in maps] for k in common_keys]
    return common_keys, aligned


def rolling_zscore(values, window):
    z = [None] * len(values)
    for i in range(len(values)):
        if i < window:
            continue
        w = values[i - window:i]
        mean = statistics.fmean(w)
        sd = statistics.pstdev(w)
        z[i] = 0.0 if sd == 0 else (values[i] - mean) / sd
    return z


def detect_episodes(z, threshold, confirm_bars, resolve_threshold):
    """
    onset: |z| >= threshold sustained for confirm_bars consecutive points, not
    already inside an active episode.
    resolution: the first point after a confirmed onset where |z| < resolve_threshold.
    Returns list of dicts: {onset_idx, confirm_idx, resolve_idx (or None), direction}
    """
    episodes = []
    in_episode = False
    streak = 0
    streak_dir = 0
    onset_start = None

    i = 0
    n = len(z)
    while i < n:
        zi = z[i]
        if zi is None:
            i += 1
            continue

        if not in_episode:
            direction = 1 if zi >= threshold else (-1 if zi <= -threshold else 0)
            if direction != 0:
                if streak_dir == direction:
                    streak += 1
                else:
                    streak_dir = direction
                    streak = 1
                    onset_start = i
                if streak >= confirm_bars:
                    confirm_idx = i
                    # find resolution
                    resolve_idx = None
                    for j in range(i + 1, n):
                        if z[j] is None:
                            continue
                        if abs(z[j]) < resolve_threshold:
                            resolve_idx = j
                            break
                    episodes.append({
                        "onset_idx": onset_start,
                        "confirm_idx": confirm_idx,
                        "resolve_idx": resolve_idx,
                        "direction": streak_dir,
                    })
                    in_episode = True
                    streak = 0
                    streak_dir = 0
            else:
                streak = 0
                streak_dir = 0
        else:
            # stay in episode until resolved (skip forward to resolve_idx if known)
            ep = episodes[-1]
            if ep["resolve_idx"] is not None and i >= ep["resolve_idx"]:
                in_episode = False
        i += 1

    return episodes


def forward_return(btc, idx, horizon_bars):
    if idx + horizon_bars >= len(btc):
        return None
    p0 = btc[idx]
    p1 = btc[idx + horizon_bars]
    if p0 == 0:
        return None
    return (p1 - p0) / p0


def unconditional_baseline(btc, horizon_bars, exclude_idxs):
    rets = []
    excl = set()
    for e in exclude_idxs:
        for d in range(-horizon_bars, horizon_bars + 1):
            excl.add(e + d)
    for i in range(len(btc) - horizon_bars):
        if i in excl:
            continue
        r = forward_return(btc, i, horizon_bars)
        if r is not None:
            rets.append(r)
    return rets


def t_stat(sample, pop_mean):
    n = len(sample)
    if n < 2:
        return None, None
    mean = statistics.fmean(sample)
    sd = statistics.pstdev(sample)
    if sd == 0:
        return None, None
    se = sd / math.sqrt(n)
    t = (mean - pop_mean) / se
    return t, mean


def cross_correlation(a, b, max_lag):
    """Pearson correlation of a[t] vs b[t+lag] for lag in [-max_lag, max_lag]."""
    n = len(a)
    out = {}
    for lag in range(-max_lag, max_lag + 1):
        xs, ys = [], []
        for i in range(n):
            j = i + lag
            if 0 <= j < n:
                xs.append(a[i])
                ys.append(b[j])
        if len(xs) < 10:
            continue
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sx = statistics.pstdev(xs)
        sy = statistics.pstdev(ys)
        if sx == 0 or sy == 0:
            continue
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
        out[lag] = cov / (sx * sy)
    return out


def analyze_window(label, usdc_raw, usdt_raw, btc_raw, zwindow, thresholds, confirm_bars_opts,
                    horizons_bars):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    keys, aligned = align(load_series(usdc_raw), load_series(usdt_raw), load_series(btc_raw))
    usdc = [row[0] for row in aligned]
    usdt = [row[1] for row in aligned]
    btc = [row[2] for row in aligned]
    n = len(usdc)
    print(f"aligned points: {n}  (span {(keys[-1]-keys[0])/86400:.1f} days)")

    usdc_dev = [v - 1.0 for v in usdc]
    usdt_dev = [v - 1.0 for v in usdt]
    cross_basis = [(u / t - 1.0) for u, t in zip(usdc, usdt)]

    print("\n-- descriptive stats (raw deviation from $1.00, in bps) --")
    for name, series in [("USDC/USD", usdc_dev), ("USDT/USD", usdt_dev), ("USDC/USDT cross", cross_basis)]:
        bps = [v * 10000 for v in series]
        print(f"{name:18s}  mean={statistics.fmean(bps):+7.3f}bps  "
              f"stdev={statistics.pstdev(bps):7.3f}bps  "
              f"min={min(bps):+7.3f}bps  max={max(bps):+7.3f}bps")

    if n < zwindow + 20:
        print(f"\n[skip episode detection: need > {zwindow + 20} points, have {n}]")
        z = None
    else:
        z = rolling_zscore(cross_basis, zwindow)

    print("\n-- lead-lag cross-correlation: cross_basis[t] vs BTC log-return[t+lag] --")
    btc_ret = [0.0] + [(btc[i] - btc[i - 1]) / btc[i - 1] for i in range(1, len(btc))]
    max_lag = min(12, n // 10)
    if max_lag >= 2:
        xc = cross_correlation(cross_basis, btc_ret, max_lag)
        for lag in sorted(xc):
            tag = "  <-- contemporaneous" if lag == 0 else ("  <-- basis LEADS btc" if lag < 0 else "")
            print(f"  lag {lag:+3d}: corr = {xc[lag]:+.4f}{tag}")
        print("  (negative lag = basis move happens BEFORE the btc return it's compared to,")
        print("   i.e. a real lead. lag 0 = same bar. positive lag = basis reacts AFTER btc.)")
    else:
        print("  [skip: not enough points for a meaningful lag window]")

    if z is None:
        return

    print("\n-- episode detection across threshold/persistence grid --")
    print(f"{'thresh':>7} {'confirm':>7} {'n_onset':>8} {'n_resolved':>10}")
    best = None
    for thresh in thresholds:
        for cb in confirm_bars_opts:
            eps = detect_episodes(z, thresh, cb, resolve_threshold=thresh * 0.5)
            n_resolved = sum(1 for e in eps if e["resolve_idx"] is not None)
            print(f"{thresh:7.2f} {cb:7d} {len(eps):8d} {n_resolved:10d}")
            if best is None or len(eps) > best[0]:
                best = (len(eps), thresh, cb, eps)

    if best is None or best[0] == 0:
        print("\n[NO episodes detected at any tested threshold in this window.")
        print(" Honest reading: either the calm 2025-2026 window genuinely has no stress")
        print(" episode of the tested magnitude, or the CoinGecko aggregate-price series")
        print(" is smoothed enough across exchanges to mute single-venue depeg spikes.")
        print(" This is a valid, recordable result, not a bug -- do not lower the")
        print(" threshold just to manufacture episodes.]")
        return

    _, thresh, cb, eps = best
    print(f"\n-- forward-return test at the threshold/persistence with the most episodes "
          f"(thresh={thresh}, confirm_bars={cb}, n={len(eps)}) --")
    print("[UNDERPOWERED if n < 30 -- reported for information, NOT a readout against the "
          "pre-registered gate.]")

    onset_idxs = [e["confirm_idx"] for e in eps]
    for horizon in horizons_bars:
        onset_rets = [r for r in (forward_return(btc, i, horizon) for i in onset_idxs) if r is not None]
        if not onset_rets:
            continue
        baseline = unconditional_baseline(btc, horizon, onset_idxs)
        t, mean_sample = t_stat(onset_rets, statistics.fmean(baseline))
        print(f"  horizon {horizon:3d} bars: n={len(onset_rets):3d}  "
              f"mean fwd ret={statistics.fmean(onset_rets)*100:+.3f}%  "
              f"baseline mean={statistics.fmean(baseline)*100:+.3f}%  "
              f"t={'n/a' if t is None else f'{t:+.2f}'}")

    resolved = [e for e in eps if e["resolve_idx"] is not None]
    if resolved:
        resolve_idxs = [e["resolve_idx"] for e in resolved]
        print(f"\n  resolution arm (n={len(resolved)}):")
        for horizon in horizons_bars:
            res_rets = [r for r in (forward_return(btc, i, horizon) for i in resolve_idxs) if r is not None]
            if not res_rets:
                continue
            baseline = unconditional_baseline(btc, horizon, resolve_idxs)
            t, mean_sample = t_stat(res_rets, statistics.fmean(baseline))
            print(f"  horizon {horizon:3d} bars: n={len(res_rets):3d}  "
                  f"mean fwd ret={statistics.fmean(res_rets)*100:+.3f}%  "
                  f"baseline mean={statistics.fmean(baseline)*100:+.3f}%  "
                  f"t={'n/a' if t is None else f'{t:+.2f}'}")
    else:
        print("\n  resolution arm: 0 episodes resolved within the available window")


def main():
    if not RAW_PATH.exists():
        raise SystemExit(f"missing {RAW_PATH} -- fetch data first")
    raw = json.loads(RAW_PATH.read_text())

    analyze_window(
        "HOURLY WINDOW (~89 days, fine resolution)",
        raw["hourly"]["usdc"], raw["hourly"]["usdt"], raw["hourly"]["btc"],
        zwindow=24 * 7,          # 7-day rolling calibration window
        thresholds=[1.5, 2.0, 2.5, 3.0],
        confirm_bars_opts=[1, 2, 3],
        horizons_bars=[4, 12, 24, 48],
    )

    analyze_window(
        "DAILY WINDOW (~364 days, coarse resolution)",
        raw["daily"]["usdc"], raw["daily"]["usdt"], raw["daily"]["btc"],
        zwindow=30,              # 30-day rolling calibration window
        thresholds=[1.5, 2.0, 2.5, 3.0],
        confirm_bars_opts=[1, 2],
        horizons_bars=[1, 3, 7],
    )


if __name__ == "__main__":
    main()
