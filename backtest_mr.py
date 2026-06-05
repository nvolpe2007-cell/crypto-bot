"""
Backtest MeanReversionStrategy (Bollinger Band + RSI) standalone.

This is the alternative to ScientificStrategy. It's:
  - Simple (BB + RSI), no OFI/lead-lag — fully testable on candles.
  - A different hypothesis: "buy oversold dips in ranges, sell overbought rips."
  - Uses the same Tier-1 filters validated in backtest_scientific.py.

Run: python backtest_mr.py [--days N] [--no-filters] [--fee-pct 0.0016]
"""
import argparse
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reuse data fetch + filter from the scientific backtester
from backtest_scientific import (
    fetch_1m, filter_entry, Position, ClosedTrade,
)

from src.indicators import Signal
from src.mean_reversion_strategy import MeanReversionStrategy, MRSignal


def _to_signal(mr: Optional[MRSignal]):
    """Adapter: MRSignal exposes is_buy/is_sell/stop_loss_pct/take_profit_pct;
    that's all run_backtest needs from a signal object — except it also reads
    .signal, .confidence, .size_mult, .regime which we synthesize."""
    if mr is None:
        return None

    class _Adapted:
        signal           = mr.signal
        confidence       = 100.0   # MR fires only at extremes — no confidence tier
        size_mult        = 1.0
        is_buy           = mr.is_buy
        is_sell          = mr.is_sell
        regime           = 'MR'
        rsi              = mr.rsi
        adx              = mr.adx
        atr              = mr.atr
        close            = mr.close
        ofi              = None
        lead_lag_dir     = None
        ofi_score        = 0.0
        lead_lag_score   = 0.0
        regime_score     = 0.0
        funding_rate     = None
        volume_ratio     = 1.0
        ema_fast         = mr.close
        ema_slow         = mr.close
        @staticmethod
        def stop_loss_pct():   return mr.stop_loss_pct()
        @staticmethod
        def take_profit_pct(): return mr.take_profit_pct()
    return _Adapted


def run_mr_backtest(data: Dict[str, pd.DataFrame],
                    capital: float = 1000.0,
                    fee_pct: float = 0.0016,
                    slippage_pct: float = 0.0002,
                    base_equity_pct: float = 0.06,
                    use_filters: bool = True) -> dict:
    strategy = MeanReversionStrategy()

    common = None
    for s, df in data.items():
        common = df.index if common is None else common.intersection(df.index)
    common = common.sort_values()
    print(f"  common bars across {len(data)} symbols: {len(common):,}", flush=True)

    cash = capital
    positions: Dict[str, Position] = {}
    trades: List[ClosedTrade] = []
    equity_curve: List[tuple] = []
    skipped_hold = 0
    skipped_low_conf = 0
    filter_rejects: Dict[str, int] = {}

    warmup = 60   # MR needs ~50 bars for BB+RSI
    print(f"  running MR backtest from bar {warmup} to {len(common):,}...", flush=True)
    last_progress = 0

    for i in range(warmup, len(common)):
        ts = common[i]

        for sym, df in data.items():
            end_pos = df.index.get_loc(ts)
            if end_pos < warmup:
                continue
            window = df.iloc[max(0, end_pos - 299):end_pos + 1]
            price  = float(window['close'].iloc[-1])

            mr  = strategy.get_latest_signal(window)
            sig = _to_signal(mr)

            # Update MFE/MAE on open position
            if sym in positions:
                p = positions[sym]
                if p.side == 'buy':
                    p.mfe_price = max(p.mfe_price, price) if p.mfe_price else price
                    p.mae_price = min(p.mae_price, price) if p.mae_price else price
                else:
                    p.mfe_price = min(p.mfe_price, price) if p.mfe_price else price
                    p.mae_price = max(p.mae_price, price) if p.mae_price else price

            # Exit
            if sym in positions:
                pos = positions[sym]
                if pos.side == 'buy':
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                else:
                    pnl_pct = (pos.entry_price - price) / pos.entry_price * 100

                exit_reason = None
                if pnl_pct <= -pos.sl_pct:
                    exit_reason = 'STOP_LOSS'
                elif pnl_pct >= pos.tp_pct:
                    exit_reason = 'TAKE_PROFIT'
                # Mean-reversion exit: price returns to middle band, or RSI normalizes
                elif pos.side == 'buy'   and strategy.should_exit_long(window):
                    exit_reason = 'MID_BAND'
                elif pos.side == 'short' and strategy.should_exit_short(window):
                    exit_reason = 'MID_BAND'

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
                    cash += cost_basis + pnl

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

            # Entry
            if sym not in positions and sig is not None and sig.signal in (Signal.BUY, Signal.SELL):
                if use_filters:
                    reject = filter_entry(sym, ts, window, sig)
                    if reject is not None:
                        filter_rejects[reject] = filter_rejects.get(reject, 0) + 1
                        continue

                equity = cash + sum(p.entry_price * p.size for p in positions.values())
                size_usd = equity * base_equity_pct
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

        if i % 60 == 0:
            mark = cash
            for sym, p in positions.items():
                price = float(data[sym].at[ts, 'close'])
                mark += (price * p.size) if p.side == 'buy' else (2 * p.entry_price - price) * p.size
            equity_curve.append((ts, mark))

        pct = int((i - warmup) / (len(common) - warmup) * 100)
        if pct >= last_progress + 10:
            print(f"    ...{pct}%  trades={len(trades)}  open={len(positions)}  cash=${cash:,.0f}", flush=True)
            last_progress = pct

    return {
        'capital': capital, 'final_equity': cash,
        'trades': trades, 'equity_curve': equity_curve,
        'skipped_low_conf': skipped_low_conf, 'skipped_hold': skipped_hold,
        'filter_rejects': filter_rejects,
    }


