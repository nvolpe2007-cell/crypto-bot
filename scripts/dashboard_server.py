#!/usr/bin/env python3
"""Live web dashboard — lev_perp focused, real-time, dark mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR    = os.environ.get("DASHBOARD_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
REFRESH_SEC = int(os.environ.get("DASHBOARD_REFRESH_SEC", "10"))

_PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ crypto-bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b0f;--surface:#0d1117;--card:#111820;--border:#1e2733;--border2:#2a3545;
  --text:#e6edf3;--muted:#7d8fa3;--dim:#4a5568;
  --green:#00d084;--green-dim:#003d25;--green-glow:rgba(0,208,132,.15);
  --red:#ff4757;--red-dim:#3d0012;--red-glow:rgba(255,71,87,.15);
  --blue:#4da6ff;--blue-dim:#001a3d;
  --yellow:#ffd166;--purple:#c084fc;
  --gold:#f59e0b;
}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;line-height:1.5;min-height:100vh}
.mono{font-family:'JetBrains Mono',monospace}

/* Layout */
.layout{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
.sidebar{background:var(--surface);border-right:1px solid var(--border);padding:24px 16px;display:flex;flex-direction:column;gap:20px;position:sticky;top:0;height:100vh;overflow-y:auto}
.main{padding:24px;display:flex;flex-direction:column;gap:20px}

/* Sidebar */
.logo{display:flex;align-items:center;gap:10px;padding-bottom:16px;border-bottom:1px solid var(--border)}
.logo-icon{width:36px;height:36px;background:linear-gradient(135deg,var(--green),#00a86b);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.logo-text{font-weight:700;font-size:15px;line-height:1.2}
.logo-sub{color:var(--muted);font-size:11px}
.sb-stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}
.sb-stat-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.sb-stat-val{font-size:20px;font-weight:700;font-family:'JetBrains Mono',monospace}
.sb-stat-sub{color:var(--muted);font-size:11px;margin-top:2px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 var(--green-glow);animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(0,208,132,.4)}70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
.status-row{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.nav-label{color:var(--dim);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;padding:0 2px;margin-bottom:4px}

/* Metric cards */
.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}
.metric-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;position:relative;overflow:hidden;transition:border-color .2s}
.metric-card:hover{border-color:var(--border2)}
.mc-label{color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.mc-val{font-size:24px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1}
.mc-sub{color:var(--muted);font-size:11px;margin-top:4px}
.mc-icon{position:absolute;right:14px;top:14px;font-size:20px;opacity:.4}

/* Section headers */
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.section-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)}
.badge{padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;background:var(--border);color:var(--muted)}

/* Positions */
.positions-grid{display:flex;flex-direction:column;gap:8px}
.pos-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;display:grid;grid-template-columns:140px 1fr 1fr 1fr 120px;gap:16px;align-items:center;transition:border-color .15s}
.pos-card:hover{border-color:var(--border2)}
.pos-card.short{border-left:3px solid var(--red)}
.pos-card.long{border-left:3px solid var(--green)}
.pos-symbol{font-size:20px;font-weight:700;display:flex;flex-direction:column;gap:4px}
.pos-side{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;width:fit-content}
.pos-side.short{background:var(--red-dim);color:var(--red)}
.pos-side.long{background:var(--green-dim);color:var(--green)}
.pos-field{display:flex;flex-direction:column;gap:2px}
.pos-field-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.pos-field-val{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600}
.progress-bar-wrap{background:var(--border);border-radius:4px;height:4px;overflow:hidden;margin-top:6px}
.progress-bar{height:100%;border-radius:4px;transition:width .5s}
.progress-bar.green{background:var(--green)}
.progress-bar.red{background:var(--red)}

/* Trade history */
.trade-list{display:flex;flex-direction:column;gap:6px;max-height:340px;overflow-y:auto;padding-right:4px}
.trade-list::-webkit-scrollbar{width:4px}
.trade-list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.trade-row{display:grid;grid-template-columns:90px 70px 90px 90px 1fr 90px;gap:8px;align-items:center;padding:10px 12px;background:var(--card);border:1px solid var(--border);border-radius:8px;font-size:12px;transition:border-color .15s}
.trade-row:hover{border-color:var(--border2)}
.reason-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600}
.reason-badge.take_profit{background:#00301e;color:var(--green)}
.reason-badge.flip{background:#001530;color:var(--blue)}
.reason-badge.liquidation{background:var(--red-dim);color:var(--red)}

/* Filter cards */
.filter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}
.filter-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;display:flex;align-items:center;gap:10px;transition:border-color .15s}
.filter-card.pass{border-color:rgba(0,208,132,.3)}
.filter-card.fail{border-color:rgba(255,71,87,.2)}
.filter-icon{font-size:16px;flex-shrink:0}
.filter-name{font-size:12px;font-weight:600;margin-bottom:1px}
.filter-val{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace}

/* Colors */
.green{color:var(--green)}.red{color:var(--red)}.muted{color:var(--muted)}.gold{color:var(--gold)}.blue{color:var(--blue)}
.ts{color:var(--dim);font-size:11px;font-family:'JetBrains Mono',monospace}
.empty{text-align:center;color:var(--muted);padding:32px;font-size:13px}

/* scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
</style>
</head>
<body>
<div class="layout">

<aside class="sidebar">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div><div class="logo-text">crypto-bot</div><div class="logo-sub">lev perp · 3x · SMA-50</div></div>
  </div>

  <div>
    <div class="nav-label" style="margin-bottom:8px">Account</div>
    <div class="sb-stat">
      <div class="sb-stat-label">Total Equity</div>
      <div class="sb-stat-val" id="sb-eq-val">—</div>
      <div class="sb-stat-sub" id="sb-eq-sub">loading…</div>
    </div>
    <div class="sb-stat" style="margin-top:8px">
      <div class="sb-stat-label">Win Rate</div>
      <div class="sb-stat-val" id="sb-wr">—</div>
      <div class="sb-stat-sub" id="sb-wr-sub">—</div>
    </div>
  </div>

  <div>
    <div class="nav-label" style="margin-bottom:8px">Live Status</div>
    <div class="status-row"><div class="pulse" id="live-dot"></div><span id="live-status">connecting…</span></div>
    <div class="ts" id="last-update" style="margin-top:6px">—</div>
  </div>

  <div style="margin-top:auto">
    <div class="nav-label" style="margin-bottom:8px">Filters</div>
    <div id="sb-filters" style="display:flex;flex-direction:column;gap:6px;font-size:12px"></div>
  </div>
</aside>

<main class="main">

  <div class="metric-grid" id="metric-grid"></div>

  <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px">
    <div class="section-header">
      <span class="section-title">Equity Curve</span>
      <span class="badge" id="chart-badge">—</span>
    </div>
    <canvas id="equity-chart" style="width:100%;height:180px"></canvas>
  </div>

  <div>
    <div class="section-header">
      <span class="section-title">Open Positions</span>
      <span class="badge" id="pos-count">0 open</span>
    </div>
    <div class="positions-grid" id="positions-grid"><div class="empty">No open positions</div></div>
  </div>

  <div>
    <div class="section-header">
      <span class="section-title">Entry Filters</span>
      <span class="badge" id="filter-badge">—</span>
    </div>
    <div class="filter-grid" id="filter-grid"></div>
  </div>

  <div>
    <div class="section-header">
      <span class="section-title">Trade History</span>
      <span class="badge" id="trade-badge">—</span>
    </div>
    <div class="trade-list" id="trade-list"><div class="empty">No closed trades yet</div></div>
  </div>

</main>
</div>

<script>
const REFRESH = %REFRESH% * 1000;
const $ = id => document.getElementById(id);
const cls = v => v > 0 ? 'green' : v < 0 ? 'red' : 'muted';
const fmt$ = (v, d=2) => (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(d);
const fmtPct = v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';

function drawChart(curve) {
  const canvas = $('equity-chart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.parentElement.clientWidth - 32, H = 180;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (!curve || curve.length < 2) {
    ctx.fillStyle = '#4a5568'; ctx.font = '13px Inter'; ctx.textAlign = 'center';
    ctx.fillText('Not enough data yet', W/2, H/2); return;
  }
  const vals = curve.map(p => typeof p === 'object' ? (p.equity_mtm || p.realized || p.equity || 0) : p);
  const start = vals[0], last = vals[vals.length - 1];
  const min = Math.min(...vals) * 0.997, max = Math.max(...vals) * 1.003;
  const range = max - min || 1;
  const pad = { t:16, r:12, b:24, l:52 };
  const cW = W - pad.l - pad.r, cH = H - pad.t - pad.b;
  const x = i => pad.l + (i / (vals.length - 1)) * cW;
  const y = v => pad.t + (1 - (v - min) / range) * cH;
  const profit = last >= start;
  const color = profit ? '#00d084' : '#ff4757';

  // grid
  ctx.strokeStyle = '#1e2733'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const yv = pad.t + i / 4 * cH;
    ctx.beginPath(); ctx.moveTo(pad.l, yv); ctx.lineTo(pad.l + cW, yv); ctx.stroke();
    ctx.fillStyle = '#4a5568'; ctx.font = '10px JetBrains Mono'; ctx.textAlign = 'right';
    ctx.fillText('$' + (max - i / 4 * range).toFixed(0), pad.l - 4, yv + 4);
  }

  // baseline
  ctx.strokeStyle = '#2a3545'; ctx.setLineDash([4,4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, y(start)); ctx.lineTo(pad.l + cW, y(start)); ctx.stroke();
  ctx.setLineDash([]);

  // fill
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + cH);
  grad.addColorStop(0, profit ? 'rgba(0,208,132,.3)' : 'rgba(255,71,87,.3)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  vals.forEach((v, i) => i > 0 && ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(vals.length-1), pad.t + cH); ctx.lineTo(x(0), pad.t + cH);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  // line
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  vals.forEach((v, i) => i > 0 && ctx.lineTo(x(i), y(v)));
  ctx.stroke();

  // dot
  ctx.beginPath(); ctx.arc(x(vals.length-1), y(last), 4, 0, Math.PI*2);
  ctx.fillStyle = color; ctx.fill();
}

async function tick() {
  let d;
  try {
    d = await (await fetch('/api/state')).json();
    $('live-dot').style.background = 'var(--green)';
  } catch {
    $('live-status').textContent = 'connection error';
    $('live-dot').style.background = 'var(--red)';
    return;
  }

  const lev = d.lev_perp || {};
  const eq = lev.equity || 1000;
  const start = lev.starting_equity || 1000;
  const pnlAbs = eq - start;
  const pnlPct = pnlAbs / start * 100;
  const closed = lev.closed || [];
  const positions = lev.positions || {};
  const prices = lev.prices || {};
  const filters = lev.filters || {};
  const n = closed.length;
  const wins = closed.filter(t => t.pnl > 0).length;
  const wr = n ? wins / n * 100 : 0;
  const tps = closed.filter(t => t.reason === 'take_profit').length;
  const liqs = closed.filter(t => t.reason === 'liquidation').length;

  // sidebar
  $('sb-eq-val').textContent = '$' + eq.toFixed(2);
  $('sb-eq-val').className = 'sb-stat-val ' + cls(pnlAbs);
  $('sb-eq-sub').innerHTML = '<span class="' + cls(pnlAbs) + '">' + (pnlAbs>=0?'+':'') + fmt$(pnlAbs) + ' (' + fmtPct(pnlPct) + ')</span>';
  $('sb-wr').textContent = n ? wr.toFixed(0) + '%' : '—';
  $('sb-wr').className = 'sb-stat-val ' + (wr >= 60 ? 'green' : wr >= 40 ? 'gold' : wr > 0 ? 'red' : 'muted');
  $('sb-wr-sub').textContent = n + ' trades · ' + tps + ' TP · ' + liqs + ' liq';
  $('live-status').textContent = 'live · ' + Object.keys(positions).length + ' open';
  $('last-update').textContent = new Date().toLocaleTimeString();

  // sidebar filter summary
  const sfItems = [
    {k:'RSI', ok:filters.rsi_ok, v:filters.rsi != null ? 'RSI '+filters.rsi.toFixed(1) : '—'},
    {k:'Age', ok:filters.age_ok, v:filters.age != null ? filters.age+'d' : '—'},
    {k:'Vol', ok:filters.vol_ok, v:filters.vol_ratio != null ? filters.vol_ratio.toFixed(2)+'x' : '—'},
    {k:'ADX', ok:filters.adx_ok, v:filters.adx != null ? 'ADX '+filters.adx.toFixed(1) : '—'},
  ];
  $('sb-filters').innerHTML = sfItems.map(f =>
    '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">' +
    '<span style="color:' + (f.ok ? 'var(--green)' : 'var(--red)') + ';font-weight:600">' + (f.ok ? '✓ ' : '✗ ') + f.k + '</span>' +
    '<span class="mono muted" style="font-size:10px">' + f.v + '</span></div>'
  ).join('');

  // unrealized
  const posKeys = Object.keys(positions);
  let unreal = 0;
  posKeys.forEach(base => {
    const p = positions[base];
    const px = prices[base] || p.entry;
    unreal += p.notional_usd * p.side * (px - p.entry) / p.entry;
  });

  // metric cards
  const metrics = [
    {label:'Equity', val: '$'+eq.toFixed(2), sub:'realized P&L base', icon:'💰', color: cls(pnlAbs)},
    {label:'Total P&L', val: (pnlAbs>=0?'+':'')+fmt$(pnlAbs), sub: fmtPct(pnlPct)+' vs start', icon:'📈', color: cls(pnlAbs)},
    {label:'Unrealized', val: (unreal>=0?'+':'')+fmt$(unreal), sub: posKeys.length+' positions', icon:'📊', color: cls(unreal)},
    {label:'Mark Value', val: '$'+(eq+unreal).toFixed(2), sub:'equity + unrealized', icon:'⚡', color:'muted'},
    {label:'Win Rate', val: n ? wr.toFixed(0)+'%' : '—', sub: wins+'W / '+(n-wins)+'L', icon:'🎯', color: wr>=50?'green':wr>0?'gold':'muted'},
    {label:'Trades', val: ''+n, sub: tps+' TP · '+liqs+' liq', icon:'🔢', color:'muted'},
  ];
  $('metric-grid').innerHTML = metrics.map(m =>
    '<div class="metric-card"><div class="mc-icon">'+m.icon+'</div>' +
    '<div class="mc-label">'+m.label+'</div>' +
    '<div class="mc-val '+m.color+'">'+m.val+'</div>' +
    '<div class="mc-sub">'+m.sub+'</div></div>'
  ).join('');

  // chart
  drawChart(lev.equity_curve || []);
  $('chart-badge').textContent = n + ' closed trades';

  // positions
  $('pos-count').textContent = posKeys.length + ' open';
  if (!posKeys.length) {
    $('positions-grid').innerHTML = '<div class="empty">No open positions — filters are active</div>';
  } else {
    $('positions-grid').innerHTML = posKeys.map(base => {
      const p = positions[base];
      const px = prices[base] || p.entry;
      const r = p.side * (px - p.entry) / p.entry;
      const unr = p.notional_usd * r;
      const sd = p.side > 0 ? 'long' : 'short';
      const tp = p.tp, liq = p.liq;
      const pctToTp = Math.min(Math.max(Math.abs(px - p.entry) / Math.abs(tp - p.entry) * (r >= 0 ? 100 : 0), 0), 100);
      return '<div class="pos-card '+sd+'">' +
        '<div class="pos-symbol">'+base+'<span class="pos-side '+sd+'">'+(p.side>0?'▲ LONG':'▼ SHORT')+' '+p.leverage+'x</span></div>' +
        '<div class="pos-field"><div class="pos-field-label">Entry</div><div class="pos-field-val">$'+p.entry.toLocaleString()+'</div></div>' +
        '<div class="pos-field"><div class="pos-field-label">Price</div><div class="pos-field-val '+cls(r)+'">$'+px.toLocaleString()+'</div>' +
          '<div style="font-size:10px;color:'+(r>=0?'var(--green)':'var(--red)')+'">'+fmtPct(r*100)+'</div></div>' +
        '<div class="pos-field"><div class="pos-field-label">Unrealized</div><div class="pos-field-val '+cls(unr)+'">'+(unr>=0?'+':'')+fmt$(unr)+'</div>' +
          '<div class="progress-bar-wrap"><div class="progress-bar '+(r>=0?'green':'red')+'" style="width:'+pctToTp.toFixed(0)+'%"></div></div></div>' +
        '<div class="pos-field"><div class="pos-field-label">TP / Liq</div>' +
          '<div style="font-size:11px;color:var(--green);font-family:monospace">▲ $'+tp.toLocaleString(undefined,{maximumFractionDigits:1})+'</div>' +
          '<div style="font-size:11px;color:var(--red);font-family:monospace">✕ $'+liq.toLocaleString(undefined,{maximumFractionDigits:1})+'</div></div>' +
        '</div>';
    }).join('');
  }

  // filters
  const fa = [
    {name:'RSI < 45', desc:'oversold at entry', ok:filters.rsi_ok, val:filters.rsi!=null?'RSI = '+filters.rsi.toFixed(1):'no data'},
    {name:'Trend Age ≥ 8d', desc:'established trend', ok:filters.age_ok, val:filters.age!=null?filters.age+'d in trend':'no data'},
    {name:'Volume > 1.2x', desc:'above 20d average', ok:filters.vol_ok, val:filters.vol_ratio!=null?filters.vol_ratio.toFixed(2)+'x avg':'no data'},
    {name:'ADX valid', desc:'skip 20-30 dead-zone', ok:filters.adx_ok, val:filters.adx!=null?'ADX '+filters.adx.toFixed(1):'no data'},
  ];
  $('filter-grid').innerHTML = fa.map(f =>
    '<div class="filter-card '+(f.ok?'pass':'fail')+'">' +
    '<div class="filter-icon">'+(f.ok?'✅':'🚫')+'</div>' +
    '<div><div class="filter-name '+(f.ok?'green':'red')+'">'+f.name+'</div>' +
    '<div class="filter-val">'+f.val+' · '+f.desc+'</div></div></div>'
  ).join('');
  const passing = fa.filter(f=>f.ok).length;
  $('filter-badge').textContent = passing + '/4 passing';
  $('filter-badge').style.color = passing===4?'var(--green)':passing>=2?'var(--yellow)':'var(--red)';

  // trades
  $('trade-badge').textContent = n + ' trades';
  if (!n) {
    $('trade-list').innerHTML = '<div class="empty">No closed trades yet</div>';
  } else {
    $('trade-list').innerHTML = [...closed].reverse().map(t => {
      const date = new Date((t.exit_ts || t.ts || 0) * 1000).toLocaleDateString('en-US',{month:'short',day:'numeric'});
      return '<div class="trade-row">' +
        '<div style="font-weight:600">'+t.symbol+' <span class="'+(t.side>0?'green':'red')+'">'+(t.side>0?'L':'S')+'</span></div>' +
        '<div><span class="reason-badge '+t.reason+'">' +
          (t.reason==='take_profit'?'TP ✓':t.reason==='liquidation'?'LIQ ✕':'FLIP')+'</span></div>' +
        '<div class="mono muted" style="font-size:11px">$'+t.entry.toLocaleString(undefined,{maximumFractionDigits:0})+'</div>' +
        '<div class="mono muted" style="font-size:11px">$'+t.exit.toLocaleString(undefined,{maximumFractionDigits:0})+'</div>' +
        '<div class="mono '+(t.pnl>=0?'green':'red')+'" style="font-weight:600">'+(t.pnl>=0?'+':'')+fmt$(t.pnl)+'</div>' +
        '<div class="muted" style="font-size:11px">'+date+'</div></div>';
    }).join('');
  }
}

tick();
setInterval(tick, REFRESH);
window.addEventListener('resize', () => {
  const d = fetch('/api/state').then(r=>r.json()).then(d=>drawChart((d.lev_perp||{}).equity_curve||[]));
});
</script>
</body></html>"""


def _load_state():
    """Read lev_perp_state.json and state.json, merge into a response dict."""
    out = {}
    lev_file = os.path.join(DATA_DIR, "lev_perp_state.json")
    if os.path.exists(lev_file):
        with open(lev_file) as f:
            lev = json.load(f)
        # inject current prices from state.json
        state_file = os.path.join(DATA_DIR, "state.json")
        if os.path.exists(state_file):
            with open(state_file) as f:
                st = json.load(f)
            raw = st.get("prices", {})
            lev["prices"] = {k.split("/")[0]: v for k, v in raw.items()}
        out["lev_perp"] = lev

    # try legacy dashboard_data module as fallback
    try:
        from src.dashboard_data import snapshot  # type: ignore
        base = snapshot(DATA_DIR)
        base.update(out)
        return base
    except Exception:
        pass

    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = _PAGE.replace("%REFRESH%", str(REFRESH_SEC)).encode()
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                body = json.dumps(_load_state()).encode()
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode()
            self._send(200, body, "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="crypto-bot live dashboard")
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8787")))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard → http://{args.host}:{args.port}/  (refresh {REFRESH_SEC}s)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
