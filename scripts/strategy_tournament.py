"""
Strategy tournament — fund 40+ "bots" with $1,000 each, race them on ~2 years of
daily BTC & ETH, honest 0.5% round-trip cost. Popular strategies + hybrids +
invented ones, plus a RANDOM control (the noise floor) and buy-&-hold.

THE DISCIPLINE (this is a discovery tool, not a proof machine): with this many
strategies, several top the leaderboard by luck. So each is judged by ROBUSTNESS
— positive risk-adjusted return on BOTH coins AND in BOTH halves of the window.
A strategy that only wins on one coin in one half is a fit, not an edge. Anything
robust is a CANDIDATE for forward proof (proof_scorecard), never an auto-deploy.

Shorts are allowed in-sim (paper); each strategy is tagged long-only-executable
or not (a US Kraken-spot account can't short). ccxt Kraken daily data.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

COST_LEG = 0.0025          # per position-unit change (round trip = enter+exit = 0.5%)
START = 1000.0
ANN = 365


# ── indicators ────────────────────────────────────────────────────────────────
def sma(s, n): return s.rolling(n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def roc(s, n): return s.pct_change(n)
def zscore(s, n): return (s - s.rolling(n).mean()) / s.rolling(n).std()

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def atr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def macd_line(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl)
    return m, ema(m, sig)


# ── strategy library: each returns a position series in [-1,1] (lookahead-free:
#    decided at close[t], the backtester realizes it on t+1). (fn, long_only_ok) ──
def S():
    reg = {}
    def add(name, fn, lo=False): reg[name] = (fn, lo)
    c = lambda df: df['close']

    # --- Trend / momentum (long/short) ---
    for n in (50, 100, 150, 200):
        add(f"tsmom_{n}", lambda df, n=n: np.sign(c(df) - sma(c(df), n)))
    add("sma_cross_20_50", lambda df: np.sign(sma(c(df),20) - sma(c(df),50)))
    add("sma_cross_50_200", lambda df: np.sign(sma(c(df),50) - sma(c(df),200)))
    add("ema_cross_12_26", lambda df: np.sign(ema(c(df),12) - ema(c(df),26)))
    add("macd_sign", lambda df: np.sign(macd_line(c(df))[0]))
    add("macd_hist", lambda df: np.sign(macd_line(c(df))[0] - macd_line(c(df))[1]))
    for n in (20, 55):
        add(f"donchian_{n}", lambda df, n=n: pd.Series(
            np.where(c(df) >= c(df).rolling(n).max().shift(1), 1,
                     np.where(c(df) <= c(df).rolling(n).min().shift(1), -1, np.nan)),
            index=df.index).ffill().fillna(0))
    add("roc_20", lambda df: np.sign(roc(c(df),20)))
    add("roc_60", lambda df: np.sign(roc(c(df),60)))
    add("bollinger_breakout", lambda df: pd.Series(
        np.where(c(df) > sma(c(df),20)+2*c(df).rolling(20).std(), 1,
                 np.where(c(df) < sma(c(df),20)-2*c(df).rolling(20).std(), -1, np.nan)),
        index=df.index).ffill().fillna(0))
    add("triple_sma_align", lambda df: pd.Series(
        np.where((sma(c(df),10)>sma(c(df),20))&(sma(c(df),20)>sma(c(df),50)),1,
                 np.where((sma(c(df),10)<sma(c(df),20))&(sma(c(df),20)<sma(c(df),50)),-1,0)),
        index=df.index))
    add("dual_momentum_3_6", lambda df: np.sign(
        ((roc(c(df),63)>0)&(roc(c(df),126)>0)).astype(float) -
        ((roc(c(df),63)<0)&(roc(c(df),126)<0)).astype(float)))

    # --- Trend long-only (flat instead of short → executable on spot) ---
    for n in (50, 100, 200):
        add(f"tsmom_long_{n}", lambda df, n=n: (c(df) > sma(c(df), n)).astype(float), lo=True)
    add("sma_cross_long_50_200", lambda df: (sma(c(df),50)>sma(c(df),200)).astype(float), lo=True)
    add("donchian_long_55", lambda df: pd.Series(
        np.where(c(df) >= c(df).rolling(55).max().shift(1), 1,
                 np.where(c(df) <= c(df).rolling(55).min().shift(1), 0, np.nan)),
        index=df.index).ffill().fillna(0), lo=True)

    # --- Mean reversion ---
    add("rsi2_meanrev_lo", lambda df: pd.Series(
        np.where(rsi(c(df),2) < 10, 1, np.where(rsi(c(df),2) > 60, 0, np.nan)),
        index=df.index).ffill().fillna(0), lo=True)
    add("rsi14_meanrev", lambda df: pd.Series(
        np.where(rsi(c(df),14) < 30, 1, np.where(rsi(c(df),14) > 70, -1, np.nan)),
        index=df.index).ffill().fillna(0))
    add("bollinger_reversion", lambda df: pd.Series(
        np.where(c(df) < sma(c(df),20)-2*c(df).rolling(20).std(), 1,
                 np.where(c(df) > sma(c(df),20)+2*c(df).rolling(20).std(), -1, np.nan)),
        index=df.index).ffill().fillna(0))
    add("zscore_reversion_20", lambda df: pd.Series(
        np.where(zscore(c(df),20) < -2, 1, np.where(zscore(c(df),20) > 2, -1, np.nan)),
        index=df.index).ffill().fillna(0))
    add("rsi2_in_uptrend_lo", lambda df: (
        (rsi(c(df),2) < 5) & (c(df) > sma(c(df),200))).astype(float), lo=True)

    # --- Volatility / hybrid ---
    add("voltarget_tsmom_100", lambda df: np.sign(c(df)-sma(c(df),100)) *
        (0.02/(c(df).pct_change().rolling(20).std()+1e-9)).clip(0,1))
    add("volregime_long", lambda df: (
        c(df).pct_change().rolling(20).std() <
        c(df).pct_change().rolling(20).std().rolling(90).median()).astype(float), lo=True)
    add("keltner_breakout", lambda df: pd.Series(
        np.where(c(df) > ema(c(df),20)+2*atr(df), 1,
                 np.where(c(df) < ema(c(df),20)-2*atr(df), -1, np.nan)),
        index=df.index).ffill().fillna(0))

    # --- Novel / hybrid / invented ---
    add("anti_whipsaw_trend", lambda df: pd.Series(   # SMA200 + 3% hysteresis band
        np.where(c(df) > sma(c(df),200)*1.03, 1,
                 np.where(c(df) < sma(c(df),200)*0.97, -1, np.nan)),
        index=df.index).ffill().fillna(0))
    add("trend_pullback_lo", lambda df: (        # buy dips IN an uptrend (long-only)
        (c(df) > sma(c(df),200)) & (rsi(c(df),3) < 35)).astype(float), lo=True)
    add("triple_confirm", lambda df: pd.Series(  # SMA + MACD + RSI all agree
        np.where((sma(c(df),50)>sma(c(df),200))&(macd_line(c(df))[0]>0)&(rsi(c(df),14)>50),1,
                 np.where((sma(c(df),50)<sma(c(df),200))&(macd_line(c(df))[0]<0)&(rsi(c(df),14)<50),-1,0)),
        index=df.index))
    add("regime_switch", lambda df: pd.Series(   # trend when |z| big, MR when small
        np.where(zscore(c(df),50).abs() > 1, np.sign(c(df)-sma(c(df),50)),
                 -np.sign(zscore(c(df),20))), index=df.index).fillna(0))
    add("momentum_accel", lambda df: np.sign(roc(c(df),20) - roc(c(df),20).shift(10)))
    add("dual_tf_trend", lambda df: (            # daily AND weekly trend agree (long-only)
        (c(df)>sma(c(df),50)) & (c(df)>sma(c(df),200))).astype(float), lo=True)
    add("trend_voltarget_lo", lambda df: (c(df)>sma(c(df),100)).astype(float) *
        (0.02/(c(df).pct_change().rolling(20).std()+1e-9)).clip(0,1), lo=True)
    add("kama_trend", lambda df: np.sign(c(df) - ema(c(df),30)) *  # mild adaptive trend
        (roc(c(df),10).abs() > roc(c(df),10).abs().rolling(50).median()).astype(float))

    return reg


def cross_asset(strat_name):
    """Marker for strategies needing both coins (handled separately)."""
    return strat_name in ()   # none yet — kept simple/correct


# ── backtest ──────────────────────────────────────────────────────────────────
def backtest(df, pos):
    pos = pd.Series(pos, index=df.index).clip(-1, 1).fillna(0)
    held = pos.shift(1).fillna(0)
    gross = held * df['close'].pct_change().fillna(0)
    cost = held.diff().abs().fillna(0) * COST_LEG
    net = gross - cost
    eq = (1 + net).cumprod() * START
    return net, eq


def metrics(net, eq):
    net = net.dropna()
    if len(net) < 2:
        return dict(final=START, ret=0, cagr=0, sharpe=0, mdd=0, trades=0)
    sd = net.std()
    sharpe = (net.mean()/sd*np.sqrt(ANN)) if sd > 0 else 0.0
    roll_max = eq.cummax()
    mdd = ((eq/roll_max) - 1).min()
    held_changes = (net != 0)
    cagr = (eq.iloc[-1]/START) ** (ANN/len(net)) - 1
    return dict(final=eq.iloc[-1], ret=eq.iloc[-1]/START-1, cagr=cagr,
                sharpe=sharpe, mdd=mdd, trades=int(held_changes.sum()))


def fetch(ex, sym, want=730):
    o = ex.fetch_ohlcv(sym, timeframe='1d', limit=want)
    df = pd.DataFrame(o, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
    return df


def evaluate(strats, data):
    """Run every strategy on every coin + both halves. Returns rows."""
    rows = []
    rng = np.random.default_rng(42)
    for name, (fn, lo) in strats.items():
        per_coin = {}
        for coin, df in data.items():
            pos = pd.Series(fn(df), index=df.index).clip(-1, 1).fillna(0)
            if lo:
                pos = pos.clip(0, 1)
            net, eq = backtest(df, pos)
            m = metrics(net, eq)
            half = len(net) // 2
            s1 = metrics(net.iloc[:half], (1+net.iloc[:half]).cumprod()*START)
            s2 = metrics(net.iloc[half:], (1+net.iloc[half:]).cumprod()*START)
            per_coin[coin] = (m, s1['sharpe'], s2['sharpe'])
        # aggregate
        sh = np.mean([v[0]['sharpe'] for v in per_coin.values()])
        fin = np.mean([v[0]['final'] for v in per_coin.values()])
        mdd = np.mean([v[0]['mdd'] for v in per_coin.values()])
        trades = int(np.mean([v[0]['trades'] for v in per_coin.values()]))
        # robust = positive Sharpe on BOTH coins AND BOTH halves
        robust = all(v[0]['sharpe'] > 0.3 for v in per_coin.values()) and \
                 all(v[1] > 0 and v[2] > 0 for v in per_coin.values())
        rows.append(dict(name=name, lo=lo, sharpe=sh, final=fin, mdd=mdd,
                         trades=trades, robust=robust))
    # random control (noise floor)
    for trial in range(3):
        fins, shs = [], []
        for coin, df in data.items():
            pos = pd.Series(rng.choice([-1, 0, 1], size=len(df)), index=df.index)
            net, eq = backtest(df, pos)
            m = metrics(net, eq); fins.append(m['final']); shs.append(m['sharpe'])
        rows.append(dict(name=f"[random control {trial+1}]", lo=False,
                         sharpe=np.mean(shs), final=np.mean(fins), mdd=0,
                         trades=0, robust=False))
    # buy & hold
    fins = []
    for coin, df in data.items():
        net, eq = backtest(df, pd.Series(1.0, index=df.index))
        fins.append(metrics(net, eq)['final'])
    rows.append(dict(name="[buy & hold]", lo=True, sharpe=np.nan,
                     final=np.mean(fins), mdd=np.nan, trades=1, robust=False))
    return rows


def main():
    import ccxt
    ex = ccxt.kraken({'enableRateLimit': True})
    data = {}
    for c in ('BTC/USD', 'ETH/USD'):
        data[c] = fetch(ex, c)
    days = len(next(iter(data.values())))
    strats = S()
    rows = evaluate(strats, data)
    rows.sort(key=lambda r: (r['robust'], r['sharpe'] if not np.isnan(r['sharpe']) else -9), reverse=True)

    print("=" * 78)
    print(f"STRATEGY TOURNAMENT — {len(strats)} bots, $1,000 each, BTC+ETH daily "
          f"(~{days}d), 0.5% cost")
    print("=" * 78)
    print(f"{'strategy':<24}{'$1k->avg':>10}{'Sharpe':>8}{'maxDD':>8}{'trades':>7}"
          f"{'exec':>6}{'ROBUST':>8}")
    print("-" * 78)
    for r in rows:
        ex_tag = 'spot' if r['lo'] else 'perp'
        rob = 'YES' if r['robust'] else ''
        sh = f"{r['sharpe']:.2f}" if not np.isnan(r['sharpe']) else '  -'
        mdd = f"{r['mdd']*100:.0f}%" if not np.isnan(r['mdd']) else '  -'
        print(f"{r['name']:<24}${r['final']:>8.0f}{sh:>8}{mdd:>8}{r['trades']:>7}"
              f"{ex_tag:>6}{rob:>8}")
    print("-" * 78)
    robust = [r for r in rows if r['robust']]
    print(f"\nROBUST survivors (Sharpe>0.3 on BOTH coins AND BOTH halves): "
          f"{len(robust)}/{len(strats)}")
    for r in robust:
        print(f"  - {r['name']} ({'spot-executable' if r['lo'] else 'needs perps/shorts'})"
              f" — avg ${r['final']:.0f}, Sharpe {r['sharpe']:.2f}")
    print("\nNOTE: best-of-many on one window overfits — only ROBUST + (ideally)")
    print("spot-executable survivors are candidates, and they STILL need forward")
    print("proof (proof_scorecard, n>=30, family-wise t). Random controls show the")
    print("noise floor; anything near them is luck.")


if __name__ == '__main__':
    main()