def report(result: dict, data: Dict[str, pd.DataFrame]):
    trades = result['trades']
    cap, eq = result['capital'], result['final_equity']
    print("\n" + "=" * 70)
    print("BACKTEST RESULT - MeanReversionStrategy (BB + RSI)")
    print("=" * 70)
    print(f"  capital       ${cap:,.2f}")
    print(f"  final equity  ${eq:,.2f}")
    print(f"  total return  {(eq - cap)/cap*100:+.2f}%")
    print(f"  trades        {len(trades)}")
    rejs = result.get('filter_rejects') or {}
    if rejs:
        print(f"  filter rejects: " + "  ".join(f"{k}={v:,}" for k, v in sorted(rejs.items(), key=lambda kv: -kv[1])))

    if not trades:
        print("\n  ZERO trades.")
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

    eqs = pd.Series([e for _, e in result['equity_curve']])
    if len(eqs) > 1:
        running_max = eqs.expanding().max()
        dd = (eqs - running_max) / running_max * 100
        print(f"  max drawdown  {dd.min():.2f}%")
        rets = eqs.pct_change().dropna()
        sharpe = (rets.mean() / rets.std()) * np.sqrt(365 * 24) if rets.std() > 0 else 0
        print(f"  sharpe (1h)   {sharpe:.2f}")

    print("\n  Buy-and-hold baseline (equal weight):")
    bh_total = 0.0
    for sym, df in data.items():
        first, last = float(df['close'].iloc[0]), float(df['close'].iloc[-1])
        ret = (last - first) / first * 100
        bh_total += ret
        print(f"    {sym:<10} {ret:+.2f}%")
    print(f"    avg       {bh_total / len(data):+.2f}%")

    by_reason: Dict[str, list] = {}
    for t in trades:
        by_reason.setdefault(t.reason, []).append(t)
    print("\n  Exit reason breakdown:")
    for reason, ts in by_reason.items():
        wr = sum(1 for t in ts if t.pnl > 0) / len(ts) * 100
        avg = np.mean([t.pnl for t in ts])
        print(f"    {reason:<14} n={len(ts):>4}  wr={wr:5.1f}%  avg=${avg:+.2f}")

    out = pd.DataFrame([t.__dict__ for t in trades])
    out_path = os.path.join('data', 'backtest_mr_trades.csv')
    os.makedirs('data', exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"\n  wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--no-filters', action='store_true')
    ap.add_argument('--fee-pct', type=float, default=0.0016)
    ap.add_argument('--slippage-pct', type=float, default=0.0002)
    args = ap.parse_args()

    symbols = ['BTC/USD', 'ETH/USD', 'SOL/USD']
    data: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        data[s] = fetch_1m(s, args.days)

    result = run_mr_backtest(data,
                             fee_pct=args.fee_pct,
                             slippage_pct=args.slippage_pct,
                             use_filters=not args.no_filters)
    report(result, data)


if __name__ == '__main__':
    main()
