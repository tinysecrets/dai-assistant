#!/usr/bin/env python3
"""dai headquarters — one browser tab over the running spine.

Stdlib only.  Serves a single HTML page that polls the three services' own
HTTP endpoints (router, agent-s-worker, voice-bridge) and shows models,
tasks, voice status and cooldowns in a split-screen layout.

    python3 lib/dai/dashboard.py                  # serve on :8799
    python3 lib/dai/dashboard.py --port 8080
    curl http://127.0.0.1:8799/health

Launched automatically by ``bin/start-spine.sh`` as the ``dai-dashboard``
service; ``bin/dai dashboard`` starts it on demand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.dai.env import env_int, env_str, load_dotenv  # noqa: E402

POLL_SECONDS = 3


def service_urls() -> dict[str, tuple[str, int]]:
    return {
        "router": (env_str("DAI_ROUTER_HOST", "127.0.0.1"), env_int("DAI_ROUTER_PORT", 11435)),
        "worker": (env_str("DAI_AGENT_S_HOST", "127.0.0.1"), env_int("DAI_AGENT_S_PORT", 8765)),
        "voice": (env_str("DAI_VOICE_BRIDGE_HOST", "127.0.0.1"), env_int("DAI_VOICE_BRIDGE_PORT", 8766)),
    }


def _get_json(url: str, timeout: float = 3.0) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def snapshot() -> Dict[str, Any]:
    """Poll every service once.  Never raises; a down service becomes None."""
    urls = service_urls()
    r_host, r_port = urls["router"]
    w_host, w_port = urls["worker"]
    v_host, v_port = urls["voice"]
    router = _get_json(f"http://{r_host}:{r_port}/health")
    worker = _get_json(f"http://{w_host}:{w_port}/health")
    voice = _get_json(f"http://{v_host}:{v_port}/health")
    models = _get_json(f"http://{r_host}:{r_port}/v1/models")
    tasks_raw = _get_json(f"http://{w_host}:{w_port}/v1/tasks")
    cooldowns_raw = _get_json(f"http://{r_host}:{r_port}/v1/status/cooldowns")
    models_data = (models or {}).get("data") if isinstance(models, dict) else None
    tasks_data = None
    tasks_meta = None
    if isinstance(tasks_raw, dict):
        tasks_data = tasks_raw.get("data")
        tasks_meta = {
            "count": tasks_raw.get("count"),
            "queue_depth": tasks_raw.get("queue_depth"),
            "running": tasks_raw.get("running"),
        }
    elif isinstance(tasks_raw, list):
        tasks_data = tasks_raw
    cooldowns_data = None
    if isinstance(cooldowns_raw, dict):
        inner = cooldowns_raw.get("cooldowns")
        cooldowns_data = inner if isinstance(inner, dict) else cooldowns_raw
    else:
        cooldowns_data = cooldowns_raw
    return {
        "ts": time.time(),
        "router": router,
        "worker": worker,
        "voice": voice,
        "models": models_data,
        "tasks": tasks_data,
        "tasks_meta": tasks_meta,
        "cooldowns": cooldowns_data,
        "cooldowns_meta": cooldowns_raw,
    }


class DashboardState:
    """Latest snapshot, refreshed by a background thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}

    def refresh(self) -> None:
        self._data = snapshot()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dai headquarters</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#070b12;color:#e8eef7;font-size:13px;line-height:1.45}header{background:rgba(9,14,22,.94);padding:15px 20px;font-weight:700;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #253248;position:sticky;top:0;z-index:2;backdrop-filter:blur(12px)}header .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px;background:#65e6a5;box-shadow:0 0 14px #65e6a5}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;border:0;padding:14px 14px 0}.panel{padding:16px;border:1px solid #253248;border-radius:14px;background:linear-gradient(145deg,#111925,#0b1119);box-shadow:0 14px 35px #0005;min-height:180px}.panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:#aebbd0;margin-bottom:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 5px;font-size:12px;border-bottom:1px;border-bottom:1px solid #25324899}th{color:#8190a8;font-size:10px;text-transform:uppercase}td{color:#d8e0ec}.pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:10px;font-weight:800}.up{background:#143a2a;color:#65e6a5}.down{background:#401c25;color:#ff7c89}.warn{background:#403416;color:#ffd166}.small{color:#8190a8;font-size:11px}.muted{color:#65748c}.panel:before{content:"";display:block;width:32px;height:2px;background:#58d8ff;margin-bottom:10px;border-radius:2px}@media(max-width:800px){.grid{grid-template-columns:1fr}.panel{min-height:auto}}</style>
