"""
Timeframe edge — the honest lever for "trade AND win" (not "trade less").

Win rate on the 1m engine was ~1% because the typical bar MOVE (~0.07%) is far
smaller than the round-trip COST (~0.5%) — price can't reach a profitable target
before noise stops it. No exit tuning or win-rate trick fixes a move that is a
fraction of the toll.

The lever that actually works: trade a timeframe where the MOVE dwarfs the cost.
This fetches live Kraken OHLCV for BTC/ETH/SOL at several timeframes and prints
the median move (ATR%) and the move/cost ratio. Where ratio >> 1, a real edge
can clear the toll and a *profitable* (not just high) win rate is possible.

Read-only. Public Kraken data (no keys). Falls back gracefully if offline.
"""
from __future__ import annotations
import statistics as st

COST_RT = 0.005   # ~0.5% round-trip (Kraken spot taker, both legs)
SYMBOLS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
TFS = ['1m', '15m', '1h', '4h', '1d']
LIMIT = 200


def atr_pct(ohlcv):
    """Median True Range as % of close over the window (robust to outliers)."""
    trs = []
    prev_close = None
    for _ts, o, h, l, c, _v in ohlcv:
        if prev_close is None:
            tr = (h - l)
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        if c:
            trs.append(tr / c * 100.0)
        prev_close = c
    return st.median(trs) if trs else 0.0


def main():
    try:
        import ccxt
    except Exception as e:
        print("ccxt not available:", e)
        return
    ex = ccxt.kraken({'enableRateLimit': True})

    print("=" * 64)
    print("TIMEFRAME EDGE — median move vs ~0.5% round-trip cost")
    print("=" * 64)
    print(f"{'timeframe':<10}{'median move%':>14}{'move / cost':>14}   verdict")
    print("-" * 64)

    for tf in TFS:
        moves = []
        for sym in SYMBOLS:
            try:
                o = ex.fetch_ohlcv(sym, timeframe=tf, limit=LIMIT)
                if o:
                    moves.append(atr_pct(o))
            except Exception as e:
                print(f"  [{sym} {tf}] fetch failed: {e}")
        if not moves:
            continue
        mv = st.median(moves)
        ratio = mv / (COST_RT * 100)
        if ratio < 1:
            verdict = "UNWINNABLE — move < cost"
        elif ratio < 3:
            verdict = "marginal — needs real edge"
        else:
            verdict = "winnable — move dwarfs cost"
        print(f"{tf:<10}{mv:>13.3f}%{ratio:>13.1f}x   {verdict}")

    print("-" * 64)
    print("READ: the 1m engine sits at ratio<1 — no win-rate trick beats a move")
    print("smaller than the toll. At 4h/1d the move is many times the cost, so a")
    print("real trend edge can WIN while trading. That's why the live candidates")
    print("(swing=4h, tsmom=daily) live there — they're idle on REGIME (no trend")
    print("right now), not because they refuse to trade. Optimize EXPECTANCY there")
    print("(let winners run, cut losers) — not win rate on the dead 1m engine.")


if __name__ == '__main__':
    main()
