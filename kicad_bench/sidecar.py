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
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs

from .core import cli
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
  nav#tabs{position:sticky;top:0;z-index:4;display:flex;gap:2px;background:#101113;
           border-bottom:1px solid var(--line);padding:0 10px}
  nav#tabs button{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;
           color:var(--muted);padding:9px 14px;font-weight:600}
  nav#tabs button:hover{background:#1b1c1f;color:var(--fg)}
  nav#tabs button.active{color:var(--fg);border-bottom-color:var(--info)}
  iframe#docframe{display:none;width:100%;border:0;background:#fff}
</style></head>
<body>
<header>
  <h1>KiCad Sidecar</h1><span class="board" id="board"></span>
  <span class="pill" id="verdict">…</span>
  <span class="spacer"></span>
  <span class="muted" id="updated"></span>
  <span id="auditctl">
    <label><input type="checkbox" id="issuesOnly"> issues only</label>
    <label><input type="checkbox" id="auto" checked> auto</label>
    <button id="rerun">Re-run</button>
  </span>
</header>
<nav id="tabs"></nav>
<main id="auditview">
  <div id="loading">running audit…</div>
  <div class="fixnext" id="fixnext" style="display:none"></div>
  <div id="sections"></div>
</main>
<iframe id="docframe" title="guide"></iframe>
<script>
let lastMtime=null, busy=false, curTab="audit", lastSchMtime=null;
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
  if(curTab==="preview"){
    try{ const sm=(await (await fetch("/api/preview/sch.mtime")).json()).mtime;
      if(lastSchMtime!==null && sm!==lastSchMtime){ docSrc=null; $("docframe").src="/preview/sch.pdf?t="+Date.now(); }
      lastSchMtime=sm;
    }catch(e){}
  }
}
// ---- tabs: Audit dashboard + project doc guides (served from /doc/<i>) ----
let docSrc=null;
function sizeFrame(){ const f=$("docframe");
  if(f.style.display!=="none") f.style.height=(window.innerHeight-f.getBoundingClientRect().top)+"px"; }
function switchTab(kind,i,btn){
  document.querySelectorAll("#tabs button").forEach(b=>b.classList.remove("active"));
  if(btn) btn.classList.add("active");
  curTab=kind;
  const isAudit=(kind==="audit"), f=$("docframe");
  $("auditview").style.display=isAudit?"block":"none";
  $("auditctl").style.display=isAudit?"":"none";
  if(isAudit){ f.style.display="none"; return; }
  let src;
  if(kind==="preview") src="/preview/sch.pdf?t="+Date.now();
  else if(kind==="pcb2d") src="/preview/pcb2d?t="+Date.now();
  else if(kind==="pcb3d") src="/preview/pcb3d?t="+Date.now();
  else src="/doc/"+i;
  if(docSrc!==src){ f.src=src; docSrc=src; } f.style.display="block"; sizeFrame();
}
async function loadTabs(){
  let tabs=[]; try{ tabs=await (await fetch("/api/tabs")).json(); }catch(e){}
  const nav=$("tabs"); nav.innerHTML="";
  const mk=(label,kind,i)=>{ const b=document.createElement("button"); b.textContent=label;
    b.onclick=()=>switchTab(kind,i,b); nav.appendChild(b); return b; };
  mk("Audit","audit",null).classList.add("active");
  mk("Schematic","preview",null);
  mk("PCB 2D","pcb2d",null);
  mk("PCB 3D","pcb3d",null);
  tabs.forEach((t,i)=>mk(t.title,"doc",i));
}
window.addEventListener("resize",sizeFrame);