</head>
<body>
<header><span class="dot" id="dot"></span><span id="title">dai headquarters</span>
<span class="small" id="ts"></span></header>
<div class="grid">
  <div class="panel">
    <h2>Services</h2>
    <table id="svc"><tbody></tbody></table>
  </div>
  <div class="panel">
    <h2>Models (router)</h2>
    <table id="models"><tbody></tbody></table>
  </div>
</div>
<div class="grid">
  <div class="panel">
    <h2>Tasks (worker)</h2>
    <table id="tasks"><tbody></tbody></table>
  </div>
  <div class="panel">
    <h2>Cooldowns (router)</h2>
    <table id="cd"><tbody></tbody></table>
  </div>
</div>
<script>
function esc(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function rows(o){var h="";for(var k in (o||{})){h+="<tr><th>"+esc(k)+"</th><td>"+esc((o||{})[k])+"</td></tr>";}return h;}
setInterval(function(){
  fetch('/api/snapshot').then(function(r){return r.json()}).then(function(d){
    var dot=document.getElementById('dot');
    var allOk = (d.router && d.router.ok !== false) &&
                (d.worker && d.worker.ok !== false) &&
                (!d.voice || d.voice.ok !== false);
    dot.style.background = allOk ? '#a6e3a9' : '#f38bae';
    document.getElementById('ts').textContent = new Date().toLocaleTimeString();
    var sv='';
    [['router',d.router],['agent-s-worker',d.worker],['voice-bridge',d.voice]].forEach(function(e){
      var name=e[0], o=e[1];
      var ok = o ? '<span class="pill up">up</span>' : '<span class="pill down">down</span>';
      sv += '<tr><td>'+esc(name)+'</td><td>'+ok+'</td><td>'+rows(o||{}).join('')+'</td></tr>';
    });
    document.querySelector('#svc tbody').innerHTML = sv;
    var mm=''; (d.models||[]).forEach(function(m){
      var tags = m.dai ? (m.dai.origin ? '<span class="pill warn">'+m.dai.origin+'</span> ' : '') : '';
      mm += '<tr><td>'+esc(m.id||m.name||'')+'</td><td>'+tags+esc(m.owned_by||'')+'</td></tr>';
    });
    document.querySelector('#models tbody').innerHTML = mm || '<tr><td class="muted">no data</td></tr>';
    var tl = d.tasks;
    var tt='';
    if (tl && tl.length) tl.forEach(function(t){
      tt += '<tr><td>'+esc(String(t.id||''))+'</td><td>'+esc(t.instruction||t.status||'')+
            '</td><td>'+(t.status||'')+'</td><td>'+(t.approved?'yes':'no')+'</td></tr>';
    }); else tt = '<tr><td class="muted">no tasks</td></tr>';
    document.querySelector('#tasks tbody').innerHTML = tt;
    var cc=''; var cds=d.cooldowns||{};
    Object.keys(cds).forEach(function(k){
      cc += '<tr><td>'+esc(k)+'</td><td>'+esc(cds[k])+'</td></tr>';
    });
    document.querySelector('#cd tbody').innerHTML = cc || '<tr><td class="muted">no cooldowns</td></tr>';
  }).catch(function(){});
}, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    state: DashboardState = DashboardState()

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
        elif self.path == "/health":
            data = self.state.get()
            ok = bool(data) and (data.get("router") is not None)
            body = json.dumps({"ok": ok, "ts": data.get("ts")}).encode()
            self._send(200 if ok else 503, body, "application/json")
        elif self.path == "/api/snapshot":
            self._send(200, json.dumps(self.state.get()).encode(), "application/json")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")


def main() -> None:
    load_dotenv(Path(os.environ.get("DAI_ENV", str(ROOT / ".env"))))
    global HTML
    ui=ROOT / "lib" / "dai" / "dashboard_ui.html"
    if ui.exists():
        HTML=ui.read_text(encoding="utf-8")
    ap = argparse.ArgumentParser(description="dai headquarters dashboard")
    ap.add_argument("--port", type=int, default=env_int("DAI_DASHBOARD_PORT", 8799))
    ap.add_argument("--host", default=env_str("DAI_DASHBOARD_HOST", "0.0.0.0"))
    args = ap.parse_args()
    Handler.state.refresh()

    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            Handler.state.refresh()
            stop.wait(POLL_SECONDS)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dai headquarters on http://{args.host}:{args.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        srv.shutdown()


if __name__ == "__main__":
    main()
