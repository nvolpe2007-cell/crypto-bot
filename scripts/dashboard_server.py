#!/usr/bin/env python3
"""Live web dashboard for the crypto-bot — dependency-free (stdlib only).

Serves one auto-refreshing HTML page showing every paper arm's equity, P&L,
trades, open positions and a lightweight proof-status, plus the cross-arm
attribution ledger and portfolio totals. Reads the same ``data/*_state.json``
files and ``data/attribution.db`` the arms already write — read-only, safe to
run alongside the live bot.

Run on the VPS:
    python scripts/dashboard_server.py --host 0.0.0.0 --port 8787
then open http://<vps-ip>:8787/  (bind 127.0.0.1 + SSH tunnel for private view).

Endpoints:
    /            HTML dashboard (auto-refreshes via /api/state)
    /api/state   JSON snapshot (what the page polls)
    /healthz     200 OK
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dashboard_data import snapshot  # noqa: E402

DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR")  # default → repo data/ inside snapshot()
REFRESH_SEC = int(os.environ.get("DASHBOARD_REFRESH_SEC", "10"))

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>crypto-bot live</title>
<style>
 :root{color-scheme:dark}
 body{background:#0d1117;color:#c9d1d9;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:18px}
 h1{font-size:18px;margin:0 0 4px}
 .sub{color:#8b949e;font-size:12px;margin-bottom:16px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;min-width:150px}
 .card .k{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
 .card .v{font-size:22px;font-weight:600;margin-top:2px}
 table{border-collapse:collapse;width:100%;margin-bottom:22px}
 th,td{text-align:right;padding:7px 10px;border-bottom:1px solid #21262d}
 th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #30363d}
 td:first-child,th:first-child{text-align:left}
 tr:hover td{background:#161b22}
 .pos{color:#3fb950}.neg{color:#f85149}.muted{color:#8b949e}
 .pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#21262d}
 .pill.promo{background:#16331f;color:#3fb950}.pill.bad{background:#3d1518;color:#f85149}
 .pill.idle{background:#21262d;color:#8b949e}
 h2{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}
</style></head><body>
<h1>🤖 crypto-bot — live</h1>
<div class="sub" id="ts">loading…</div>
<div class="cards" id="cards"></div>
<h2>Strategy arms</h2>
<table id="arms"><thead><tr>
 <th>arm</th><th>equity</th><th>P&L</th><th>%</th><th>trades</th><th>win%</th><th>open</th><th>status</th>
</tr></thead><tbody></tbody></table>
<h2>Attribution ledger (executed fills)</h2>
<table id="attrib"><thead><tr>
 <th>arm</th><th>fills</th><th>gross</th><th>fees</th><th>slippage</th><th>net</th>
</tr></thead><tbody></tbody></table>
<h2>Switch readiness — when does the allocator fund an arm?</h2>
<div class="sub">An arm gets capital only when it has <b>≥30 closed trades</b> (enough to trust),
 a <b>positive average</b>, and a t-stat over the family bar. "needs N more trades" = how
 far from the 30-trade floor. Negative arms can't be fixed by waiting.</div>
<table id="ready"><thead><tr>
 <th>arm</th><th>trades (n)</th><th>need</th><th>avg/trade</th><th>t-stat</th><th>bar</th><th>status</th><th>ETA</th>
</tr></thead><tbody></tbody></table>
<h2 id="tourhdr">Strategy tournament</h2>
<div class="sub" id="toursub"></div>
<table id="tour"><thead><tr>
 <th>strategy</th><th>family</th><th>sharpe</th><th>t</th><th>ret%</th><th>maxDD%</th><th>trades</th><th>verdict</th>
</tr></thead><tbody></tbody></table>
<script>
const REFRESH=%REFRESH%*1000;
const money=x=>(x<0?'-$':'$')+Math.abs(x).toFixed(2);
const cls=x=>x>0?'pos':x<0?'neg':'muted';
function pill(s){let c='idle';if(/PROMISING/.test(s))c='promo';else if(/LOSING/.test(s))c='bad';
 return '<span class="pill '+c+'">'+s+'</span>';}
async function tick(){
 let d;try{d=await(await fetch('/api/state')).json()}catch(e){document.getElementById('ts').textContent='fetch error';return}
 const t=d.totals;
 document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString()+' · '+t.n_arms+' arms · '+t.active+' with open positions';
 document.getElementById('cards').innerHTML=
  card('Total equity',money(t.equity))+
  card('Net P&L','<span class="'+cls(t.pnl)+'">'+money(t.pnl)+'</span>')+
  card('Return','<span class="'+cls(t.pnl)+'">'+t.pnl_pct.toFixed(2)+'%</span>')+
  card('Arms',t.n_arms+' ('+t.active+' active)');
 document.querySelector('#arms tbody').innerHTML=d.arms.map(a=>
  '<tr><td>'+a.name+'</td><td>'+money(a.equity)+'</td>'+
  '<td class="'+cls(a.pnl)+'">'+money(a.pnl)+'</td>'+
  '<td class="'+cls(a.pnl)+'">'+a.pnl_pct.toFixed(2)+'%</td>'+
  '<td>'+a.trades+'</td><td>'+(a.trades?a.win_rate.toFixed(0)+'%':'—')+'</td>'+
  '<td>'+(a.open||'—')+'</td><td style="text-align:left">'+pill(a.status)+'</td></tr>').join('');
 document.querySelector('#attrib tbody').innerHTML=d.attribution.length?d.attribution.map(a=>
  '<tr><td>'+a.arm+'</td><td>'+a.fills+'</td><td>'+money(a.gross)+'</td>'+
  '<td class="neg">'+money(a.fees)+'</td><td class="neg">'+money(a.slippage)+'</td>'+
  '<td class="'+cls(a.net)+'">'+money(a.net)+'</td></tr>').join(''):
  '<tr><td colspan=6 class="muted">no executed fills logged yet</td></tr>';
 // switch readiness
 const rd=d.readiness||[];
 document.querySelector('#ready tbody').innerHTML=rd.length?rd.map(r=>{
  let cl=r.proven?'pos':r.positive?'':'neg';
  let pill=r.proven?'<span class="pill promo">PROVEN</span>':
    r.positive?'<span class="pill">'+r.status+'</span>':
    '<span class="pill bad">'+r.status+'</span>';
  let eta=r.eta_days!=null?('~'+(r.eta_days>=14?Math.round(r.eta_days/7)+' wk':r.eta_days+' d')):
    (r.proven?'now':'—');
  let lo=r.executable?'':' <span class="pill" style="background:#3d2a15;color:#e0a458">perp</span>';
  return '<tr><td>'+r.name+lo+'</td><td>'+r.n+'</td>'+
   '<td>'+(r.need_more>0?'+'+r.need_more:'✓')+'</td>'+
   '<td class="'+cls(r.expectancy)+'">'+(r.expectancy>=0?'+':'')+r.expectancy.toFixed(2)+'</td>'+
   '<td class="'+cls(r.t_clustered)+'">'+r.t_clustered.toFixed(2)+'</td>'+
   '<td class="muted">'+r.t_bar.toFixed(2)+'</td>'+
   '<td style="text-align:left">'+pill+'</td><td>'+eta+'</td></tr>';}).join(''):
  '<tr><td colspan=8 class="muted">allocator has not run yet — run scripts/run_allocator.py</td></tr>';
 // tournament
 const tr=d.tournament||{},s=tr.summary||{};
 if(tr.candidates&&tr.candidates.length){
  const age=tr.generated_at?Math.round((Date.now()/1000-tr.generated_at)/3600)+'h ago':'';
  document.getElementById('toursub').innerHTML=
   s.n_candidates+' candidates · Šidák |t| bar <b>'+s.family_t_bar+'</b> · '+
   '<b>'+s.n_robust+'</b> robust · <b class="'+(s.n_passes_family>0?'pos':'muted')+'">'+
   s.n_passes_family+'</b> clear family-bar ('+s.n_long_only_executable+' long-only executable) · '+
   (tr.coins||[]).join(' ')+' '+(tr.n_bars||'')+'d · '+age;
  document.querySelector('#tour tbody').innerHTML=tr.candidates.map(r=>{
   let v='<span class="pill idle">in-sample</span>';
   if(r.passes_family)v='<span class="pill promo">CLEARS FAMILY-BAR</span>';
   else if(r.robust)v='<span class="pill">robust</span>';
   const lo=r.long_only_ok?' <span class="pill" style="background:#1b2838">LO</span>':'';
   return '<tr><td>'+r.name+lo+'</td><td>'+r.family+'</td>'+
    '<td class="'+cls(r.sharpe)+'">'+r.sharpe.toFixed(2)+'</td>'+
    '<td>'+r.t_stat.toFixed(1)+'</td>'+
    '<td class="'+cls(r.ret_pct)+'">'+r.ret_pct.toFixed(1)+'</td>'+
    '<td class="neg">'+r.mdd_pct.toFixed(1)+'</td><td>'+r.trades+'</td>'+
    '<td style="text-align:left">'+v+'</td></tr>';}).join('');
 }else{
  document.getElementById('toursub').innerHTML='<span class="muted">tournament not run yet — '+
   'run <code>python scripts/run_tournament.py</code></span>';
 }
}
function card(k,v){return '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>'}
tick();setInterval(tick,REFRESH);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, _PAGE.replace("%REFRESH%", str(REFRESH_SEC)).encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                body = json.dumps(snapshot(DATA_DIR)).encode()
            except Exception as exc:  # never 500 the page over a transient read
                body = json.dumps({"error": str(exc), "arms": [], "attribution": [],
                                   "totals": {"equity": 0, "start": 0, "pnl": 0, "pnl_pct": 0,
                                              "n_arms": 0, "active": 0}}).encode()
            self._send(200, body, "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:  # quiet — don't spam the journal
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="crypto-bot live web dashboard")
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8787")))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard on http://{args.host}:{args.port}/  (refresh {REFRESH_SEC}s)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
