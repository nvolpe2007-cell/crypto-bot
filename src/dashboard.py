"""
Web dashboard — serves on port 8080
"""

import asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from .state import read_state

app = FastAPI()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crypto Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace; font-size: 14px; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { color: #58a6ff; font-size: 18px; letter-spacing: 2px; }
  .badge { padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
  .badge.running { background: #1a3c2a; color: #3fb950; border: 1px solid #3fb950; }
  .badge.stopped { background: #3c1a1a; color: #f85149; border: 1px solid #f85149; }
  .badge.paper { background: #1a2a3c; color: #58a6ff; border: 1px solid #58a6ff; }
  .badge.live { background: #3c2a1a; color: #e3b341; border: 1px solid #e3b341; }
  main { padding: 20px 24px; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card h2 { color: #8b949e; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 8px; }
  .card .value { font-size: 22px; font-weight: bold; color: #e6edf3; }
  .card .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  .neu { color: #8b949e; }
  table { width: 100%; border-collapse: collapse; }
  th { color: #8b949e; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; padding: 8px 12px; border-bottom: 1px solid #30363d; text-align: left; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; color: #c9d1d9; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  .section-title { color: #8b949e; font-size: 12px; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px; }
  .no-data { color: #484f58; text-align: center; padding: 20px; }
  .indicator-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
  .pill { padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #21262d; color: #8b949e; }
  .pill.buy { background: #1a3c2a; color: #3fb950; }
  .pill.sell { background: #3c1a1a; color: #f85149; }
  #last-update { color: #484f58; font-size: 11px; }
  .uptime { color: #8b949e; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>&#9670; CRYPTO BOT</h1>
  <div style="display:flex;gap:10px;align-items:center;">
    <span id="mode-badge" class="badge paper">PAPER</span>
    <span id="status-badge" class="badge running">RUNNING</span>
    <span id="last-update"></span>
  </div>
</header>
<main>
  <div id="learning-bar" style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px;margin-bottom:14px;font-size:12px;color:#8b949e;display:flex;gap:24px;align-items:center;">
    <span style="color:#58a6ff;font-weight:bold;letter-spacing:1px;">LEARNING</span>
    <span id="learn-total">-- trades recorded</span>
    <span id="learn-wr">Win rate: --%</span>
    <span id="learn-status" style="color:#484f58;">Needs 5+ trades to start adjusting thresholds</span>
  </div>
  <div class="grid-4">
    <div class="card">
      <h2>Equity</h2>
      <div class="value" id="equity">--</div>
      <div class="sub" id="cash">Cash: --</div>
    </div>
    <div class="card">
      <h2>Total PnL</h2>
      <div class="value" id="pnl">--</div>
      <div class="sub" id="pnl-pct">--</div>
    </div>
    <div class="card">
      <h2>Win Rate</h2>
      <div class="value" id="win-rate">--</div>
      <div class="sub" id="trade-count">-- trades</div>
    </div>
    <div class="card">
      <h2>Open Positions</h2>
      <div class="value" id="open-pos">--</div>
      <div class="sub" id="unrealized">Unrealized: --</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="section-title">Open Positions</div>
      <table id="positions-table">
        <thead><tr><th>Pair</th><th>Entry</th><th>Current</th><th>Size</th><th>PnL</th></tr></thead>
        <tbody id="positions-body"><tr><td colspan="5" class="no-data">No open positions</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <div class="section-title">Market Overview</div>
      <div id="market-overview"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px">
    <div class="section-title">Account Equity</div>
    <div style="position:relative;height:180px;">
      <canvas id="equity-chart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px">
    <div class="section-title">Funding Rate Opportunities <span style="color:#484f58;font-size:10px;margin-left:8px;">SHORT PERP + LONG SPOT to collect positive funding</span></div>
    <table>
      <thead><tr><th>Exchange</th><th>Symbol</th><th>8h Rate</th><th>APY</th><th>Action</th></tr></thead>
      <tbody id="funding-body"><tr><td colspan="5" class="no-data">Scanning...</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <div class="section-title">Recent Trades</div>
    <table>
      <thead><tr><th>Pair</th><th>Entry Time</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="6" class="no-data">No trades yet</td></tr></tbody>
    </table>
  </div>
</main>

<script>
function fmt(n, dec=2) { return n != null ? Number(n).toFixed(dec) : '--'; }
function fmtUSD(n) { return n != null ? '$' + fmt(n) : '--'; }
function fmtPct(n) { return n != null ? fmt(n) + '%' : '--'; }
function colorClass(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu'; }
function set(id, val) { const el = document.getElementById(id); if(el) el.innerHTML = val; }
function cls(id, c) { const el = document.getElementById(id); if(el) { el.className = el.className.replace(/pos|neg|neu/g,''); el.classList.add(c); } }

// ── Equity chart ────────────────────────────────────────────────
let equityChart = null;

function initChart() {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{
      label: 'Account Equity ($)',
      data: [],
      borderColor: '#3fb950',
      backgroundColor: 'rgba(63,185,80,0.08)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: '#3fb950',
      fill: true,
      tension: 0.3
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e', font: { size: 10 },
                      callback: v => '$' + v.toFixed(2) },
             grid: { color: '#21262d' } }
      }
    }
  });
}

function updateChart(curve) {
  if (!equityChart) initChart();
  if (!curve || curve.length === 0) return;
  equityChart.data.labels   = curve.map(p => p.t);
  equityChart.data.datasets[0].data = curve.map(p => p.v);
  equityChart.update('none');
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    const acc = d.account || {};
    const pnl = acc.total_pnl || 0;
    const pnlPct = acc.pnl_pct || 0;
    const wins = acc.winning_trades || 0;
    const total = acc.closed_trades || 0;
    const winRate = total > 0 ? (wins/total*100) : 0;
    const unrealized = Object.values(d.positions || {}).reduce((s,p) => s + (p.unrealized_pnl||0), 0);

    // Header
    const sb = document.getElementById('status-badge');
    sb.textContent = (d.status||'unknown').toUpperCase();
    sb.className = 'badge ' + (d.status === 'running' ? 'running' : 'stopped');
    const mb = document.getElementById('mode-badge');
    mb.textContent = (d.mode||'paper').toUpperCase();
    mb.className = 'badge ' + (d.mode === 'live' ? 'live' : 'paper');
    document.getElementById('last-update').textContent = d.last_update ? 'Updated ' + new Date(d.last_update).toLocaleTimeString() : '';

    // Stats
    set('equity', fmtUSD(acc.total_equity));
    set('cash', 'Cash: ' + fmtUSD(acc.cash));
    set('pnl', '<span class="' + colorClass(pnl) + '">' + (pnl >= 0 ? '+' : '') + fmtUSD(pnl) + '</span>');
    set('pnl-pct', '<span class="' + colorClass(pnlPct) + '">' + (pnlPct >= 0 ? '+' : '') + fmtPct(pnlPct) + '</span>');
    set('win-rate', '<span class="' + colorClass(winRate - 50) + '">' + fmtPct(winRate) + '</span>');
    set('trade-count', wins + 'W / ' + (total - wins) + 'L (' + total + ' total)');
    set('open-pos', Object.keys(d.positions || {}).length);
    set('unrealized', 'Unrealized: <span class="' + colorClass(unrealized) + '">' + (unrealized >= 0 ? '+' : '') + fmtUSD(unrealized) + '</span>');

    // Positions
    const posBody = document.getElementById('positions-body');
    const positions = d.positions || {};
    const prices = d.prices || {};
    if (Object.keys(positions).length === 0) {
      posBody.innerHTML = '<tr><td colspan="5" class="no-data">No open positions</td></tr>';
    } else {
      posBody.innerHTML = Object.entries(positions).map(([sym, p]) => {
        const cur = prices[sym] || p.entry_price;
        const upnl = p.unrealized_pnl || 0;
        const upnlPct = p.entry_price > 0 ? (cur - p.entry_price) / p.entry_price * 100 : 0;
        return `<tr>
          <td><b>${sym}</b></td>
          <td>${fmtUSD(p.entry_price)}</td>
          <td>${fmtUSD(cur)}</td>
          <td>${fmt(p.size, 6)}</td>
          <td class="${colorClass(upnl)}">${upnl >= 0 ? '+' : ''}${fmtUSD(upnl)} (${upnlPct >= 0 ? '+' : ''}${fmtPct(upnlPct)})</td>
        </tr>`;
      }).join('');
    }

    // Market overview
    const indicators = d.indicators || {};
    const moEl = document.getElementById('market-overview');
    if (Object.keys(prices).length === 0) {
      moEl.innerHTML = '<div class="no-data">Waiting for data...</div>';
    } else {
      moEl.innerHTML = Object.entries(prices).map(([sym, price]) => {
        const ind = indicators[sym] || {};
        const sig = (ind.signal || 'HOLD').toUpperCase();
        const sigCls = sig === 'BUY' ? 'buy' : sig === 'SELL' ? 'sell' : '';
        return `<div style="padding:10px 0;border-bottom:1px solid #21262d;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <b>${sym}</b>
            <span style="font-size:16px;">${fmtUSD(price)}</span>
          </div>
          <div class="indicator-row">
            <span class="pill ${sigCls}">${sig}</span>
            ${ind.confidence != null ? '<span class="pill ' + (ind.confidence >= 75 ? 'buy' : '') + '">Conf ' + ind.confidence + '/100</span>' : ''}
            ${ind.rsi != null ? '<span class="pill">RSI ' + fmt(ind.rsi,1) + '</span>' : ''}
            ${ind.ema_fast != null ? '<span class="pill">EMA ' + fmt(ind.ema_fast,0) + '/' + fmt(ind.ema_slow,0) + '</span>' : ''}
            ${ind.adx != null ? '<span class="pill">ADX ' + fmt(ind.adx,1) + '</span>' : ''}
            ${ind.volume_ratio != null ? '<span class="pill">Vol ' + fmt(ind.volume_ratio,2) + 'x</span>' : ''}
          </div>
        </div>`;
      }).join('');
    }

    // Learning bar
    const learn = d.learning || {};
    if (learn.total > 0) {
      document.getElementById('learn-total').textContent = learn.total + ' trades recorded';
      document.getElementById('learn-wr').textContent = 'Win rate: ' + (learn.win_rate || 0) + '%';
      const status = learn.total >= 5
        ? (learn.win_rate >= 50 ? 'Performing well — thresholds normal' : 'Raising thresholds on weak setups')
        : 'Needs ' + (5 - learn.total) + ' more trades to start learning';
      document.getElementById('learn-status').textContent = status;
      document.getElementById('learn-status').style.color = learn.win_rate >= 50 ? '#3fb950' : '#e3b341';
    }

    // Funding opportunities
    const fundingBody = document.getElementById('funding-body');
    const funding = d.funding_opportunities || [];
    if (funding.length === 0) {
      fundingBody.innerHTML = '<tr><td colspan="5" class="no-data">Scanning Binance & Bybit...</td></tr>';
    } else {
      fundingBody.innerHTML = funding.slice(0, 10).map(f => {
        const apy = f.apy || 0;
        const apyCls = Math.abs(apy) >= 30 ? 'pos' : Math.abs(apy) >= 15 ? '' : 'neu';
        const rateCls = f.rate_8h > 0 ? 'pos' : 'neg';
        return `<tr>
          <td>${f.exchange}</td>
          <td><b>${f.symbol}</b></td>
          <td class="${rateCls}">${f.rate_8h > 0 ? '+' : ''}${fmt(f.rate_8h, 4)}%</td>
          <td class="${apyCls}"><b>${apy > 0 ? '+' : ''}${fmt(apy, 1)}%</b></td>
          <td style="font-size:11px;color:#8b949e">${f.action}</td>
        </tr>`;
      }).join('');
    }

    // Recent trades
    const tradesBody = document.getElementById('trades-body');
    const trades = d.recent_trades || [];
    if (trades.length === 0) {
      tradesBody.innerHTML = '<tr><td colspan="6" class="no-data">No trades yet</td></tr>';
    } else {
      tradesBody.innerHTML = trades.slice(-15).reverse().map(t => {
        const pnlVal = t.pnl || 0;
        return `<tr>
          <td><b>${t.symbol || '--'}</b></td>
          <td>${t.entry_time ? new Date(t.entry_time).toLocaleString() : '--'}</td>
          <td>${fmtUSD(t.entry_price)}</td>
          <td>${fmtUSD(t.exit_price)}</td>
          <td class="${colorClass(pnlVal)}">${pnlVal >= 0 ? '+' : ''}${fmtUSD(pnlVal)}</td>
          <td>${t.reason || '--'}</td>
        </tr>`;
      }).join('');
    }
  } catch(e) {
    document.getElementById('status-badge').textContent = 'OFFLINE';
    document.getElementById('status-badge').className = 'badge stopped';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/state")
async def state():
    return JSONResponse(read_state())


async def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
