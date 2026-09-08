"""
The pre-registered sanity check from Notes/Hypotheses/stablecoin-basis-stress-signal.md:
"does the detector fire on a known, agreed-real stress episode?"

Data: Coinbase Exchange public candles (no key needed, no 365-day wall like CoinGecko's
free tier). Implied USDT/USD basis is derived from BTC-USDT vs BTC-USD price, the standard
technique when a direct USDT-USD spot pair isn't listed:

    implied_usdt_per_usd = price(BTC-USD) / price(BTC-USDT)

If USDT is trading at a discount to the dollar (stress), you need MORE USDT to buy the same
BTC that USD buys, so price(BTC-USDT) > price(BTC-USD), and the ratio drops below 1.0.

Three windows, chosen because they are the three most agreed-upon crypto-liquidity-stress
events in this period, none of them cherry-picked for this result (they were the ones
selected in this note before this script ran):
    - Terra/UST collapse, ~2022-05-09 to 2022-05-12
    - FTX collapse, ~2022-11-08 to 2022-11-11
    - SVB / USDC depeg, ~2023-03-10 to 2023-03-13

Run: python scripts/stablecoin_basis_crisis_check.py
"""
import json
import statistics
from pathlib import Path
from datetime import datetime, timezone

PATH = Path(__file__).resolve().parent.parent / "scratch_stablecoin" / "crisis_windows.json"


def to_series(rows):
    # rows: [time, low, high, open, close, volume], ascending
    return [(r[0], r[4]) for r in rows]  # (unix_ts, close)


def align(usdt_rows, usd_rows):
    usdt = dict(to_series(usdt_rows))
    usd = dict(to_series(usd_rows))
    common = sorted(set(usdt.keys()) & set(usd.keys()))
    return [(t, usd[t] / usdt[t]) for t in common]


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    data = json.loads(PATH.read_text())

    for name, windows in data.items():
        basis = align(windows["usdt"], windows["usd"])
        vals = [v for _, v in basis]
        dev_bps = [(v - 1.0) * 10000 for v in vals]

        print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
        print(f"n points: {len(basis)}")
        print(f"implied USDT/USD basis, deviation from 1.0000 in bps:")
        print(f"  mean={statistics.fmean(dev_bps):+8.2f}bps  "
              f"stdev={statistics.pstdev(dev_bps):7.2f}bps  "
              f"min={min(dev_bps):+8.2f}bps  max={max(dev_bps):+8.2f}bps")

        min_idx = dev_bps.index(min(dev_bps))
        max_idx = dev_bps.index(max(dev_bps))
        print(f"  most-negative (USDT cheap / discount) at {fmt(basis[min_idx][0])}: "
              f"{dev_bps[min_idx]:+.2f}bps")
        print(f"  most-positive (USDT rich / premium)   at {fmt(basis[max_idx][0])}: "
              f"{dev_bps[max_idx]:+.2f}bps")

        # crude "did it move materially" check: compare max abs deviation here to the
        # calm-regime stdev (~3.5bps) measured in stablecoin_basis_research.py
        calm_stdev_bps = 3.5
        peak = max(abs(min(dev_bps)), abs(max(dev_bps)))
        multiple = peak / calm_stdev_bps
        print(f"  peak deviation is {multiple:.1f}x the calm-regime stdev (~{calm_stdev_bps}bps)")
        if multiple >= 3:
            print(f"  -> DETECTOR WOULD FIRE at a 3-sigma threshold on this window.")
        else:
            print(f"  -> detector would NOT clearly fire at a 3-sigma threshold "
                  f"(peak only {multiple:.1f}x calm stdev).")


if __name__ == "__main__":
    main()
