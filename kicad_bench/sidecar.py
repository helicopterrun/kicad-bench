"""sidecar — a live web dashboard of the whole-repo audit, to keep open beside KiCad.

`kb sidecar` starts a tiny local web server (stdlib only — no Flask) that serves one
self-contained dark dashboard. It runs the same `audit.run_all` the CLI does, returns it
as JSON, and the page re-renders. As you route and SAVE the board, the page notices the
.kicad_pcb mtime change and re-audits automatically — so the verdict, the "fix next" list,
and every check stay live alongside KiCad without you re-running anything.

The audit is cached by board mtime, so opening the page in several tabs (or polling) does
not trigger redundant DRC runs. Read-only — it never writes the board.

  kb sidecar --config CFG            # http://127.0.0.1:8765
  kb sidecar --port 9000 --host 0.0.0.0
"""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

from .core import config as cfgmod
from . import audit

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiCad Sidecar</title>
<style>
  :root{--bg:#1b1c1f;--panel:#232427;--line:#34363b;--fg:#d4d7dd;--muted:#8a8f98;
        --err:#ff7b7b;--warn:#ffcc66;--ok:#5fd38d;--info:#7fa8d8;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{position:sticky;top:0;z-index:5;background:#16171a;border-bottom:1px solid var(--line);
         padding:10px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.2px}
  .board{color:var(--muted)}
  .pill{padding:3px 10px;border-radius:20px;font-weight:700}
  .pill.pass{background:#143d28;color:var(--ok)} .pill.fail{background:#3d1717;color:var(--err)}
  .spacer{flex:1}
  .muted{color:var(--muted);font-size:12px}
  label{color:var(--muted);cursor:pointer;user-select:none}
  button{background:#2d2f34;color:var(--fg);border:1px solid var(--line);border-radius:6px;
         padding:5px 12px;cursor:pointer;font:inherit}
  button:hover{background:#383b41}
  main{padding:16px;max-width:1000px;margin:0 auto}
  .fixnext{background:#241a1a;border:1px solid #4a2a2a;border-radius:8px;padding:10px 14px;margin-bottom:18px}
  .fixnext h2{margin:0 0 6px;font-size:13px;color:var(--err)}
  .fixnext .row{padding:2px 0;border-top:1px solid #3a2626}
  .fixnext .row:first-of-type{border-top:0}
  .sec{margin:18px 0 6px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
  .check{background:var(--panel);border:1px solid var(--line);border-left-width:3px;border-radius:7px;
         margin:7px 0;overflow:hidden}
  .check.g-error{border-left-color:var(--err)} .check.g-warn{border-left-color:var(--warn)}
  .check.g-ok{border-left-color:var(--ok)}
  .chead{display:flex;gap:10px;align-items:baseline;padding:8px 12px;cursor:pointer}
  .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;align-self:center}
  .g-error .dot{background:var(--err)} .g-warn .dot{background:var(--warn)} .g-ok .dot{background:var(--ok)}
  .ctitle{font-weight:600} .csum{color:var(--muted);font-size:12px;margin-left:auto;text-align:right}
  .cfind{padding:0 12px 8px 30px;display:none} .check.open .cfind{display:block}
  .f{padding:3px 0;border-top:1px dashed #2c2e33}
  .badge{display:inline-block;min-width:42px;text-align:center;border-radius:4px;font-size:10px;
         padding:1px 5px;margin-right:7px;font-weight:700;text-transform:uppercase}
  .sev-error .badge{background:#3d1717;color:var(--err)} .sev-warn .badge{background:#3a3115;color:var(--warn)}
  .sev-info .badge{background:#1e2a38;color:var(--info)}
  .where{color:var(--muted)} .detail{color:var(--muted);font-size:12px;padding-left:49px}
  #loading{color:var(--muted);padding:8px 0}
</style></head>
<body>
<header>
  <h1>KiCad Sidecar</h1><span class="board" id="board"></span>
  <span class="pill" id="verdict">…</span>
  <span class="spacer"></span>
  <span class="muted" id="updated"></span>
  <label><input type="checkbox" id="issuesOnly"> issues only</label>
  <label><input type="checkbox" id="auto" checked> auto</label>
  <button id="rerun">Re-run</button>
</header>
<main>
  <div id="loading">running audit…</div>
  <div class="fixnext" id="fixnext" style="display:none"></div>
  <div id="sections"></div>
</main>
<script>
let lastMtime=null, busy=false;
const $=id=>document.getElementById(id);
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

async function loadAudit(force){
  if(busy) return; busy=true;
  $("loading").style.display="block"; $("loading").textContent="running audit…";
  try{
    const r=await fetch("/api/audit"+(force?"?force=1":""));
    const d=await r.json();
    lastMtime=d.board_mtime; render(d);
  }catch(e){ $("loading").textContent="audit error: "+e; }
  finally{ busy=false; $("loading").style.display="none"; }
}

function render(d){
  $("board").textContent=d.board||"";
  const v=d.verdict, fail=v.err_checks>0;
  const pill=$("verdict"); pill.className="pill "+(fail?"fail":"pass");
  pill.textContent=(fail?"FAIL":"PASS")+" — "+v.err_checks+" err / "+v.warn_checks+" warn / "+v.clean+" clean";
  $("updated").textContent="updated "+new Date().toLocaleTimeString();

  const issuesOnly=$("issuesOnly").checked;
  // fix-next: every error finding, grouped by check
  const fx=[];
  d.sections.forEach(s=>s.checks.forEach(c=>{
    c.findings.filter(f=>f.severity==="error").forEach(f=>fx.push({c:c.title,f}));
  }));
  const fn=$("fixnext");
  if(fx.length){ fn.style.display="block";
    fn.innerHTML="<h2>Fix next — "+fx.length+" error(s)</h2>"+fx.slice(0,40).map(x=>
      '<div class="row">'+esc(x.f.message)+(x.f.where?' <span class="where">['+esc(x.f.where)+']</span>':'')+
      ' <span class="where">· '+esc(x.c)+'</span></div>').join("");
  } else fn.style.display="none";

  const root=$("sections"); root.innerHTML="";
  d.sections.forEach(s=>{
    const shown=s.checks.filter(c=>!issuesOnly||c.glyph!=="ok");
    if(!shown.length) return;
    const h=document.createElement("div"); h.className="sec"; h.textContent=s.section; root.appendChild(h);
    shown.forEach(c=>{
      const finds=c.findings.filter(f=>!issuesOnly||f.severity!=="info");
      const open=c.glyph==="error";
      const el=document.createElement("div"); el.className="check g-"+c.glyph+(open?" open":"");
      el.innerHTML='<div class="chead"><span class="dot"></span>'+
        '<span class="ctitle">'+esc(c.title)+'</span><span class="csum">'+esc(c.summary)+'</span></div>'+
        '<div class="cfind">'+ (finds.length?finds.map(f=>
          '<div class="f sev-'+f.severity+'"><span class="badge">'+f.severity+'</span>'+esc(f.message)+
          (f.where?' <span class="where">['+esc(f.where)+']</span>':'')+'</div>'+
          (f.detail?'<div class="detail">'+esc(f.detail)+'</div>':'')
        ).join("") : '<div class="f"><span class="where">no findings</span></div>') +'</div>';
      el.querySelector(".chead").onclick=()=>el.classList.toggle("open");
      root.appendChild(el);
    });
  });
}

async function poll(){
  if(!$("auto").checked||busy) return;
  try{ const r=await fetch("/api/mtime"); const m=(await r.json()).mtime;
    if(lastMtime!==null && m!==lastMtime){ loadAudit(false); }
  }catch(e){}
}
$("rerun").onclick=()=>loadAudit(true);
$("issuesOnly").onchange=()=>loadAudit(false);
setInterval(poll,3000);
loadAudit(false);
</script>
</body></html>"""


class _State:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache: dict | None = None
        self.cache_mtime = None

    def _mtime(self):
        p = self.cfg.pcb
        return p.stat().st_mtime if p and p.exists() else 0

    def audit(self, force: bool) -> dict:
        with self.lock:
            m = self._mtime()
            if not force and self.cache is not None and self.cache_mtime == m:
                return self.cache
            data = audit.sections_to_dict(audit.run_all(self.cfg), self.cfg)
            self.cache, self.cache_mtime = data, m
            return data


def _make_handler(state: _State):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/mtime":
                self._send(200, json.dumps({"mtime": state._mtime()}).encode(), "application/json")
            elif path == "/api/audit":
                data = state.audit("force=1" in self.path)
                self._send(200, json.dumps(data).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(cfg: cfgmod.Config, host: str, port: int) -> int:
    httpd = http.server.ThreadingHTTPServer((host, port), _make_handler(_State(cfg)))
    print(f"KiCad sidecar → http://{host}:{port}   (Ctrl-C to stop)")
    print(f"  auditing: {cfg.pcb}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    return serve(cfg, args.host, args.port)


def add_parser(sub):
    p = sub.add_parser("sidecar", help="live web dashboard of the repo audit (open beside KiCad)")
    p.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
