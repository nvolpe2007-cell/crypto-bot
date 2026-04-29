"""
Mobile-first trading dashboard with Server-Sent Events for real-time updates.
Access on phone via: http://<your-pc-ip>:8080
"""

import asyncio
import json
import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from .state import read_state

app = FastAPI()

# Simple token auth — set DASHBOARD_TOKEN in .env to secure the dashboard.
# If not set, dashboard is open (fine for personal LAN use).
_TOKEN = os.getenv('DASHBOARD_TOKEN', '')


def _auth_ok(request: Request) -> bool:
    if not _TOKEN:
        return True
    return request.cookies.get('dash_token') == _TOKEN or \
           request.query_params.get('token') == _TOKEN

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="theme-color" content="#060a12">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>CRYPTO BOT</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:      #060a12;
  --bg2:     #0d1421;
  --bg3:     #111827;
  --border:  #1e2d3d;
  --text:    #cdd9e5;
  --dim:     #4a6080;
  --green:   #00f5a0;
  --red:     #ff4d6d;
  --blue:    #4d9fff;
  --purple:  #b44dff;
  --gold:    #ffd700;
  --orange:  #ff9500;
  --cyan:    #00e5ff;
  --font:    'SF Mono', 'Fira Code', 'Consolas', monospace;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { background:var(--bg); color:var(--text); font-family:var(--font);
             font-size:13px; min-height:100vh; overscroll-behavior:none; }

