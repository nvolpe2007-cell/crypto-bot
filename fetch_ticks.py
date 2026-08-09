import ccxt, time, json, sys
from pathlib import Path

def fetch_ticks(symbol, days, outpath):
    ex = ccxt.kraken({'enableRateLimit': True})
    since = ex.milliseconds() - days*86400_000
    all_trades = []
    last_since = since
    stall = 0
    while True:
        try:
            batch = ex.fetch_trades(symbol, since=last_since, limit=1000)
        except Exception as e:
            print(f"err {e}, retry", file=sys.stderr); time.sleep(2); continue
        if not batch:
            break
        all_trades += batch
        nxt = batch[-1]['timestamp'] + 1
        if nxt <= last_since:
            stall += 1
            if stall > 3: break
            nxt = last_since + 1
        else:
            stall = 0
        last_since = nxt
        if last_since >= ex.milliseconds():
            break
        if len(all_trades) % 20000 < 1000:
            print(f"  {symbol}: {len(all_trades)} trades, at {batch[-1]['datetime']}", file=sys.stderr)
    with open(outpath, 'w') as f:
        for t in all_trades:
            f.write(json.dumps({"ts": t["timestamp"], "price": t["price"], "amount": t["amount"], "side": t["side"]}) + "\n")
    print(f"{symbol}: saved {len(all_trades)} trades -> {outpath}")

if __name__ == "__main__":
    sym = sys.argv[1]; days = float(sys.argv[2]); out = sys.argv[3]
    fetch_ticks(sym, days, out)