$("rerun").onclick=()=>loadAudit(true);
$("issuesOnly").onchange=()=>loadAudit(false);
setInterval(poll,3000);
loadTabs();
loadAudit(false);
</script>
</body></html>"""


# Shared toolbar/style for the two PCB-preview wrapper pages (dark bg + Top/Bottom toggle).
_PCB_CSS = """
 html,body{margin:0;height:100%;background:#1b1c1f;color:#d4d7dd;
   font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
 #bar{position:sticky;top:0;display:flex;gap:8px;align-items:center;
   padding:8px 12px;background:#16171a;border-bottom:1px solid #34363b}
 #bar .lbl{color:#8a8f98}
 button{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:4px 12px;cursor:pointer;font:inherit}
 button.active{background:#1e2a38;border-color:#7fa8d8;color:#fff}
 img{max-width:100%;max-height:100%;object-fit:contain}
 .dim{opacity:.55}"""

# "PCB 2D" tab — a dark-background layer browser. Each board layer is fetched as its own
# SVG (recolored server-side to a palette color) and stacked as a pixel-aligned overlay;
# checkboxes toggle layers and a colored swatch shows each layer's color. Top/Bottom are
# presets (a sensible layer set + mirror); individual layers refine it. The page polls
# board mtime and refreshes only the visible overlays on save, preserving the toggles.
PCB2D_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>""" + _PCB_CSS + """
 body{display:flex;flex-direction:column}
 #bar{flex-wrap:wrap}
 #stagewrap{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;
   padding:10px;box-sizing:border-box;background:#001023}
 #stage{position:relative;width:100%;height:100%}
 #stage.mir{transform:scaleX(-1)}
 #stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
 #layers{display:flex;flex-wrap:wrap;align-items:center}
 .lyr{display:inline-flex;align-items:center;gap:4px;margin:0 6px 0 0;cursor:pointer;
   color:#d4d7dd;font-size:12px}
 .lyr input{margin:0}
 .sw{width:10px;height:10px;border-radius:2px;display:inline-block;border:1px solid #0006}
</style></head><body>
<div id="bar"><span class="lbl">PCB 2D</span>
  <button id="bt" class="active" onclick="preset('top')">Top</button>
  <button id="bb" onclick="preset('bottom')">Bottom</button>
  <span id="layers"></span>
</div>
<div id="stagewrap"><div id="stage"></div></div>
<script>
// Colors are the KiCad "Paimon-Dark" theme's pcbnew `board` layer colors, so the 2D
// view matches eeschema/pcbnew under that theme (board bg #001023 set in CSS above).
const LAYERS=[
 {id:'B.Cu',         name:'B.Cu',   color:'FF8B44'},
 {id:'In2.Cu',       name:'In2.Cu', color:'A9DD00'},
 {id:'In1.Cu',       name:'In1.Cu', color:'18D4FF'},
 {id:'F.Cu',         name:'F.Cu',   color:'1B90B3'},
 {id:'B.Mask',       name:'B.Mask', color:'005D7C'},
 {id:'F.Mask',       name:'F.Mask', color:'AE4E00'},
 {id:'B.Silkscreen', name:'B.Silk', color:'C0D4D4'},
 {id:'F.Silkscreen', name:'F.Silk', color:'CFE0E5'},
 {id:'B.Fab',        name:'B.Fab',  color:'987D7E'},
 {id:'F.Fab',        name:'F.Fab',  color:'798596'},
 {id:'Edge.Cuts',    name:'Edge',   color:'D0D2CD'},
];
const DEFAULT_ON=['F.Cu','F.Silkscreen','Edge.Cuts'];
const stage=document.getElementById('stage'), imgs={}, cbs={};
let mt=0, mirror=false;
function src(L){ return '/preview/pcb-layer.svg?layer='+encodeURIComponent(L.id)+'&color='+L.color+'&t='+mt; }
function show(L,on){
  if(on){ let im=imgs[L.id];
    if(!im){ im=new Image(); im.alt=L.id; im.style.zIndex=LAYERS.indexOf(L); imgs[L.id]=im; stage.appendChild(im); }
    im.src=src(L); im.style.display='block';
  }else if(imgs[L.id]){ imgs[L.id].style.display='none'; }
}
function build(){
  const box=document.getElementById('layers');
  LAYERS.forEach(L=>{
    const lab=document.createElement('label'); lab.className='lyr';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=DEFAULT_ON.includes(L.id);
    const sw=document.createElement('span'); sw.className='sw'; sw.style.background='#'+L.color;
    cb.onchange=()=>show(L,cb.checked); cbs[L.id]=cb;
    lab.appendChild(cb); lab.appendChild(sw); lab.appendChild(document.createTextNode(L.name));
    box.appendChild(lab);
  });
}
function preset(p){
  const want = p==='bottom' ? ['B.Cu','B.Silkscreen','Edge.Cuts'] : ['F.Cu','F.Silkscreen','Edge.Cuts'];
  LAYERS.forEach(L=>{ const on=want.includes(L.id); cbs[L.id].checked=on; show(L,on); });
  mirror=(p==='bottom'); stage.classList.toggle('mir',mirror);
  document.getElementById('bt').classList.toggle('active',p==='top');
  document.getElementById('bb').classList.toggle('active',p==='bottom');
}
async function tick(){
  if(document.hidden) return;
  try{ const m=(await (await fetch('/api/mtime')).json()).mtime;
    if(m!==mt){ mt=m; LAYERS.forEach(L=>{ const im=imgs[L.id]; if(im&&im.style.display!=='none') im.src=src(L); }); }
  }catch(e){}
}
build();
LAYERS.forEach(L=>{ if(DEFAULT_ON.includes(L.id)) show(L,true); });
tick(); setInterval(tick,3000);
</script>
</body></html>"""

# "PCB 3D" tab. The raytrace is ~30s, so this page polls the render status, shows a
# "rendering…" note, and swaps in the cached PNG when ready — keeping the last good image
# on screen while a fresh one renders after a save. Top/Bottom each render independently
# (cached server-side per side).
PCB3D_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>""" + _PCB_CSS + """
 #wrap{height:calc(100% - 39px);display:flex;flex-direction:column;align-items:center;
   justify-content:center;gap:14px;padding:12px;box-sizing:border-box}
 img{display:none;border-radius:6px}
 #msg{padding:8px 14px;border:1px solid #34363b;border-radius:6px;background:#232427}
 #msg.err{color:#ff7b7b;border-color:#4a2a2a}
</style></head><body>
<div id="bar"><span class="lbl">PCB 3D</span>
  <button id="bt" class="active" onclick="setSide('top')">Top</button>
  <button id="bb" onclick="setSide('bottom')">Bottom</button>
</div>
<div id="wrap"><img id="im" alt="PCB 3D render"><div id="msg">starting 3D render…</div></div>
<script>
let side='top', cur=null;
const im=document.getElementById('im'), msg=document.getElementById('msg');
function setSide(s){ side=s; cur=null; im.style.display='none';
  document.getElementById('bt').classList.toggle('active',s==='top');
  document.getElementById('bb').classList.toggle('active',s==='bottom');
  tick();
}
async function tick(){
  if(document.hidden) return;
  let s; try{ s=await (await fetch('/api/preview/pcb3d?side='+side)).json(); }
  catch(e){ msg.className='err'; msg.textContent='3D preview unavailable'; return; }
  if(s.status==='ready'){
    if(cur!==s.mtime){ cur=s.mtime; im.src='/preview/pcb.png?side='+side+'&t='+s.mtime; }
    im.style.display='block'; im.classList.remove('dim'); msg.className='dim';
    msg.textContent='3D render · '+side+' · live (re-renders on board save)';
  }else if(s.status==='rendering'){
    msg.className=cur?'dim':''; im.classList.toggle('dim',!!cur);
    msg.textContent=cur?'re-rendering 3D after save… (~30s)':'rendering 3D '+side+'… (~30s — first load and after each save)';
  }else if(s.status==='error'){
    msg.className='err'; msg.textContent='3D render failed: '+(s.error||'unknown');
  }else{
    msg.className=''; msg.textContent='no board configured for 3D preview';
  }
}
tick(); setInterval(tick,2000);
</script>
</body></html>"""


class _State:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache: dict | None = None
        self.cache_mtime = None
        self.sch_cache: bytes | None = None
        self.sch_cache_mtime = None
        # 2D PCB preview (fast, synchronous, board-mtime cached) — keyed by side
        self._svg: dict = {}                       # side -> (mtime, bytes)
        # 2D per-layer SVG overlays (recolored), board-mtime cached — keyed by (layer, color)
        self._layer_svg: dict = {}                 # (layer, color) -> (mtime, bytes)
        # 3D PCB preview (slow ~30s — background thread, never blocks a request) — per side
        self._png = {s: {"cache": None, "mtime": None, "rendering": False, "error": None}
                     for s in ("top", "bottom")}

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

    def sch_mtime(self):
        """Newest mtime across all sheets, so editing ANY child sheet invalidates the
        preview (the root sheet's own mtime wouldn't change when a child changes)."""
        rs = self.cfg.root_sch
        if not rs:
            return 0
        mts = [p.stat().st_mtime for p in rs.parent.glob("*.kicad_sch") if p.exists()]
        return max(mts) if mts else 0

    def sch_pdf(self):
        """Cached schematic→PDF render (bytes), refreshed when any sheet changes.
        Returns None if there is no root schematic or the render fails — the preview
        must never break the dashboard."""
        if not self.cfg.root_sch:
            return None
        with self.lock:
            m = self.sch_mtime()
            if self.sch_cache is not None and self.sch_cache_mtime == m:
                return self.sch_cache
            try:
                data = cli.export_sch_pdf(self.cfg.root_sch)
            except Exception:  # noqa: BLE001
                return None
            self.sch_cache, self.sch_cache_mtime = data, m
            return data

    def pcb_svg(self, side="top"):
        """Cached 2D PCB SVG (bytes) for the given side, refreshed when the board
        changes. Fast enough (~2s) to render on the request thread. Returns None if
        there is no board or the render fails — the preview must never break the
        dashboard."""
        if not self.cfg.pcb:
            return None
        m = self._mtime()
        with self.lock:
            c = self._svg.get(side)
            if c is not None and c[0] == m:
                return c[1]
        try:  # render OUTSIDE the lock — don't stall audit/mtime requests
            data = cli.export_pcb_svg(self.cfg.pcb, side=side)
        except Exception:  # noqa: BLE001
            return None
        with self.lock:
            self._svg[side] = (m, data)
        return data

    def pcb_layer_svg(self, layer, color=None):
        """Cached recolored SVG (bytes) for one board layer, refreshed on board change.
        Returns None on no-board / render failure — a missing overlay must not break the
        tab."""
        if not self.cfg.pcb:
            return None
        m = self._mtime()
        key = (layer, color)
        with self.lock:
            c = self._layer_svg.get(key)
            if c is not None and c[0] == m:
                return c[1]
        try:  # render OUTSIDE the lock — don't stall audit/mtime requests
            data = cli.export_pcb_layer_svg(self.cfg.pcb, layer, color)
        except Exception:  # noqa: BLE001
            return None
        with self.lock:
            self._layer_svg[key] = (m, data)
        return data

    def pcb_3d_state(self, side="top") -> dict:
        """Status of the cached 3D render of the given board side. Kicks off a
        background render if the cache is missing/stale and none is already running,
        so the request returns instantly. status: ready|rendering|error|none."""
        if not self.cfg.pcb:
            return {"status": "none", "mtime": 0, "side": side}
        m = self._mtime()
        st = self._png[side]
        with self.lock:
            if st["cache"] is not None and st["mtime"] == m:
                return {"status": "ready", "mtime": m, "side": side}
            if st["rendering"]:
                return {"status": "rendering", "mtime": m, "side": side}
            if st["error"] is not None and st["mtime"] == m:
                return {"status": "error", "mtime": m, "side": side, "error": st["error"]}
            st["rendering"] = True
        threading.Thread(target=self._render_3d_bg, args=(side, m), daemon=True).start()
        return {"status": "rendering", "mtime": m, "side": side}

    def _render_3d_bg(self, side, m):
        err = None
        data = None
        try:
            data = cli.render_pcb_3d(self.cfg.pcb, side=side)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        with self.lock:
            st = self._png[side]
            if data is not None:
                st["cache"], st["mtime"], st["error"] = data, m, None
            else:
                st["mtime"], st["error"] = m, err
            st["rendering"] = False

    def pcb_png(self, side="top"):
        with self.lock:
            return self._png[side]["cache"]


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

        def _side(self):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            s = (parse_qs(q).get("side", ["top"])[0]).lower()
            return s if s in ("top", "bottom") else "top"

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/mtime":
                self._send(200, json.dumps({"mtime": state._mtime()}).encode(), "application/json")
            elif path == "/api/audit":
                data = state.audit("force=1" in self.path)
                self._send(200, json.dumps(data).encode(), "application/json")
            elif path == "/api/tabs":
                tabs = [{"title": t.title} for t in state.cfg.sidecar_tabs]
                self._send(200, json.dumps(tabs).encode(), "application/json")
            elif path == "/api/preview/sch.mtime":
                self._send(200, json.dumps({"mtime": state.sch_mtime()}).encode(),
                           "application/json")
            elif path == "/preview/sch.pdf":
                data = state.sch_pdf()
                if not data:
                    self._send(404, b"no schematic preview (no root_sch or render failed)",
                               "text/plain")
                else:
                    self._send(200, data, "application/pdf")
            elif path == "/preview/pcb2d":
                self._send(200, PCB2D_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/preview/pcb.svg":
                data = state.pcb_svg(self._side())
                if not data:
                    self._send(404, b"no PCB preview (no board or render failed)", "text/plain")
                else:
                    self._send(200, data, "image/svg+xml; charset=utf-8")
            elif path == "/preview/pcb-layer.svg":
                q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                layer = q.get("layer", [""])[0]
                color = q.get("color", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9._]+", layer):
                    self._send(400, b"bad layer", "text/plain"); return
                color = color if re.fullmatch(r"[0-9A-Fa-f]{6}", color) else None
                data = state.pcb_layer_svg(layer, color)
                if not data:
                    self._send(404, b"layer render failed", "text/plain")
                else:
                    self._send(200, data, "image/svg+xml; charset=utf-8")
            elif path == "/preview/pcb3d":
                self._send(200, PCB3D_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/preview/pcb3d":
                self._send(200, json.dumps(state.pcb_3d_state(self._side())).encode(),
                           "application/json")
            elif path == "/preview/pcb.png":
                data = state.pcb_png(self._side())
                if not data:
                    self._send(404, b"3D render not ready", "text/plain")
                else:
                    self._send(200, data, "image/png")
            elif path.startswith("/doc/"):
                try:
                    tab = state.cfg.sidecar_tabs[int(path[len("/doc/"):])]
                except (ValueError, IndexError):
                    self._send(404, b"no such doc tab", "text/plain"); return
                if not tab.path.exists():
                    self._send(404, f"guide file missing: {tab.path}".encode(), "text/plain"); return
                self._send(200, tab.path.read_bytes(), "text/html; charset=utf-8")
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
