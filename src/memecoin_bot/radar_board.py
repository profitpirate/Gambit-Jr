from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>
<title>Gambit Jr Live Intelligence</title><style>
:root{color-scheme:dark;background:#07111f;color:#e5edf8;font:14px system-ui}body{margin:0;padding:24px}h1{letter-spacing:.16em}.sub{color:#69d5ff}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,table{background:#0d1b2d;border:1px solid #213650;border-radius:10px}.card{padding:14px}table{width:100%;border-collapse:collapse;margin-top:20px;overflow:hidden}th,td{padding:10px;border-bottom:1px solid #1c3049;text-align:left}th{color:#85a5c8}.hot{color:#ffb84d}.risk{color:#ff6b7a}a{color:#69d5ff}@media(max-width:700px){body{padding:12px}.wide{overflow:auto}}
</style></head><body><h1>GAMBIT JR</h1><h2 class=sub>LIVE INTELLIGENCE</h2><div id=stats class=grid></div><div class=wide><table><thead><tr><th>Token</th><th>Chain</th><th>State</th><th>Radar MC</th><th>Current MC</th><th>Peak X</th><th>Liquidity</th><th>Radar</th><th>Confidence</th><th>Age</th></tr></thead><tbody id=rows></tbody></table></div><script>
const money=v=>v==null?'UNKNOWN':'$'+Intl.NumberFormat('en',{notation:'compact'}).format(v);const esc=s=>String(s??'UNKNOWN').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function load(){const address=new URLSearchParams(location.search).get('address');if(address){const d=await fetch('/api/token?address='+encodeURIComponent(address)).then(x=>x.json());document.querySelector('h2').textContent='TOKEN INTELLIGENCE';stats.innerHTML=[['Token',esc(d.name||d.symbol)],['Chain',esc(d.chain)],['State',esc(d.state)],['Radar Score',d.radar_score??'UNKNOWN'],['Confidence',d.confidence==null?'UNKNOWN':(d.confidence*100).toFixed(0)+'%']].map(x=>`<div class=card><small>${x[0]}</small><h3>${x[1]}</h3></div>`).join('');document.querySelector('.wide').innerHTML=`<div class=card><h2>${esc(d.name)} · ${esc(d.symbol)}</h2><p><b>Contract</b><br><code>${esc(d.token_address)}</code></p><div class=grid><div><small>Radar MC</small><h3>${money(d.radar_market_cap_usd)}</h3></div><div><small>Current MC</small><h3>${money(d.current_market_cap_usd)}</h3></div><div><small>Liquidity</small><h3>${money(d.current_liquidity_usd)}</h3></div><div><small>Peak X</small><h3>${d.max_multiple?.toFixed(2)??'UNKNOWN'}</h3></div></div><h3>GMGN / WALLET INTELLIGENCE</h3><pre>${esc(JSON.stringify(d.wallet_intelligence||{status:'UNKNOWN'},null,2))}</pre><h3>TIMELINE</h3><pre>${esc(JSON.stringify(d.timeline||[],null,2))}</pre><h3>MARKET CAP / LIQUIDITY HISTORY</h3><pre>${esc(JSON.stringify(d.snapshots||[],null,2))}</pre></div>`;return}const [s,r]=await Promise.all([fetch('/api/status').then(x=>x.json()),fetch('/api/radar').then(x=>x.json())]);const g=(s.provider_status||[]).find(x=>x.provider==='gmgn');stats.innerHTML=[['Scanner','ONLINE'],['GMGN',g?.state||'OFFLINE'],['Active Radar',s.early_radar],['Signals',s.signals],['Reconciled',s.state_reconciliation?.difference===0?'YES':'NO']].map(x=>`<div class=card><small>${x[0]}</small><h3>${x[1]}</h3></div>`).join('');rows.innerHTML=r.map(x=>`<tr><td><a href='/token.html?address=${encodeURIComponent(x.token_address)}'>${esc(x.name||x.symbol)}</a><br><small>${esc(x.symbol)}</small></td><td>${esc(x.chain)}</td><td>${esc(x.state)}</td><td>${money(x.radar_market_cap_usd)}</td><td>${money(x.current_market_cap_usd)}</td><td>${x.max_multiple?.toFixed(2)??'—'}</td><td>${money(x.current_liquidity_usd)}</td><td>${x.radar_score?.toFixed(1)??'—'}</td><td>${x.confidence==null?'—':(x.confidence*100).toFixed(0)+'%'}</td><td>${esc(x.updated_at)}</td></tr>`).join('')}load();setInterval(load,15000)</script></body></html>"""


def start_radar_board(port: int, store: object, started_at: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path)
            if path.path in {"/", "/index.html", "/token.html"}:
                body, content_type = HTML.encode(), "text/html; charset=utf-8"
            elif path.path == "/api/status":
                body, content_type = (
                    json.dumps(store.status_stats(started_at), default=str).encode(),
                    "application/json",
                )
            elif path.path == "/api/radar":
                body, content_type = (
                    json.dumps(store.radar_board(), default=str).encode(),
                    "application/json",
                )
            elif path.path == "/api/token":
                address = parse_qs(path.query).get("address", [""])[0]
                data = store.token_intelligence(address)
                body, content_type = json.dumps(data, default=str).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="radar-board", daemon=True).start()
    return server
