"""
Backtest the live ScientificStrategy on real 1m candles, with OFI=None
(degraded mode — order book history is unavailable). LeadLagDetector and
RegimeDetector are real and fed real price ticks.

Purpose: answer the question "if we strip out OFI, does the strategy have
any edge?" If yes, OFI is a bonus on top. If no, the live strategy depends
entirely on a piece we cannot validate without paying for L2 data.

Run: python backtest_scientific.py [--days N] [--symbol BTC/USD]
"""
import argparse
import os
import sys
import time as _real_time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Patch lead_lag_detector's time.time so simulated bar timestamps drive its
# decay logic instead of wall-clock. We hold the simulated time in a list
# (mutable closure) so we can advance it bar-by-bar.
_sim_time = [_real_time.time()]
import src.lead_lag_detector as _lldm
_lldm.time.time = lambda: _sim_time[0]

from src.scientific_strategy import ScientificStrategy, Signal  # noqa: E402
from src.regime_detector import RegimeDetector                  # noqa: E402
from src.lead_lag_detector import LeadLagDetector               # noqa: E402
from src.entry_checklist import (                                # noqa: E402
    Checklist, Check, CheckContext,
    _rsi_healthy, _adx_strong, _volume_strong,
    _lead_lag_aligned, _funding_favorable, _atr_alive,
)


def _soft_only_checklist(soft_threshold: float = 0.6) -> Checklist:
    return Checklist([
        Check("atr_alive",         "hard", _atr_alive),
        Check("rsi_healthy",       "soft", _rsi_healthy,       weight=2.0),
        Check("adx_strong",        "soft", _adx_strong,        weight=2.0),
        Check("volume_strong",     "soft", _volume_strong,     weight=1.0),
        Check("lead_lag_aligned",  "soft", _lead_lag_aligned,  weight=2.0),
        Check("funding_favorable", "soft", _funding_favorable, weight=1.0),
    ], soft_threshold=soft_threshold)


# ── Data fetch ────────────────────────────────────────────────────────────────
# Async ccxt is broken on Python 3.14 (aiohttp incompat). Use sync ccxt.
TIMEFRAME_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000}


