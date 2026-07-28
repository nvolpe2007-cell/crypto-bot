# stockbot — intraday US-equities backtest (paper/sim only)

An **isolated, backtest-only** project for a *defensible intraday* equities strategy
(Opening-Range Breakout), built with the same honesty discipline as the crypto bot:
honest costs, no look-ahead, EOD-flat, and a **pre-registered proof bar**. There is
**no broker and no live trading** here — it's a simulator.

> It lives inside the crypto-bot repo only because that's the only place this
> environment can persist + push code. It has **zero coupling** to the crypto code
> (depends on pandas/numpy only) and is a clean `git subtree split` candidate.

## Why ORB, and why not a "scalper"
Retail **scalping loses** — you're racing HFT at timescales where your edge after
spread/fees/latency is ~zero (the same reason the crypto scalper failed at t≈−8.82),
and live US day-trading under **$25k** is capped by the **PDT rule** (3 day-trades /
5 business days). So this builds the version with an actual chance: **one
high-conviction intraday setup per symbol per day** (ORB), and lets the proof bar —
not hope — decide if it has an edge.

## Run it
```bash
pip install -r stockbot/requirements.txt

# offline demo on deterministic synthetic data:
python -m stockbot.run_backtest --synthetic --direction both

# your own bars (CSV: time,open,high,low,close,volume):
python -m stockbot.run_backtest --csv SPY_5m.csv --symbol SPY --or-minutes 15 --target-r 2

# pull intraday bars via yfinance (needs `pip install yfinance` + network):
python -m stockbot.run_backtest --yf SPY --interval 5m --period 60d
```

## Post results to Telegram
`stockbot` has its **own** Telegram poster (independent of the crypto bot — the crypto
`CRYPTO_TELEGRAM_MUTE` does not affect it):
```bash
export STOCKBOT_TELEGRAM=1
export TELEGRAM_BOT_TOKEN=...           # can be the same bot as crypto
export STOCKBOT_TELEGRAM_CHAT_ID=...    # falls back to TELEGRAM_CHAT_ID
python -m stockbot.run_backtest --yf SPY --interval 5m --period 60d --telegram --capital 10000
```
`--telegram` posts the P&L summary + verdict; `--capital` translates net % into $.
For a recurring post, cron the command.

## What you get
A scorecard per run: trade count, net-of-cost expectancy, win rate, **t-stat**,
per-trade Sharpe, max drawdown, profit factor, and a verdict against the bar
**`n≥30 & expectancy>0 & t>2`**.

## Live PAPER trading on Alpaca (optional)
The same ORB edge, placing **paper** orders via Alpaca (no real money). Get **free
paper keys** from the Alpaca dashboard (toggle to *Paper Trading* → *Generate API
Keys*), then:
```bash
pip install alpaca-py
export ALPACA_API_KEY=...  ALPACA_API_SECRET=...  ALPACA_PAPER=1
export STOCKBOT_SYMBOLS=SPY,QQQ  STOCKBOT_NOTIONAL=2000  STOCKBOT_TARGET_R=2
export STOCKBOT_TELEGRAM=1  TELEGRAM_BOT_TOKEN=...  STOCKBOT_TELEGRAM_CHAT_ID=...
# run on a schedule during market hours (e.g. cron every 5 min, 9:30–16:00 ET):
python -m stockbot.live_paper
```
Each run: after the opening range and before the cutoff, takes **at most one trade
per symbol per day** on a breakout — a market entry with a **server-side bracket**
(stop + target), so a missed run can't strand a position — flattens at the close,
and posts the day's P&L to Telegram. **Fail-safe:** no keys / no `alpaca-py` → it
no-ops (no orders). **Verify everything in your Alpaca paper dashboard.**

## Choose params HONESTLY: walk-forward + robustness
Don't sweep `--or-minutes`/`--target-r` until a backtest looks good — that's how you
fool yourself. Instead:
```bash
python -m stockbot.run_walkforward --synthetic
python -m stockbot.run_walkforward --csv SPY_5m.csv --symbol SPY --train-days 40 --test-days 10
```
It **selects** the best params on each TRAIN window and trades them **untouched** on
the next unseen TEST window; the pooled **out-of-sample** record is the honest verdict.
It also reports **robustness** (is the edge a broad plateau or one lucky cell?) and a
**deflated Sharpe** that corrects the best cell for how many param sets you tried.
Trust the OOS block, not the best in-sample cell.

## The honesty bar (read before trusting anything)
- A passing backtest is **IN-SAMPLE**. It is necessary, not sufficient. Before real
  money: **walk-forward / out-of-sample**, and a correction for **how many parameter
  sets you tried** (the deflated-Sharpe / multiple-testing problem — the same trap
  the crypto `proof_scorecard` guards against). Sweeping `--or-minutes`/`--target-r`
  until something passes is how you fool yourself.
- Costs here are spread+slippage only; real fills, gaps, halts, and partial fills are
  worse.
- **PDT rule**: live day-trading <$25k equity is throttled to 3/5 days.
- It's paper/sim. No broker is connected; nothing here places an order.

## Layout
```
stockbot/
  strategy.py    # ORBConfig + Trade + simulate_day (the ORB rules; no look-ahead)
  backtest.py    # run_backtest / run_multi over sessions; one trade/day
  metrics.py     # proof stats + pre-registered verdict
  data.py        # load_csv / fetch_yfinance / synthetic_intraday
  run_backtest.py# CLI
  tests/         # ORB scenarios, cost/EOD/gap, metrics thresholds
```