/* ── HEADER ─────────────────────────────────────────────────────── */
header {
  position: sticky; top:0; z-index:100;
  background: linear-gradient(135deg, #060a12 0%, #0d1421 100%);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  display: flex; align-items:center; justify-content:space-between;
  backdrop-filter: blur(10px);
}
.logo { color:var(--cyan); font-size:16px; font-weight:700; letter-spacing:3px; }
.logo span { color:var(--blue); }
.header-right { display:flex; gap:8px; align-items:center; }
.badge {
  padding:3px 8px; border-radius:20px; font-size:11px; font-weight:700;
  letter-spacing:1px;
}
.badge-paper  { background:#0a1a2e; color:var(--blue);   border:1px solid var(--blue); }
.badge-live   { background:#2e1a0a; color:var(--gold);   border:1px solid var(--gold); }
.badge-run    { background:#002e1a; color:var(--green);  border:1px solid var(--green); }
.badge-stop   { background:#2e000a; color:var(--red);    border:1px solid var(--red); }
.pulse {
  display:inline-block; width:8px; height:8px; border-radius:50%;
  background:var(--green); margin-right:4px;
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
  0%,100% { opacity:1; box-shadow:0 0 0 0 rgba(0,245,160,.4); }
  50%      { opacity:.7; box-shadow:0 0 0 6px rgba(0,245,160,0); }
}

/* ── LAYOUT ──────────────────────────────────────────────────────── */
main { padding:12px; max-width:600px; margin:0 auto; }
.section { margin-bottom:12px; }
.section-title {
  font-size:10px; letter-spacing:2px; text-transform:uppercase;
  color:var(--dim); margin-bottom:8px; padding-left:2px;
}
.card {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:12px; padding:14px;
  transition: border-color .2s;
}
.card:active { border-color:var(--blue); }

/* ── EQUITY ROW ──────────────────────────────────────────────────── */
.equity-row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.eq-main {
  background: linear-gradient(135deg, #0a1a2e, #060a12);
  border:1px solid var(--blue); border-radius:12px; padding:16px;
}
.eq-label { font-size:10px; color:var(--dim); letter-spacing:1px; margin-bottom:4px; }
.eq-value { font-size:28px; font-weight:700; color:var(--text); letter-spacing:-1px; }
.eq-sub   { font-size:11px; margin-top:4px; }
.eq-pnl {
  border-radius:12px; padding:16px;
  display:flex; flex-direction:column; justify-content:center;
}

/* ── PRICE CARDS ─────────────────────────────────────────────────── */
.price-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
.price-card {
  background:var(--bg2); border:1px solid var(--border);
  border-radius:10px; padding:10px 8px; text-align:center;
}
.price-sym { font-size:10px; color:var(--dim); margin-bottom:4px; }
.price-val { font-size:15px; font-weight:700; color:var(--text); }
.price-change { font-size:10px; margin-top:2px; }

/* ── SIGNAL CARDS ────────────────────────────────────────────────── */
.signal-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
.sig-card {
  border-radius:10px; padding:10px 8px;
  border:1px solid var(--border); background:var(--bg2);
}
.sig-sym  { font-size:10px; color:var(--dim); margin-bottom:6px; }
.sig-badge {
  display:inline-block; padding:2px 8px; border-radius:4px;
  font-size:11px; font-weight:700; margin-bottom:6px;
}
.sig-buy  { background:#003320; color:var(--green); border:1px solid var(--green); }
.sig-sell { background:#3d0015; color:var(--red);   border:1px solid var(--red); }
.sig-hold { background:#0d1421; color:var(--dim);   border:1px solid var(--border); }
.sig-row  { display:flex; gap:4px; flex-wrap:wrap; margin-top:4px; }
.tag {
  padding:1px 5px; border-radius:3px; font-size:9px;
  background:var(--bg3); color:var(--dim);
}

/* ── REGIME ──────────────────────────────────────────────────────── */
.regime-card {
  border-radius:12px; padding:14px;
  border:1px solid var(--border);
  background:var(--bg2);
}
.regime-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
.regime-dot { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
.regime-name { font-size:15px; font-weight:700; }
.regime-hint { font-size:10px; color:var(--dim); margin-top:2px; }
.regime-stats { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-top:10px; }
.rstat { text-align:center; }
.rstat-val { font-size:14px; font-weight:700; }
.rstat-lbl { font-size:9px; color:var(--dim); margin-top:1px; }

/* ── SENTIMENT ROW ───────────────────────────────────────────────── */
.sent-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.sent-card {
  background:var(--bg2); border:1px solid var(--border);
  border-radius:10px; padding:10px;
}
.sent-lbl { font-size:9px; color:var(--dim); margin-bottom:4px; }
.sent-val { font-size:18px; font-weight:700; }
.sent-sub { font-size:10px; color:var(--dim); margin-top:2px; }

/* ── IV STRIP ────────────────────────────────────────────────────── */
.iv-strip {
  display:grid; grid-template-columns:1fr 1fr; gap:8px;
  margin-top:8px;
}
.iv-bar-wrap { margin-top:6px; background:var(--bg3); border-radius:4px; height:4px; overflow:hidden; }
.iv-bar      { height:100%; border-radius:4px; transition:width .5s; }

/* ── CVaR WEIGHTS ────────────────────────────────────────────────── */
.cvar-row { display:flex; gap:8px; }
.cvar-asset {
  flex:1; background:var(--bg2); border:1px solid var(--border);
  border-radius:10px; padding:10px; text-align:center;
}
.cvar-sym { font-size:9px; color:var(--dim); margin-bottom:4px; }
.cvar-pct { font-size:20px; font-weight:700; }
.cvar-bar-wrap { margin-top:6px; background:var(--bg3); border-radius:4px; height:3px; }
.cvar-bar { height:100%; background:var(--blue); border-radius:4px; transition:width .5s; }

/* ── POSITIONS ───────────────────────────────────────────────────── */
.pos-card {
  background:var(--bg2); border-radius:10px; padding:12px;
  margin-bottom:6px;
  border-left:3px solid var(--blue);
}
.pos-header { display:flex; justify-content:space-between; margin-bottom:6px; }
.pos-sym  { font-size:14px; font-weight:700; }
.pos-pnl  { font-size:14px; font-weight:700; }
.pos-details { display:grid; grid-template-columns:1fr 1fr; gap:4px; }
.pos-detail { font-size:10px; color:var(--dim); }
.pos-detail span { color:var(--text); }

/* ── TRADES ──────────────────────────────────────────────────────── */
.trade-row {
  display:flex; align-items:center; justify-content:space-between;
  padding:10px 0; border-bottom:1px solid var(--border);
}
.trade-row:last-child { border-bottom:none; }
.trade-left  { display:flex; flex-direction:column; gap:2px; }
.trade-sym   { font-size:13px; font-weight:700; }
.trade-time  { font-size:10px; color:var(--dim); }
.trade-right { display:flex; flex-direction:column; align-items:flex-end; gap:2px; }
.trade-pnl   { font-size:14px; font-weight:700; }
.trade-rsn   { font-size:10px; color:var(--dim); }
.win-badge   { padding:2px 6px; border-radius:4px; font-size:10px; font-weight:700; }
.win  { background:#002e1a; color:var(--green); }
.lose { background:#2e000a; color:var(--red); }

/* ── CHART ───────────────────────────────────────────────────────── */
.chart-wrap { position:relative; height:160px; }

/* ── STATS BAR ───────────────────────────────────────────────────── */
.stats-bar {
  display:grid; grid-template-columns:repeat(3,1fr); gap:8px;
}
.stat-item { text-align:center; padding:10px 6px; }
.stat-val { font-size:18px; font-weight:700; }
.stat-lbl { font-size:9px; color:var(--dim); margin-top:2px; text-transform:uppercase; letter-spacing:1px; }

/* ── FUNDING ─────────────────────────────────────────────────────── */
.fund-row {
  display:flex; justify-content:space-between; align-items:center;
  padding:8px 0; border-bottom:1px solid var(--border); font-size:12px;
}
.fund-row:last-child { border-bottom:none; }

/* ── FOOTER ──────────────────────────────────────────────────────── */
footer {
  text-align:center; padding:16px; color:var(--dim); font-size:10px;
  border-top:1px solid var(--border); margin-top:12px;
}

/* ── UTILITY ─────────────────────────────────────────────────────── */
.pos  { color:var(--green); }
.neg  { color:var(--red); }
.neu  { color:var(--dim); }
.no-data { text-align:center; padding:20px; color:var(--dim); font-size:12px; }
.glow-green { text-shadow: 0 0 12px rgba(0,245,160,.5); }
.glow-red   { text-shadow: 0 0 12px rgba(255,77,109,.5); }
.sep { width:100%; height:1px; background:var(--border); margin:4px 0; }
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div class="logo">&#9670; <span>CRYPTO</span>BOT</div>
  <div class="header-right">
    <span id="mode-badge" class="badge badge-paper">PAPER</span>
    <span id="status-badge" class="badge badge-run">
      <span class="pulse" id="pulse-dot"></span>LIVE
    </span>
  </div>
</header>

<main>

<!-- EQUITY -->
<div class="section">
  <div class="equity-row">
    <div class="eq-main">
      <div class="eq-label">ACCOUNT EQUITY</div>
      <div class="eq-value" id="equity">$--</div>
      <div class="eq-sub neu" id="cash">Cash: --</div>
    </div>
    <div class="eq-pnl card">
      <div class="eq-label">TOTAL P&amp;L</div>
      <div class="eq-value" id="pnl" style="font-size:22px">--</div>
      <div class="eq-sub" id="pnl-pct">--</div>
      <div class="sep" style="margin:8px 0"></div>
      <div class="eq-label">WIN RATE</div>
      <div id="win-rate" style="font-size:16px;font-weight:700">--%</div>
      <div class="eq-sub neu" id="trade-count">0 trades</div>
    </div>
  </div>
</div>

<!-- PRICES -->
<div class="section">
  <div class="section-title">Live Prices</div>
  <div class="price-grid" id="price-grid">
    <div class="price-card"><div class="price-sym">BTC/USD</div><div class="price-val" id="p-btc">--</div></div>
    <div class="price-card"><div class="price-sym">ETH/USD</div><div class="price-val" id="p-eth">--</div></div>
    <div class="price-card"><div class="price-sym">SOL/USD</div><div class="price-val" id="p-sol">--</div></div>
  </div>
</div>

<!-- SIGNALS -->
<div class="section">
  <div class="section-title">Signals</div>
  <div class="signal-grid" id="signal-grid"></div>
</div>

<!-- REGIME + SENTIMENT -->
<div class="section">
  <div class="section-title">Market Intelligence</div>
  <div id="regime-card" class="regime-card" style="margin-bottom:8px">
    <div class="regime-row">
      <div class="regime-dot" id="regime-dot" style="background:#aaa"></div>
      <div>
        <div class="regime-name" id="regime-name">DETECTING...</div>
        <div class="regime-hint" id="regime-hint"></div>
      </div>
      <div style="margin-left:auto;font-size:20px;font-weight:700" id="regime-conf"></div>
    </div>
    <div class="regime-stats">
      <div class="rstat"><div class="rstat-val" id="r-adx">--</div><div class="rstat-lbl">ADX</div></div>
      <div class="rstat"><div class="rstat-val" id="r-rsi">--</div><div class="rstat-lbl">RSI</div></div>
      <div class="rstat"><div class="rstat-val" id="r-atr">--</div><div class="rstat-lbl">ATR%</div></div>
    </div>
  </div>

  <div class="sent-grid">
    <div class="sent-card">
      <div class="sent-lbl">FEAR &amp; GREED</div>
      <div class="sent-val" id="fg-score">--</div>
      <div class="sent-sub" id="fg-label">--</div>
    </div>
    <div class="sent-card">
      <div class="sent-lbl">BTC DOMINANCE</div>
      <div class="sent-val" id="btc-dom">--%</div>
      <div class="sent-sub" id="mkt-chg">mkt --</div>
    </div>
    <div class="sent-card">
      <div class="sent-lbl">MEMPOOL</div>
      <div class="sent-val" id="mempool">--</div>
      <div class="sent-sub">unconfirmed txs</div>
    </div>
    <div class="sent-card">
      <div class="sent-lbl">LONGS</div>
      <div class="sent-val" id="longs-status">--</div>
      <div class="sent-sub" id="longs-sub"></div>
    </div>
  </div>
</div>

<!-- IV SURFACE -->
<div class="section" id="iv-section" style="display:none">
  <div class="section-title">Implied Volatility</div>
  <div class="iv-strip" id="iv-strip"></div>
</div>

<!-- CVaR ALLOCATION -->
<div class="section" id="cvar-section" style="display:none">
  <div class="section-title">CVaR Portfolio Allocation</div>
  <div class="cvar-row" id="cvar-row"></div>
  <div style="text-align:center;margin-top:6px;font-size:10px;color:var(--dim)" id="cvar-risk"></div>
</div>

<!-- STATS BAR -->
<div class="section">
  <div class="card">
    <div class="stats-bar">
      <div class="stat-item">
        <div class="stat-val" id="stat-open">0</div>
        <div class="stat-lbl">Open</div>
      </div>
      <div class="stat-item">
        <div class="stat-val" id="stat-wins">0</div>
        <div class="stat-lbl">Wins</div>
      </div>
      <div class="stat-item">
        <div class="stat-val" id="stat-losses">0</div>
        <div class="stat-lbl">Losses</div>
      </div>
    </div>
  </div>
</div>

<!-- OPEN POSITIONS -->
<div class="section">
  <div class="section-title">Open Positions</div>
  <div id="positions-wrap"><div class="no-data">No open positions</div></div>
</div>

<!-- EQUITY CHART -->
<div class="section">
  <div class="section-title">Equity Curve</div>
  <div class="card">
    <div class="chart-wrap">
      <canvas id="equity-chart"></canvas>
    </div>
  </div>
</div>

<!-- RECENT TRADES -->
<div class="section">
  <div class="section-title">Recent Trades</div>
  <div class="card" id="trades-wrap"><div class="no-data">No trades yet</div></div>
</div>

<!-- FUNDING RATES -->
<div class="section">
  <div class="section-title">Funding Rate Opportunities</div>
  <div class="card" id="funding-wrap"><div class="no-data">Scanning...</div></div>
</div>

<footer id="footer">Updated --</footer>
</main>

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function fmt(n, d=2) { return n != null ? Number(n).toFixed(d) : '--'; }
function fmtK(n) {
  if (n == null) return '--';
  if (n >= 1000) return '$' + (n/1000).toFixed(1) + 'k';
  return '$' + Number(n).toFixed(2);
}
function fmtUSD(n) { return n != null ? '$' + Number(n).toFixed(2) : '--'; }
function fmtPct(n) { return n != null ? fmt(n) + '%' : '--'; }
function clr(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu'; }
function glow(n) { return n > 0 ? 'glow-green' : n < 0 ? 'glow-red' : ''; }
function set(id, html) { const el=$(id); if(el) el.innerHTML=html; }

// ── Chart ────────────────────────────────────────────────────────────────────
let eChart = null;
function initChart() {
  const ctx = $('equity-chart').getContext('2d');
  eChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        data: [], borderColor: '#00f5a0',
        backgroundColor: ctx => {
          const g = ctx.chart.ctx.createLinearGradient(0,0,0,160);
          g.addColorStop(0,'rgba(0,245,160,.2)');
          g.addColorStop(1,'rgba(0,245,160,0)');
          return g;
        },
        borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false },
                 tooltip: {
                   callbacks: { label: c => '$' + c.raw.toFixed(2) },
                   backgroundColor: '#0d1421', borderColor: '#1e2d3d',
                   borderWidth: 1, titleColor: '#4d9fff', bodyColor: '#cdd9e5'
                 }},
      scales: {
        x: { ticks: { color:'#4a6080', maxTicksLimit:4, font:{size:9} },
             grid:  { color:'#111827' } },
        y: { ticks: { color:'#4a6080', font:{size:9}, callback: v=>'$'+v.toFixed(1) },
             grid:  { color:'#111827' } }
      }
    }
  });
}
initChart();

function updateChart(curve) {
  if (!curve || !curve.length) return;
  eChart.data.labels = curve.map(p=>p.t.slice(11,16));
  eChart.data.datasets[0].data = curve.map(p=>p.v);
  eChart.update('none');
}

// ── Regime display ───────────────────────────────────────────────────────────
const REGIME_COLORS = {
  TRENDING_UP:   '#00f5a0',
  TRENDING_DOWN: '#ff4d6d',
  RANGING:       '#ffd700',
  VOLATILE:      '#ff9500',
  CRASH:         '#ff1744',
};
function updateRegime(regime) {
  if (!regime) return;
  const col = regime.color || REGIME_COLORS[regime.regime] || '#aaa';
  $('regime-dot').style.background = col;
  set('regime-name', `<span style="color:${col}">${regime.regime||'--'}</span>`);
  set('regime-hint', regime.strategy_hint || '');
  set('regime-conf', regime.confidence != null
    ? `<span style="color:${col}">${(regime.confidence*100).toFixed(0)}%</span>` : '');
  set('r-adx',  `<span style="color:${col}">${fmt(regime.adx,1)}</span>`);
  set('r-rsi',  `<span style="color:${regime.rsi>70?'#ff4d6d':regime.rsi<30?'#ffd700':'#cdd9e5'}">${fmt(regime.rsi,1)}</span>`);
  set('r-atr',  `<span style="color:#4d9fff">${fmt(regime.atr_pct,2)}%</span>`);
}

// ── Sentiment display ────────────────────────────────────────────────────────
function updateSentiment(sent) {
  if (!sent) return;
  const fg = sent.fear_greed_score;
  const fgCol = fg < 25 ? '#ff4d6d' : fg < 45 ? '#ff9500' : fg < 55 ? '#ffd700' : fg < 75 ? '#00f5a0' : '#b44dff';
  set('fg-score',  `<span style="color:${fgCol};font-size:24px;font-weight:700">${fg}</span>`);
  set('fg-label',  `<span style="color:${fgCol}">${sent.fear_greed_label||''}</span>`);
  set('btc-dom',   `${fmt(sent.btc_dominance,1)}%`);
  set('mkt-chg',   `<span class="${clr(sent.market_cap_change_24h)}">${sent.market_cap_change_24h>=0?'+':''}${fmt(sent.market_cap_change_24h,1)}% 24h</span>`);
  set('mempool',   Number(sent.mempool_tx_count||0).toLocaleString());
  if (sent.allows_long) {
    set('longs-status', '<span class="pos">OPEN</span>');
    set('longs-sub', 'F&G >= 25');
  } else {
    set('longs-status', '<span class="neg">BLOCKED</span>');
    set('longs-sub', 'Extreme Fear');
  }
}

// ── IV display ───────────────────────────────────────────────────────────────
function updateIV(iv) {
  if (!iv || !Object.keys(iv).length) return;
  $('iv-section').style.display = '';
  const strip = $('iv-strip');
  strip.innerHTML = Object.entries(iv).map(([sym, d]) => {
    const col = d.color || '#4d9fff';
    const pct = d.iv_percentile || 0;
    return `<div class="sent-card">
      <div class="sent-lbl">${sym} ATM IV</div>
      <div class="sent-val" style="color:${col}">${fmt(d.atm_iv,1)}%</div>
      <div class="sent-sub">${d.signal} · ${d.term_structure}</div>
      <div class="iv-bar-wrap">
        <div class="iv-bar" style="width:${pct}%;background:${col}"></div>
      </div>
      <div style="font-size:9px;color:var(--dim);margin-top:2px">
        ${fmt(pct,0)}th pct · size ×${fmt(d.size_mult,2)}
      </div>
    </div>`;
  }).join('');
}

// ── CVaR display ─────────────────────────────────────────────────────────────
const CVAR_COLORS = {'BTC/USD':'#f7931a','ETH/USD':'#627eea','SOL/USD':'#9945ff'};
function updateCVaR(cvar) {
  if (!cvar || !cvar.weights || !Object.keys(cvar.weights).length) return;
  $('cvar-section').style.display = '';
  const row = $('cvar-row');
  row.innerHTML = Object.entries(cvar.weights).map(([sym, w]) => {
    const pct = (w * 100).toFixed(0);
    const col = CVAR_COLORS[sym] || '#4d9fff';
    return `<div class="cvar-asset">
      <div class="cvar-sym">${sym.split('/')[0]}</div>
      <div class="cvar-pct" style="color:${col}">${pct}%</div>
      <div class="cvar-bar-wrap">
        <div class="cvar-bar" style="width:${pct}%;background:${col}"></div>
      </div>
    </div>`;
  }).join('');
  if (cvar.portfolio_cvar != null) {
    set('cvar-risk', `Portfolio CVaR 95%: ${fmt(cvar.portfolio_cvar,2)}%`);
  }
}

// ── Main update ──────────────────────────────────────────────────────────────
function update(d) {
  const acc  = d.account  || {};
  const pos  = d.positions || {};
  const ind  = d.indicators || {};
  const pri  = d.prices    || {};
  const sent = d.sentiment;
  const reg  = d.regime;
  const iv   = d.iv;
  const cvar = d.cvar;
  const pnl  = acc.total_pnl  || 0;
  const pnlP = acc.pnl_pct    || 0;
  const eq   = acc.total_equity;
  const wins = acc.winning_trades || 0;
  const tot  = acc.closed_trades  || 0;
  const wr   = tot > 0 ? wins/tot*100 : 0;

  // Header
  const mb = $('mode-badge');
  mb.textContent = (d.mode||'paper').toUpperCase();
  mb.className = 'badge ' + (d.mode==='live' ? 'badge-live' : 'badge-paper');
  const sb = $('status-badge');
  const running = d.status === 'running';
  $('pulse-dot').style.background = running ? 'var(--green)' : 'var(--red)';
  sb.className = 'badge ' + (running ? 'badge-run' : 'badge-stop');
  sb.innerHTML = `<span class="pulse" id="pulse-dot" style="background:${running?'var(--green)':'var(--red)'}"></span>${running?'LIVE':'OFFLINE'}`;

  // Equity
  set('equity',  `<span class="${glow(pnl)}">${fmtUSD(eq)}</span>`);
  set('cash',    'Cash: ' + fmtUSD(acc.cash));
  set('pnl',     `<span class="${clr(pnl)} ${glow(pnl)}">${pnl>=0?'+':''}${fmtUSD(pnl)}</span>`);
  set('pnl-pct', `<span class="${clr(pnlP)}">${pnlP>=0?'+':''}${fmtPct(pnlP)}</span>`);
  set('win-rate', `<span class="${clr(wr-50)}">${fmtPct(wr)}</span>`);
  set('trade-count', `${wins}W / ${tot-wins}L`);
  set('stat-open', Object.keys(pos).length);
  set('stat-wins', wins);
  set('stat-losses', tot-wins);

  // Prices
  const priceMap = {'BTC/USD':'p-btc','ETH/USD':'p-eth','SOL/USD':'p-sol'};
  Object.entries(priceMap).forEach(([sym, id]) => {
    if (pri[sym] != null) set(id, fmtK(pri[sym]));
  });

  // Signals
  const sigGrid = $('signal-grid');
  sigGrid.innerHTML = Object.entries(ind).map(([sym, info]) => {
    const sig = (info.signal||'HOLD').toUpperCase();
    const sigCls = sig==='BUY' ? 'sig-buy' : sig==='SELL' ? 'sig-sell' : 'sig-hold';
    return `<div class="sig-card">
      <div class="sig-sym">${sym.split('/')[0]}</div>
      <div><span class="sig-badge ${sigCls}">${sig}</span></div>
      <div class="sig-row">
        ${info.rsi!=null ? `<span class="tag">RSI ${fmt(info.rsi,0)}</span>` : ''}
        ${info.adx!=null ? `<span class="tag">ADX ${fmt(info.adx,0)}</span>` : ''}
        ${info.atr!=null ? `<span class="tag">ATR ${fmt(info.atr,1)}</span>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="no-data">Waiting...</div>';

  // Regime
  updateRegime(reg);
  updateSentiment(sent);
  updateIV(iv);
  updateCVaR(cvar);

  // Open positions
  const posWrap = $('positions-wrap');
  if (!Object.keys(pos).length) {
    posWrap.innerHTML = '<div class="no-data">No open positions</div>';
  } else {
    posWrap.innerHTML = Object.entries(pos).map(([sym, p]) => {
      const up = p.unrealized_pnl || 0;
      const col = up >= 0 ? 'var(--green)' : 'var(--red)';
      return `<div class="pos-card" style="border-left-color:${col}">
        <div class="pos-header">
          <div class="pos-sym">${sym}</div>
          <div class="pos-pnl" style="color:${col}">${up>=0?'+':''}${fmtUSD(up)}</div>
        </div>
        <div class="pos-details">
          <div class="pos-detail">Entry <span>${fmtUSD(p.entry_price)}</span></div>
          <div class="pos-detail">Size <span>${fmt(p.size,6)}</span></div>
          <div class="pos-detail">Current <span>${fmtUSD(pri[sym]||p.entry_price)}</span></div>
          <div class="pos-detail">Since <span>${p.entry_time ? new Date(p.entry_time).toLocaleTimeString() : '--'}</span></div>
        </div>
      </div>`;
    }).join('');
  }

  // Equity chart
  updateChart(d.equity_curve);

  // Recent trades
  const tradesWrap = $('trades-wrap');
  const trades = (d.recent_trades || []).slice(-20).reverse();
  if (!trades.length) {
    tradesWrap.innerHTML = '<div class="no-data">No trades yet</div>';
  } else {
    tradesWrap.innerHTML = trades.map(t => {
      const pv = t.pnl || 0;
      const won = pv >= 0;
      return `<div class="trade-row">
        <div class="trade-left">
          <div class="trade-sym">${t.symbol||'--'}</div>
          <div class="trade-time">${t.exit_time ? new Date(t.exit_time).toLocaleString([], {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '--'}</div>
        </div>
        <div class="trade-right">
          <div class="trade-pnl ${clr(pv)}">${pv>=0?'+':''}${fmtUSD(pv)}</div>
          <div><span class="win-badge ${won?'win':'lose'}">${won?'WIN':'LOSS'}</span> <span class="trade-rsn">${t.reason||''}</span></div>
        </div>
      </div>`;
    }).join('');
  }

  // Funding rates
  const fundWrap = $('funding-wrap');
  const funding = d.funding_opportunities || [];
  if (!funding.length) {
    fundWrap.innerHTML = '<div class="no-data">Scanning for opportunities...</div>';
  } else {
    fundWrap.innerHTML = funding.slice(0,8).map(f => {
      const apy = f.apy || 0;
      return `<div class="fund-row">
        <div><b>${f.symbol}</b> <span style="color:var(--dim);font-size:10px">${f.exchange}</span></div>
        <div style="text-align:right">
          <div class="${clr(f.rate_8h)}" style="font-size:12px">${f.rate_8h>=0?'+':''}${fmt(f.rate_8h,4)}% 8h</div>
          <div style="font-size:10px;color:var(--dim)">${fmt(apy,1)}% APY</div>
        </div>
      </div>`;
    }).join('');
  }

  // Footer
  set('footer', d.last_update
    ? 'Updated ' + new Date(d.last_update).toLocaleTimeString()
    : 'Connecting...');
}

// ── Server-Sent Events ───────────────────────────────────────────────────────
function connect() {
  const es = new EventSource('/stream');
  es.onmessage = e => {
    try { update(JSON.parse(e.data)); }
    catch(err) { console.warn('Parse error:', err); }
  };
  es.onerror = () => {
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
</script>
</body>
</html>"""


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _auth_ok(request):
        return HTMLResponse(_login_page(), status_code=200)
    return HTML


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    token = form.get('token', '')
    if token == _TOKEN:
        resp = RedirectResponse('/', status_code=302)
        resp.set_cookie('dash_token', token, max_age=86400 * 30, httponly=True)
        return resp
    return HTMLResponse(_login_page(error=True), status_code=200)


def _login_page(error: bool = False) -> str:
    err = '<p style="color:#ff4d6d;margin-top:8px">Invalid token</p>' if error else ''
    return f"""<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRYPTO BOT</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#060a12;color:#cdd9e5;font-family:monospace;
     display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:#0d1421;border:1px solid #1e2d3d;border-radius:16px;padding:32px;
      width:90%;max-width:320px;text-align:center}}
h1{{color:#00e5ff;font-size:18px;letter-spacing:3px;margin-bottom:24px}}
input{{width:100%;padding:12px;background:#111827;border:1px solid #1e2d3d;
       border-radius:8px;color:#cdd9e5;font-family:monospace;font-size:14px;
       margin-bottom:12px;outline:none}}
button{{width:100%;padding:12px;background:#1e3a5f;border:1px solid #4d9fff;
        border-radius:8px;color:#4d9fff;font-family:monospace;font-size:14px;
        cursor:pointer;font-weight:700;letter-spacing:1px}}
</style></head><body>
<div class="box">
  <h1>&#9670; CRYPTOBOT</h1>
  <form method="post" action="/login">
    <input type="password" name="token" placeholder="Access token" autofocus>
    <button type="submit">ENTER</button>
    {err}
  </form>
</div></body></html>"""


@app.get("/api/state")
async def state_endpoint(request: Request):
    if not _auth_ok(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse(read_state())


@app.get("/stream")
async def stream(request: Request):
    if not _auth_ok(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    """Server-Sent Events — pushes state to browser every 2 seconds."""
    async def generate():
        while True:
            try:
                data = read_state()
                yield f"data: {json.dumps(data, default=str)}\n\n"
            except Exception:
                yield "data: {}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        }
    )


async def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