def _fetch_from_sync(exchange_name: str, symbol: str, days: int,
                     timeframe: str = '1m') -> pd.DataFrame:
    import ccxt
    ex_cls = getattr(ccxt, exchange_name)
    ex = ex_cls({'enableRateLimit': True})
    tf_ms = TIMEFRAME_MS.get(timeframe, 60_000)
    since = ex.milliseconds() - days * 24 * 60 * 60 * 1000
    limit = 1000 if exchange_name == 'binance' else 300
    rows: list = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        since = last_ts + tf_ms
        if len(batch) < limit or last_ts >= ex.milliseconds() - tf_ms:
            break
    if not rows:
        return pd.DataFrame(columns=['open','high','low','close','volume'])
    df = pd.DataFrame(rows, columns=['t', 'open', 'high', 'low', 'close', 'volume'])
    df['t'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df = df.drop_duplicates('t').set_index('t').sort_index()
    return df


def fetch_1m(symbol: str, days: int) -> pd.DataFrame:
    """Back-compat shim — calls fetch_ohlcv(symbol, '1m', days)."""
    return fetch_ohlcv(symbol, '1m', days)


SYMBOL_MAP = {
    'binanceus': {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'binance':   {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'huobi':     {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'kucoin':    {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'mexc':      {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'bybit':     {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
    'okx':       {'BTC/USD': 'BTC/USDT', 'ETH/USD': 'ETH/USDT', 'SOL/USD': 'SOL/USDT'},
}
# binanceus + huobi + kucoin reliably page 1000-1m-bars back ~30 days from US.
EXCHANGE_FALLBACK = ['binanceus', 'huobi', 'kucoin', 'kraken', 'coinbase', 'bybit', 'okx', 'mexc']


def fetch_ohlcv(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Sync OHLCV fetch with multi-exchange fallback."""
    last_err = None
    for ex_name in EXCHANGE_FALLBACK:
        ex_symbol = SYMBOL_MAP.get(ex_name, {}).get(symbol, symbol)
        try:
            print(f"  fetching {ex_symbol} {timeframe} x {days}d from {ex_name}...", flush=True)
            df = _fetch_from_sync(ex_name, ex_symbol, days, timeframe=timeframe)
            if len(df) < 100:
                print(f"    {ex_name} returned {len(df)} bars - too few, trying next", flush=True)
                continue
            print(f"    got {len(df):,} bars [{df.index[0]} -> {df.index[-1]}] from {ex_name}", flush=True)
            return df
        except Exception as e:
            print(f"    {ex_name} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
            last_err = e
    raise RuntimeError(f"all exchanges failed for {symbol}: {last_err}")


# ── Backtest engine ──────────────────────────────────────────────────────────
@dataclass
class Position:
    side: str            # 'buy' or 'short'
    entry_price: float
    size: float
    entry_time: pd.Timestamp
    sl_pct: float
    tp_pct: float
    entry_conf: float
    entry_regime: str
    mfe_price: float = 0.0
    mae_price: float = 0.0


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    fees: float
    reason: str
    holding_min: float
    entry_conf: float
    entry_regime: str
    mfe_pct: float
    mae_pct: float


# ── Entry filters ────────────────────────────────────────────────────────────
# These are the "Tier 1" filters: HTF trend, volume, liquid hours, weekday.
# Each returns the rejection reason (str) or None if the entry passes.
def filter_entry(sym: str, ts, window: pd.DataFrame, sig,
                 enable_hours: bool = True,
                 enable_weekday: bool = True,
                 enable_volume: bool = True,
                 enable_htf_trend: bool = True) -> Optional[str]:
    # Liquid hours: 12:00–21:00 UTC = US/EU overlap, where most real flow happens.
    if enable_hours and not (12 <= ts.hour < 21):
        return 'hours'
    # Skip weekends — chop and stop-hunts dominate Sat/Sun.
    if enable_weekday and ts.dayofweek >= 5:
        return 'weekend'
    # Skip dead-volume bars — wide spreads, bad fills, fake signals.
    if enable_volume:
        vol = float(window['volume'].iloc[-1])
        vol_sma = float(window['volume'].iloc[-20:].mean())
        if vol_sma > 0 and vol < 0.5 * vol_sma:
            return 'low_volume'
    # HTF trend filter: only trade with the dominant trend on a longer window.
    # On 1m bars, EMA50 vs EMA200 spans ~50 vs ~200 minutes — that IS a higher timeframe view.
    if enable_htf_trend and len(window) >= 200:
        try:
            import pandas_ta as ta_local
            ema50  = ta_local.ema(window['close'], length=50)
            ema200 = ta_local.ema(window['close'], length=200)
            ema50_v  = float(ema50.iloc[-1])  if ema50  is not None else None
            ema200_v = float(ema200.iloc[-1]) if ema200 is not None else None
            if ema50_v is not None and ema200_v is not None:
                if sig.signal == Signal.BUY and ema50_v < ema200_v:
                    return 'counter_trend_long'
                if sig.signal == Signal.SELL and ema50_v > ema200_v:
                    return 'counter_trend_short'
        except Exception:
            pass
    return None


def run_backtest(data: Dict[str, pd.DataFrame],
                 capital: float = 1000.0,
                 fee_pct: float = 0.0016,           # maker fee (Kraken 0.16% / many MEXC pairs 0.0%)
                 slippage_pct: float = 0.0002,      # tighter slippage for limit-maker fills
                 base_equity_pct: float = 0.06,
                 min_confidence: float = 45.0,
                 use_filters: bool = True,
                 use_checklist: bool = False,
                 checklist_threshold: float = 0.6) -> dict:
    """
    Per-bar replay of ScientificStrategy with OFI=None.
    Allows one open position per symbol. Long + short.
    Tier-1 entry filters can be toggled with use_filters.
    """
    strategy = ScientificStrategy(min_confidence=min_confidence)
    regime_d = RegimeDetector()
    ll       = LeadLagDetector()
    checklist = _soft_only_checklist(checklist_threshold) if use_checklist else None
    checklist_rejects: Dict[str, int] = {}
    checklist_scores: List[float] = []

    # Common timestamp axis — only bars present in every symbol
    common = None
    for s, df in data.items():
        common = df.index if common is None else common.intersection(df.index)
    common = common.sort_values()
    print(f"  common bars across {len(data)} symbols: {len(common):,}")

    cash = capital
    positions: Dict[str, Position] = {}
    trades: List[ClosedTrade] = []
    equity_curve: List[tuple] = []   # (timestamp, equity)
    skipped_low_conf = 0
    skipped_hold = 0
    filter_rejects: Dict[str, int] = {}
    # Signal-flip debounce — count consecutive opposing signals per position
    opposing_streak: Dict[str, int] = {}
    SIGNAL_EXIT_STREAK = 2   # need N opposing signals in a row before exiting

    warmup = 220   # RegimeDetector requires 200+, plus a buffer
    if len(common) < warmup + 10:
        raise RuntimeError(f"not enough data: {len(common)} bars, need >{warmup}")

    print(f"  running backtest from bar {warmup} to {len(common):,}...")
    last_progress = 0

    for i in range(warmup, len(common)):
        ts = common[i]
        _sim_time[0] = ts.timestamp()

        # Update lead-lag with current closes (BTC drives the alts)
        for sym in data:
            ll.update_price(sym, float(data[sym].at[ts, 'close']))

        for sym, df in data.items():
            # Window slice — last 300 bars up to (and including) ts
            end_pos = df.index.get_loc(ts)
            if end_pos < warmup:
                continue
            window = df.iloc[max(0, end_pos - 299):end_pos + 1]
            price  = float(window['close'].iloc[-1])

            rr = regime_d.detect(window)
            regime_name = rr.regime if rr else 'UNKNOWN'
            regime_conf = rr.confidence if rr else 0.5

            sig = strategy.evaluate(window, sym,
                                    ofi_calc=None, lead_lag=ll,
                                    regime=regime_name, regime_conf=regime_conf,
                                    funding_rate=None)
            if sig is None:
                continue

            # ── Update MFE/MAE on open position ───────────────────────────────
            if sym in positions:
                pos = positions[sym]
                if pos.side == 'buy':
                    pos.mfe_price = max(pos.mfe_price, price) if pos.mfe_price else price
                    pos.mae_price = min(pos.mae_price, price) if pos.mae_price else price
                else:
                    pos.mfe_price = min(pos.mfe_price, price) if pos.mfe_price else price
                    pos.mae_price = max(pos.mae_price, price) if pos.mae_price else price

            # ── Exit ──────────────────────────────────────────────────────────
            if sym in positions:
                pos = positions[sym]
                if pos.side == 'buy':
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                else:
                    pnl_pct = (pos.entry_price - price) / pos.entry_price * 100

                exit_reason = None
                # Track consecutive opposing signals — single flip is too noisy
                # (baseline backtest: 25/33 SIGNAL exits, 4% WR — single-bar
                # counter-signals are mostly noise on 1m bars).
                is_opposing = (
                    (pos.side == 'buy'   and sig.signal == Signal.SELL) or
                    (pos.side == 'short' and sig.signal == Signal.BUY)
                )
                if is_opposing:
                    opposing_streak[sym] = opposing_streak.get(sym, 0) + 1
                else:
                    opposing_streak[sym] = 0

                if pnl_pct <= -pos.sl_pct:
                    exit_reason = 'STOP_LOSS'
                elif pnl_pct >= pos.tp_pct:
                    exit_reason = 'TAKE_PROFIT'
                elif is_opposing and opposing_streak[sym] >= SIGNAL_EXIT_STREAK:
                    exit_reason = 'SIGNAL'

                if exit_reason:
                    if pos.side == 'buy':
                        exit_price = price * (1 - slippage_pct)
                        gross      = (exit_price - pos.entry_price) * pos.size
                    else:
                        exit_price = price * (1 + slippage_pct)
                        gross      = (pos.entry_price - exit_price) * pos.size
                    fees = (pos.entry_price + exit_price) * pos.size * fee_pct
                    pnl  = gross - fees
                    cost_basis = pos.entry_price * pos.size
                    cash += cost_basis + pnl   # release capital + realize pnl

                    if pos.side == 'buy':
                        mfe_pct = ((pos.mfe_price - pos.entry_price) / pos.entry_price * 100) if pos.mfe_price else 0.0
                        mae_pct = ((pos.mae_price - pos.entry_price) / pos.entry_price * 100) if pos.mae_price else 0.0
                    else:
                        mfe_pct = ((pos.entry_price - pos.mfe_price) / pos.entry_price * 100) if pos.mfe_price else 0.0
                        mae_pct = ((pos.entry_price - pos.mae_price) / pos.entry_price * 100) if pos.mae_price else 0.0

                    trades.append(ClosedTrade(
                        symbol=sym, side=pos.side,
                        entry_time=pos.entry_time, exit_time=ts,
                        entry_price=pos.entry_price, exit_price=exit_price,
                        size=pos.size, pnl=pnl, pnl_pct=pnl/cost_basis*100, fees=fees,
                        reason=exit_reason,
                        holding_min=(ts - pos.entry_time).total_seconds() / 60,
                        entry_conf=pos.entry_conf, entry_regime=pos.entry_regime,
                        mfe_pct=mfe_pct, mae_pct=mae_pct,
                    ))
                    del positions[sym]
                    opposing_streak.pop(sym, None)

            # ── Entry ─────────────────────────────────────────────────────────
            if sym not in positions and sig.signal in (Signal.BUY, Signal.SELL):
                if sig.confidence < min_confidence or sig.size_mult <= 0:
                    skipped_low_conf += 1
                    continue

                # Tier-1 filters (HTF trend, volume, liquid hours, weekday)
                if use_filters:
                    reject = filter_entry(sym, ts, window, sig)
                    if reject is not None:
                        filter_rejects[reject] = filter_rejects.get(reject, 0) + 1
                        continue

                # Soft-only checklist (rsi/adx/volume/lead-lag/funding)
                cl_score = 1.0
                if checklist is not None:
                    side = 'buy' if sig.signal == Signal.BUY else 'sell'
                    cl_ctx = CheckContext(
                        symbol=sym, side=side, sig=sig,
                        regime_name=sig.regime, min_confidence=min_confidence,
                        now_ts=ts.timestamp(), bar_ts=ts.timestamp(),
                        last_exit_reason='', last_exit_time=0,
                        last_entry_bar_ts=None,
                        cooldown_for=lambda r: 0,
                        last_ws_price_time=0, ws_staleness_sec=999,
                        open_positions_count=0, max_open_positions=999,
                        sentiment_allows=True, kill_filter_reason=None,
                        circuit_breaker_reason=None,
                    )
                    cl = checklist.run(cl_ctx)
                    checklist_scores.append(cl.score)
                    if not cl.passed:
                        for name in cl.failed_hard:
                            checklist_rejects[f"hard:{name}"] = checklist_rejects.get(f"hard:{name}", 0) + 1
                        for name in cl.soft_misses:
                            checklist_rejects[name] = checklist_rejects.get(name, 0) + 1
                        continue
                    cl_score = cl.score

                # Equity-fraction sizing, scaled by confidence tier
                equity = cash + sum(
                    p.entry_price * p.size for p in positions.values())
                size_usd = equity * base_equity_pct * sig.size_mult * cl_score
                if size_usd < 5.0 or size_usd > cash * 0.95:
                    skipped_hold += 1
                    continue

                if sig.signal == Signal.BUY:
                    entry_price = price * (1 + slippage_pct)
                    side = 'buy'
                else:
                    entry_price = price * (1 - slippage_pct)
                    side = 'short'

                size = size_usd / entry_price
                cash -= entry_price * size
                positions[sym] = Position(
                    side=side, entry_price=entry_price, size=size,
                    entry_time=ts,
                    sl_pct=sig.stop_loss_pct(),
                    tp_pct=sig.take_profit_pct(),
                    entry_conf=sig.confidence, entry_regime=sig.regime,
                )

        # Equity snapshot every 60 bars
        if i % 60 == 0:
            mark = cash
            for sym, p in positions.items():
                price = float(data[sym].at[ts, 'close'])
                if p.side == 'buy':
                    mark += price * p.size
                else:
                    mark += (2 * p.entry_price - price) * p.size
            equity_curve.append((ts, mark))

        # Progress log
        pct = int((i - warmup) / (len(common) - warmup) * 100)
        if pct >= last_progress + 10:
            print(f"    ...{pct}%  trades={len(trades)}  open={len(positions)}  cash=${cash:,.0f}")
            last_progress = pct

    # ── Close any still-open positions at last price ──────────────────────────
    for sym, pos in list(positions.items()):
        ts    = common[-1]
        price = float(data[sym].at[ts, 'close'])
        if pos.side == 'buy':
            exit_price = price * (1 - slippage_pct)
            gross      = (exit_price - pos.entry_price) * pos.size
        else:
            exit_price = price * (1 + slippage_pct)
            gross      = (pos.entry_price - exit_price) * pos.size
        fees = (pos.entry_price + exit_price) * pos.size * fee_pct
        pnl  = gross - fees
        cost_basis = pos.entry_price * pos.size
        cash += cost_basis + pnl
        trades.append(ClosedTrade(
            symbol=sym, side=pos.side,
            entry_time=pos.entry_time, exit_time=ts,
            entry_price=pos.entry_price, exit_price=exit_price,
            size=pos.size, pnl=pnl, pnl_pct=pnl/cost_basis*100, fees=fees,
            reason='EOD',
            holding_min=(ts - pos.entry_time).total_seconds() / 60,
            entry_conf=pos.entry_conf, entry_regime=pos.entry_regime,
            mfe_pct=0.0, mae_pct=0.0,
        ))

    final_equity = cash
    return {
        'capital':      capital,
        'final_equity': final_equity,
        'trades':       trades,
        'equity_curve': equity_curve,
        'skipped_low_conf': skipped_low_conf,
        'skipped_hold':     skipped_hold,
        'filter_rejects':   filter_rejects,
        'checklist_rejects': checklist_rejects,
        'checklist_scores':  checklist_scores,
    }


# ── Reporting ────────────────────────────────────────────────────────────────
def report(result: dict, data: Dict[str, pd.DataFrame], min_confidence: float):
    trades = result['trades']
    cap    = result['capital']
    eq     = result['final_equity']

    print("\n" + "=" * 70)
    print(f"BACKTEST RESULT — ScientificStrategy degraded (OFI=None)")
    print("=" * 70)
    print(f"  capital       ${cap:,.2f}")
    print(f"  final equity  ${eq:,.2f}")
    print(f"  total return  {(eq - cap) / cap * 100:+.2f}%")
    print(f"  trades        {len(trades)}")
    print(f"  skipped (low conf): {result['skipped_low_conf']:,}")
    rejs = result.get('filter_rejects') or {}
    if rejs:
        print(f"  filter rejects:  " + "  ".join(f"{k}={v:,}" for k, v in sorted(rejs.items(), key=lambda kv: -kv[1])))

    if not trades:
        print("\n  ZERO trades taken at min_confidence={:.0f}. Strategy is filtering everything.".format(min_confidence))
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    pf = sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)) if losses else float('inf')

    print(f"  win rate      {len(wins)/len(trades)*100:.1f}%  ({len(wins)}/{len(trades)})")
    print(f"  profit factor {pf:.2f}")
    print(f"  avg win       ${np.mean([t.pnl for t in wins]):+.2f}" if wins else "  avg win       n/a")
    print(f"  avg loss      ${np.mean([t.pnl for t in losses]):+.2f}" if losses else "  avg loss      n/a")
    print(f"  expectancy    ${np.mean([t.pnl for t in trades]):+.4f}/trade")
    print(f"  total fees    ${sum(t.fees for t in trades):,.2f}")
    print(f"  avg hold      {np.mean([t.holding_min for t in trades]):.1f} min")

    # Drawdown
    eqs = pd.Series([e for _, e in result['equity_curve']])
    if len(eqs) > 1:
        running_max = eqs.expanding().max()
        dd = (eqs - running_max) / running_max * 100
        print(f"  max drawdown  {dd.min():.2f}%")
        rets = eqs.pct_change().dropna()
        # equity_curve sampled every 60 bars (1h); annualize accordingly
        sharpe = (rets.mean() / rets.std()) * np.sqrt(365 * 24) if rets.std() > 0 else 0
        print(f"  sharpe (1h)   {sharpe:.2f}")

    # Buy-and-hold baseline (equal-weight)
    print("\n  Buy-and-hold baseline (equal weight):")
    bh_total = 0.0
    for sym, df in data.items():
        first = float(df['close'].iloc[0])
        last  = float(df['close'].iloc[-1])
        ret   = (last - first) / first * 100
        bh_total += ret
        print(f"    {sym:<10} {ret:+.2f}%")
    print(f"    avg       {bh_total / len(data):+.2f}%")

    # Breakdown by exit reason
    by_reason: Dict[str, list] = {}
    for t in trades:
        by_reason.setdefault(t.reason, []).append(t)
    print("\n  Exit reason breakdown:")
    for reason, ts in by_reason.items():
        wr = sum(1 for t in ts if t.pnl > 0) / len(ts) * 100
        avg = np.mean([t.pnl for t in ts])
        print(f"    {reason:<14} n={len(ts):>4}  wr={wr:5.1f}%  avg=${avg:+.2f}")

    # Save trades CSV for further analysis
    out = pd.DataFrame([t.__dict__ for t in trades])
    out_path = os.path.join('data', 'backtest_scientific_trades.csv')
    os.makedirs('data', exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"\n  wrote {out_path}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--timeframe', type=str, default='1m', choices=['1m','5m','15m','1h'])
    ap.add_argument('--exchange', type=str, default=None,
                    help='force a specific exchange (binance/kraken/coinbase/...)')
    ap.add_argument('--min-conf', type=float, default=45.0)
    ap.add_argument('--no-filters', action='store_true', help='disable Tier-1 entry filters')
    ap.add_argument('--checklist', action='store_true', help='apply entry_checklist soft predicates')
    ap.add_argument('--checklist-threshold', type=float, default=0.6)
    ap.add_argument('--fee-pct', type=float, default=0.0016, help='per-side fee fraction (default 0.0016 = maker)')
    ap.add_argument('--slippage-pct', type=float, default=0.0002, help='per-side slippage fraction')
    args = ap.parse_args()

    global EXCHANGE_FALLBACK
    if args.exchange:
        EXCHANGE_FALLBACK = [args.exchange] + [e for e in EXCHANGE_FALLBACK if e != args.exchange]
    symbols = ['BTC/USD', 'ETH/USD', 'SOL/USD']
    data: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        data[s] = fetch_ohlcv(s, args.timeframe, args.days)

    result = run_backtest(data,
                          min_confidence=args.min_conf,
                          fee_pct=args.fee_pct,
                          slippage_pct=args.slippage_pct,
                          use_filters=not args.no_filters,
                          use_checklist=args.checklist,
                          checklist_threshold=args.checklist_threshold)
    report(result, data, args.min_conf)
    rejs = result.get('checklist_rejects') or {}
    if rejs:
        print("\n  checklist soft-miss counts (per individual rule, across rejected setups):")
        for k, v in sorted(rejs.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<22} {v:,}")
    scs = result.get('checklist_scores') or []
    if scs:
        print(f"  checklist mean score: {np.mean(scs):.2f}  median: {np.median(scs):.2f}  n={len(scs)}")


if __name__ == '__main__':
    main()
