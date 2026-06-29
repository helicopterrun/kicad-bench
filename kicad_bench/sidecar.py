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
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import parse_qs

from .core import cli
from .core import config as cfgmod
from . import audit
from . import datasheet as dsmod

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
           color:var(--muted);padding:9px 14px;font-weight:600;white-space:nowrap;touch-action:manipulation}
  nav#tabs button:hover{background:#1b1c1f;color:var(--fg)}
  nav#tabs button.active{color:var(--fg);border-bottom-color:var(--info)}
  .tdd{position:relative;display:inline-flex;margin-left:auto}
  #moretab{font-size:16px}
  #tmenu{position:absolute;top:calc(100% + 4px);right:0;z-index:60;display:none;
         background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px;
         min-width:190px;max-height:72vh;overflow:auto;box-shadow:0 10px 30px #000a}
  #tmenu.open{display:block}
  #tmenu .trow{display:block;width:100%;text-align:left;padding:12px;border:0;border-radius:7px;
         background:transparent;color:var(--fg);font:inherit;font-size:15px;cursor:pointer;
         font-weight:600;touch-action:manipulation}
  #tmenu .trow:active{background:#2d2f34}
  #tmenu .trow.sel{background:#1e2a38;color:#fff}
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
let lastMtime=null, busy=false, curTab="audit", lastSchMtime=null, moreOpen=false, tmenuRows=[];
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
  if(kind==="preview") src="/preview/schematic?t="+Date.now();
  else if(kind==="pcb2d") src="/preview/pcb2d?t="+Date.now();
  else if(kind==="pcb3d") src="/preview/pcb3d?t="+Date.now();
  else if(kind==="parts") src="/preview/parts?t="+Date.now();
  else if(kind==="datasheets") src="/preview/datasheets?t="+Date.now();
  else src="/doc/"+i;
  if(docSrc!==src){ f.src=src; docSrc=src; } f.style.display="block"; sizeFrame();
}
// Deep-link from the Parts tab: jump to the Datasheets tab at a given doc + page.
function openDatasheet(dsIdx,page){
  document.querySelectorAll("#tabs button").forEach(b=>b.classList.remove("active"));
  const b=[...document.querySelectorAll("#tabs button")].find(x=>x.textContent==="Datasheets");
  if(b) b.classList.add("active");
  curTab="datasheets"; $("auditview").style.display="none"; $("auditctl").style.display="none";
  const f=$("docframe"), src="/preview/datasheets?ds="+dsIdx+"&page="+(page||1)+"&t="+Date.now();
  f.src=src; docSrc=src; f.style.display="block"; sizeFrame();
}
window.openDatasheet=openDatasheet;
function toggleMore(){ moreOpen?closeMore():openMore(); }
function openMore(){ const m=$("tmenu"); if(m){ m.classList.add("open"); moreOpen=true; } }
function closeMore(){ const m=$("tmenu"); if(m){ m.classList.remove("open"); moreOpen=false; } }
document.addEventListener("click",e=>{ if(moreOpen && !e.target.closest(".tdd")) closeMore(); });
async function loadTabs(){
  let tabs=[]; try{ tabs=await (await fetch("/api/tabs")).json(); }catch(e){}
  const nav=$("tabs"); nav.innerHTML=""; tmenuRows=[];
  const mk=(label,kind,i)=>{ const b=document.createElement("button"); b.textContent=label;
    b.onclick=()=>switchTab(kind,i,b); nav.appendChild(b); return b; };
  mk("Audit","audit",null).classList.add("active");
  mk("Schematic","preview",null);
  mk("PCB 2D","pcb2d",null);
  mk("PCB 3D","pcb3d",null);
  mk("Parts","parts",null);
  mk("Datasheets","datasheets",null);
  if(tabs.length){               // doc guides go behind a hamburger so the bar never overflows
    const dd=document.createElement("span"); dd.className="tdd";
    const ham=document.createElement("button"); ham.id="moretab"; ham.textContent="☰"; ham.title="More tabs";
    ham.onclick=e=>{ e.stopPropagation(); toggleMore(); };
    const menu=document.createElement("div"); menu.id="tmenu";
    tabs.forEach((t,i)=>{ const r=document.createElement("button"); r.type="button"; r.className="trow";
      r.textContent=t.title;
      r.onclick=()=>{ switchTab("doc",i,null); ham.classList.add("active");
        tmenuRows.forEach(x=>x.classList.remove("sel")); r.classList.add("sel"); closeMore(); };
      tmenuRows.push(r); menu.appendChild(r); });
    dd.appendChild(ham); dd.appendChild(menu); nav.appendChild(dd);
  }
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
PCB2D_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>""" + _PCB_CSS + """
 body{display:flex;flex-direction:column}
 #bar{flex-wrap:wrap;z-index:50}
 #stagewrap{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;
   padding:10px;box-sizing:border-box;background:#001023;position:relative;z-index:0}
 #stage{position:relative;width:100%;height:100%}
 #stage.mir{transform:scaleX(-1)}
 #stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
 #bar button{padding:8px 14px;touch-action:manipulation}
 .dd{position:relative;display:inline-block}
 #menu{position:absolute;top:calc(100% + 6px);left:0;z-index:50;display:none;
   background:#232427;border:1px solid #34363b;border-radius:10px;padding:6px;
   min-width:230px;max-height:70vh;overflow:auto;box-shadow:0 10px 30px #000a}
 #menu.open{display:block}
 #menu .row{display:flex;align-items:center;gap:11px;width:100%;text-align:left;
   padding:12px;margin:0;border:0;border-radius:7px;background:transparent;color:#d4d7dd;
   font:inherit;font-size:15px;cursor:pointer;touch-action:manipulation}
 #menu .row.on{background:#1e2a38}
 #menu .row:active{background:#2d2f34}
 #menu .sw{width:18px;height:18px;border-radius:3px;border:1px solid #0006;flex:0 0 auto}
 #menu .nm{flex:1}
 #menu .ck{width:18px;text-align:center;color:#7fa8d8;opacity:0}
 #menu .row.on .ck{opacity:1}
 #menu .allrow{justify-content:center;font-weight:600;color:#7fa8d8;border-radius:0;
   border-bottom:1px solid #34363b;margin-bottom:4px;padding:10px}
</style></head><body>
<div id="bar"><span class="lbl">PCB 2D</span>
  <button id="bt" class="active" onclick="preset('top')">Top</button>
  <button id="bb" onclick="preset('bottom')">Bottom</button>
  <span class="dd"><button id="lbtn" onclick="toggleMenu(event)">Layers ▾</button>
    <div id="menu"></div></span>
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
const stage=document.getElementById('stage'), imgs={}, rows={};
let mt=0, mirror=false;
function src(L){ return '/preview/pcb-layer.svg?layer='+encodeURIComponent(L.id)+'&color='+L.color+'&t='+mt; }
function show(L,on){
  if(on){ let im=imgs[L.id];
    if(!im){ im=new Image(); im.alt=L.id; im.style.zIndex=LAYERS.indexOf(L); imgs[L.id]=im; stage.appendChild(im); }
    im.src=src(L); im.style.display='block';
  }else if(imgs[L.id]){ imgs[L.id].style.display='none'; }
  if(rows[L.id]) rows[L.id].classList.toggle('on',on);
  updateAll();
}
function allOn(){ return LAYERS.every(L=>imgs[L.id] && imgs[L.id].style.display!=='none'); }
function updateAll(){ const b=document.getElementById('allbtn'); if(b) b.textContent=allOn()?'All off':'All on'; }
function toggleAll(){ const on=!allOn(); LAYERS.forEach(L=>show(L,on)); }
function buildMenu(){
  const menu=document.getElementById('menu');
  const all=document.createElement('button'); all.type='button'; all.id='allbtn'; all.className='row allrow';
  all.onclick=toggleAll; menu.appendChild(all);
  LAYERS.forEach(L=>{
    const row=document.createElement('button'); row.type='button'; row.className='row';
    row.innerHTML='<span class="sw" style="background:#'+L.color+'"></span>'+
      '<span class="nm">'+L.name+'</span><span class="ck">✓</span>';
    row.onclick=()=>show(L,!row.classList.contains('on'));
    rows[L.id]=row; menu.appendChild(row);
  });
  updateAll();
}
function preset(p){
  const want = p==='bottom' ? ['B.Cu','B.Silkscreen','Edge.Cuts'] : ['F.Cu','F.Silkscreen','Edge.Cuts'];
  LAYERS.forEach(L=>show(L,want.includes(L.id)));
  mirror=(p==='bottom'); stage.classList.toggle('mir',mirror);
  document.getElementById('bt').classList.toggle('active',p==='top');
  document.getElementById('bb').classList.toggle('active',p==='bottom');
}
let menuOpen=false;
function toggleMenu(e){ if(e) e.stopPropagation(); menuOpen?closeMenu():openMenu(); }
function openMenu(){ document.getElementById('menu').classList.add('open'); menuOpen=true; }
function closeMenu(){ document.getElementById('menu').classList.remove('open'); menuOpen=false; }
document.addEventListener('click',e=>{ if(menuOpen && !e.target.closest('.dd')) closeMenu(); });
async function tick(){
  if(document.hidden) return;
  try{ const m=(await (await fetch('/api/mtime')).json()).mtime;
    if(m!==mt){ mt=m; LAYERS.forEach(L=>{ const im=imgs[L.id]; if(im&&im.style.display!=='none') im.src=src(L); }); }
  }catch(e){}
}
buildMenu();
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


# "Schematic" tab. iOS Safari won't render a PDF inside an <iframe> (no zoom/pan/pages),
# so this wrapper gives an "Open in PDF viewer" link that loads the PDF as a top-level
# tab — the native viewer, with full pinch-zoom/pan/pages on iPad and desktop alike — and
# keeps an inline embed for desktop. It re-points both at the fresh PDF when a sheet saves.
SCHEMATIC_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;height:100%;background:#1b1c1f;color:#d4d7dd;
   font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;display:flex;flex-direction:column}
 #bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 12px;
   background:#16171a;border-bottom:1px solid #34363b}
 #bar .lbl{color:#8a8f98}
 a.btn{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:8px 14px;cursor:pointer;font:inherit;text-decoration:none}
 a.btn.primary{background:#1e2a38;border-color:#7fa8d8;color:#fff}
 .hint{color:#8a8f98;font-size:12px}
 #embed{flex:1;min-height:0;width:100%;border:0;background:#fff}
</style></head><body>
<div id="bar"><span class="lbl">Schematic</span>
  <a class="btn primary" id="open" href="/preview/sch.pdf" target="_blank" rel="noopener">Open in PDF viewer ↗</a>
  <a class="btn" id="dl" href="/preview/sch.pdf" download="schematic.pdf">Download</a>
  <span class="hint">On iPad/iOS, tap “Open in PDF viewer” for zoom, pan &amp; pages.</span>
</div>
<iframe id="embed" title="schematic" src="/preview/sch.pdf"></iframe>
<script>
let mt=null;
const embed=document.getElementById('embed'), open=document.getElementById('open'), dl=document.getElementById('dl');
function apply(){ const u='/preview/sch.pdf?t='+(mt||0); embed.src=u; open.href=u; dl.href=u; }
async function tick(){
  if(document.hidden) return;
  try{ const m=(await (await fetch('/api/preview/sch.mtime')).json()).mtime;
    if(m!==mt){ mt=m; apply(); }
  }catch(e){}
}
tick(); setInterval(tick,3000);
</script>
</body></html>"""


# "Datasheets" tab — a repo datasheet/app-note browser. Picks a PDF, parses its TOC, and renders
# pages to cached PNGs server-side (iOS-friendly: images, not an inline PDF). Contents dropdown
# jumps to a section's page; ◀/▶ page through; 1:1 toggles fit vs natural (scroll to read fine
# print); "Open ↗" hands the raw PDF to the native viewer. Rendering here is for a human, so it's
# unrestricted (the LLM-only cost rule is about agent image reads, not this).
DATASHEETS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>""" + _PCB_CSS + """
 body{display:flex;flex-direction:column}
 #bar{flex-wrap:wrap}
 select{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:7px 10px;font:inherit;max-width:46vw}
 a.btn{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:7px 12px;text-decoration:none;font:inherit;cursor:pointer}
 #bar button{touch-action:manipulation}
 #q{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:6px 10px;font:inherit;width:130px}
 #sres{display:inline-flex;flex-wrap:wrap;gap:4px;align-items:center}
 .ind{color:#8a8f98}
 #chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 12px;
   background:#16171a;border-bottom:1px solid #34363b}
 #chips:empty{display:none}
 #chips .lbl{color:#8a8f98;font-size:12px;margin-right:2px}
 .chip{background:#2d2f34;border:1px solid #34363b;border-radius:14px;padding:5px 11px;
   color:#d4d7dd;font:inherit;font-size:12px;cursor:pointer;touch-action:manipulation}
 .chip.pin{border-color:#7fa8d8;color:#cfe0e5}
 .chip:active{background:#383b41}
 #stagewrap{flex:1;min-height:0;overflow:auto;display:flex;align-items:center;
   justify-content:center;padding:10px;box-sizing:border-box;background:#11141a}
 #stagewrap.scroll{display:block}
 #im{display:block;border-radius:4px}
 #im.fit{max-width:100%;max-height:100%;object-fit:contain}
 #im.full{max-width:none;max-height:none}
 #msg{color:#8a8f98}
 .dd{position:relative;display:inline-block}
 #toc{position:absolute;top:calc(100% + 6px);left:0;z-index:50;display:none;background:#232427;
   border:1px solid #34363b;border-radius:10px;padding:6px;min-width:300px;max-width:88vw;
   max-height:72vh;overflow:auto;box-shadow:0 10px 30px #000a}
 #toc.open{display:block}
 #toc button{display:block;width:100%;text-align:left;padding:9px 10px;border:0;border-radius:6px;
   background:transparent;color:#d4d7dd;font:inherit;font-size:14px;cursor:pointer;
   touch-action:manipulation;white-space:nowrap}
 #toc button:active{background:#2d2f34}
 #toc .pg{color:#7fa8d8;margin-right:8px}
</style></head><body>
<div id="bar"><span class="lbl">Datasheet</span>
  <select id="ds"></select>
  <span class="dd"><button id="tocbtn" onclick="toggleToc(event)">Contents ▾</button><div id="toc"></div></span>
  <button onclick="toTop()" title="First page">⤒ Top</button>
  <button onclick="go(-1)">◀</button><span class="ind" id="ind">—</span><button onclick="go(1)">▶</button>
  <button id="zoom" onclick="toggleZoom()">1:1</button>
  <input id="q" type="search" placeholder="find in doc…" onkeydown="if(event.key==='Enter')doSearch()">
  <span id="sres"></span>
  <a class="btn" id="open" target="_blank" rel="noopener">Open ↗</a>
</div>
<div id="chips"></div>
<div id="stagewrap"><img id="im" class="fit" alt="datasheet page"><span id="msg"></span></div>
<script>
let idx=-1, page=1, meta=null, tocOpen=false, full=false, forcePage=0;
const PARAMS=new URLSearchParams(location.search);
const im=document.getElementById('im'), msg=document.getElementById('msg'),
      sel=document.getElementById('ds'), tocEl=document.getElementById('toc'),
      wrap=document.getElementById('stagewrap');
function setMsg(t){ msg.textContent=t||''; msg.style.display=t?'block':'none'; im.style.display=t?'none':'block'; }
function paint(){ if(idx<0||!meta) return;
  im.src='/preview/datasheet.png?ds='+idx+'&page='+page+'&t='+Date.now();
  document.getElementById('ind').textContent='p '+page+' / '+meta.n;
  document.getElementById('open').href='/preview/datasheet.pdf?ds='+idx;
  saveLast();
}
function go(d){ if(!meta) return; const np=Math.max(1,Math.min(page+d,meta.n)); if(np!==page){ page=np; paint(); } }
function lkey(){ return meta?('dsv:'+meta.name):null; }
function saveLast(){ try{ if(meta){ localStorage.setItem(lkey(),page); localStorage.setItem('dsv:lastdoc',meta.name); } }catch(e){} }
function lastPage(){ try{ const v=localStorage.getItem(lkey()); const n=v?parseInt(v,10):0; return (n>=1&&n<=meta.n)?n:1; }catch(e){ return 1; } }
function toTop(){ jump(1); wrap.scrollTop=0; wrap.scrollLeft=0; }
async function doSearch(){ const q=document.getElementById('q').value.trim(), s=document.getElementById('sres');
  if(idx<0) return; if(q.length<2){ s.innerHTML=''; return; }
  s.innerHTML='<span class="ind">searching…</span>';
  let pages=[]; try{ pages=((await (await fetch('/api/datasheet/search?ds='+idx+'&q='+encodeURIComponent(q))).json()).pages)||[]; }catch(e){}
  if(!pages.length){ s.innerHTML='<span class="ind">no matches</span>'; return; }
  s.innerHTML=''; const lab=document.createElement('span'); lab.className='ind';
  lab.textContent=pages.length+' hit'+(pages.length>1?'s':'')+': '; s.appendChild(lab);
  pages.slice(0,15).forEach(function(pg){ const b=document.createElement('button'); b.className='chip';
    b.textContent='p'+pg; b.onclick=function(){ jump(pg); }; s.appendChild(b); });
  if(pages.length>15){ const e=document.createElement('span'); e.className='ind'; e.textContent='…'; s.appendChild(e); }
  jump(pages[0]);
}
function toggleZoom(){ full=!full; im.className=full?'full':'fit'; wrap.classList.toggle('scroll',full);
  document.getElementById('zoom').textContent=full?'Fit':'1:1'; }
function buildToc(){ tocEl.innerHTML=''; const b=document.getElementById('tocbtn');
  if(!meta||!meta.toc||!meta.toc.length){ b.style.display='none'; return; }
  b.style.display='';
  meta.toc.forEach(function(e){ const it=document.createElement('button');
    it.innerHTML='<span class="pg">p'+e[2]+'</span>'+e[0]+'  '+e[1].replace(/&/g,'&amp;').replace(/</g,'&lt;');
    it.onclick=function(){ page=e[2]; paint(); closeToc(); }; tocEl.appendChild(it); });
}
function jump(pg){ page=pg; if(full) toggleZoom(); paint(); }
function mkchip(parent,label,pg,isPin){ const b=document.createElement('button');
  b.className='chip'+(isPin?' pin':''); b.textContent=label;
  b.onclick=function(){ jump(pg); }; parent.appendChild(b); }
const SECKW={pinout:['pinout','pin configuration','pin description','pin assignment','pin definition'],
  'abs-max':['absolute maximum'],
  'typical-app':['typical application','application information','application circuit','application schematic'],
  layout:['layout','land pattern','recommended footprint']};
// Prefer the doc's TOC (accurate); fall back to keyword locate only when there's no TOC.
function secPage(key){ const toc=meta.toc||[], kw=SECKW[key]||[];
  if(toc.length){ for(let i=0;i<toc.length;i++){ const t=toc[i][1].toLowerCase();
      for(let j=0;j<kw.length;j++){ if(t.indexOf(kw[j])>=0) return toc[i][2]; } }
    return null; }
  const arr=(meta.sections||{})[key]||[]; return arr.length?arr[0]:null;
}
function buildChips(){ const c=document.getElementById('chips'); c.innerHTML=''; if(!meta) return;
  const figs=meta.figs||[], seen={};
  if(figs.length){ const l=document.createElement('span'); l.className='lbl'; l.textContent='Pinouts:'; c.appendChild(l);
    figs.forEach(function(e){ e[1].forEach(function(pk){
      if(!(pk in seen)){ seen[pk]=e[0]; mkchip(c,pk,e[0],true); } }); });
  } else { const pp=secPage('pinout'); if(pp) mkchip(c,'Pinout',pp,true); }
  [['abs-max','Abs-max'],['typical-app','Typical app'],['layout','Layout']].forEach(function(s){
    const pg=secPage(s[0]); if(pg) mkchip(c,s[1],pg,false); });
}
function toggleToc(e){ if(e) e.stopPropagation();
  if(tocOpen){ closeToc(); } else { tocEl.classList.add('open'); tocOpen=true; } }
function closeToc(){ tocEl.classList.remove('open'); tocOpen=false; }
document.addEventListener('click',function(e){ if(tocOpen && !e.target.closest('.dd')) closeToc(); });
async function load(i){ idx=i; meta=null; document.getElementById('chips').innerHTML='';
  document.getElementById('sres').innerHTML=''; document.getElementById('q').value='';
  setMsg('loading…');
  try{ meta=await (await fetch('/api/datasheet?ds='+i)).json(); }
  catch(e){ setMsg('could not read datasheet'); return; }
  page = forcePage ? Math.max(1,Math.min(forcePage,meta.n)) : lastPage();
  forcePage=0; setMsg(''); buildToc(); buildChips(); paint();
}
async function init(){
  let d; try{ d=await (await fetch('/api/datasheets')).json(); }catch(e){ setMsg('error loading datasheets'); return; }
  d.items.forEach(function(it){ const o=document.createElement('option'); o.value=it.i; o.textContent=it.name; sel.appendChild(o); });
  sel.onchange=function(){ load(+sel.value); };
  if(!d.poppler){ setMsg('datasheet rendering needs poppler-utils (brew install poppler)'); return; }
  if(!d.items.length){ setMsg('no datasheets in the repo'); return; }
  let start=d.items[0].i;
  // Deep-link from the Parts tab: ?ds=<idx>&page=<n> wins over the remembered doc.
  const pds=PARAMS.get('ds'), ppg=PARAMS.get('page');
  if(pds!==null && d.items.some(function(x){ return x.i===+pds; })){
    start=+pds; if(ppg) forcePage=+ppg;
  } else {
    try{ const last=localStorage.getItem('dsv:lastdoc');
      if(last){ const it=d.items.find(function(x){ return x.name===last; }); if(it) start=it.i; } }catch(e){}
  }
  sel.value=start; load(start);
}
init();
</script>
</body></html>"""


# "Parts" tab — the BOM/work-list joined to ingested datasheets. Each part shows its MPN /
# value / LCSC / source, and (when a datasheet is linked) inline pinout-package thumbnails
# from the ingest index, chips that deep-link into the Datasheets tab at the pinout page, and
# an on-demand DRAFT pin table (kb datasheet pins) for the human to verify against the figure.
PARTS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
 html,body{margin:0;height:100%;background:#1b1c1f;color:#d4d7dd;
   font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;display:flex;flex-direction:column}
 #bar{position:sticky;top:0;z-index:10;display:flex;gap:12px;align-items:center;flex-wrap:wrap;
   padding:9px 14px;background:#16171a;border-bottom:1px solid #34363b}
 #bar .lbl{color:#8a8f98} .cov b{color:#d4d7dd}
 .cov .ok{color:#5fd38d} .cov .miss{color:#ff7b7b}
 #q{background:#2d2f34;color:#d4d7dd;border:1px solid #34363b;border-radius:6px;
   padding:6px 10px;font:inherit;width:150px}
 label{color:#8a8f98;cursor:pointer;user-select:none}
 #list{flex:1;min-height:0;overflow:auto;padding:12px;display:grid;gap:10px;
   grid-template-columns:repeat(auto-fill,minmax(330px,1fr));align-content:start}
 .card{background:#232427;border:1px solid #34363b;border-left:3px solid #34363b;
   border-radius:8px;padding:10px 12px}
 .card.has{border-left-color:#5fd38d} .card.no{border-left-color:#4a2a2a}
 .pn{font-weight:700;font-size:14px} .val{color:#8a8f98}
 .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:baseline;margin-bottom:4px}
 .src{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#7fa8d8;
   background:#1e2a38;border-radius:10px;padding:1px 7px}
 .lcsc{color:#8a8f98;font-size:12px}
 .dsname{color:#8a8f98;font-size:12px;margin:2px 0 6px;word-break:break-all}
 .thumbs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
 .thumb{border:1px solid #34363b;border-radius:5px;background:#11141a;cursor:pointer;
   width:84px;height:64px;object-fit:contain;padding:2px}
 .thumb:hover{border-color:#7fa8d8}
 .pcap{font-size:10px;color:#8a8f98;text-align:center;width:84px;margin-top:-4px}
 .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
 .chip{background:#2d2f34;border:1px solid #34363b;border-radius:13px;padding:4px 10px;
   color:#d4d7dd;font:inherit;font-size:12px;cursor:pointer}
 .chip.pin{border-color:#7fa8d8;color:#cfe0e5} .chip:active{background:#383b41}
 .chip:disabled{opacity:.5;cursor:default}
 .miss-hint{color:#8a8f98;font-size:12px;margin-top:4px}
 .miss-hint code{background:#11141a;padding:1px 5px;border-radius:4px;color:#cfe0e5}
 .pins{margin-top:8px;border-top:1px dashed #34363b;padding-top:6px}
 .pins .conf{font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .pins .medium{color:#5fd38d} .pins .low{color:#ffcc66} .pins .none{color:#ff7b7b}
 .pins table{border-collapse:collapse;margin:4px 0;font-size:12px}
 .pins td{padding:1px 8px 1px 0} .pins .num{color:#7fa8d8;text-align:right}
 .pins .et{color:#8a8f98}
 .pins .warn{color:#ffcc66;font-size:11px}
 #lb{position:fixed;inset:0;z-index:50;background:#000c;display:none;align-items:center;
   justify-content:center;padding:20px}
 #lb.open{display:flex} #lb img{max-width:96vw;max-height:92vh;border-radius:6px;background:#fff}
 #empty{color:#8a8f98;padding:30px;text-align:center}
</style></head><body>
<div id="bar"><span class="lbl">Parts</span>
  <span class="cov" id="cov">…</span>
  <span style="flex:1"></span>
  <label><input type="checkbox" id="missOnly"> missing only</label>
  <input id="q" type="search" placeholder="filter parts…">
</div>
<div id="list"><div id="empty">loading parts…</div></div>
<div id="lb" onclick="this.classList.remove('open')"><img id="lbimg" alt="pinout"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let DATA=null;
const SECLABEL={"abs-max":"Abs-max","typical-app":"Typical app","layout":"Layout"};

function figURL(slug,path){ return '/preview/ds-figure.png?slug='+encodeURIComponent(slug)+'&path='+encodeURIComponent(path); }
function openDS(dsidx,page){ if(dsidx==null) return;
  if(window.parent && window.parent.openDatasheet) window.parent.openDatasheet(dsidx,page||1); }
function lightbox(url){ $('lbimg').src=url; $('lb').classList.add('open'); }

async function togglePins(slug,pkg,box,btn){
  if(box.dataset.open==='1'){ box.style.display='none'; box.dataset.open='0'; return; }
  if(box.dataset.loaded==='1'){ box.style.display='block'; box.dataset.open='1'; return; }
  btn.disabled=true; box.style.display='block'; box.dataset.open='1'; box.innerHTML='extracting…';
  let r; try{ r=await (await fetch('/api/part/pins?slug='+encodeURIComponent(slug)
        +(pkg?'&package='+encodeURIComponent(pkg):''))).json(); }
  catch(e){ box.innerHTML='<span class="warn">extraction failed</span>'; btn.disabled=false; return; }
  btn.disabled=false; box.dataset.loaded='1';
  let h='<span class="conf '+r.confidence+'">pin draft · '+r.confidence
       +(r.package?' · '+esc(r.package):'')+'</span>';
  if(r.pins&&r.pins.length){ h+='<table>'+r.pins.map(p=>'<tr><td class="num">'+esc(p.number)
       +'</td><td>'+esc(p.name)+'</td><td class="et">'+esc(p.etype)+'</td></tr>').join('')+'</table>'; }
  (r.warnings||[]).forEach(w=>{ h+='<div class="warn">! '+esc(w)+'</div>'; });
  box.innerHTML=h;
}

function pinoutPage(ds){ if(ds.pinouts&&ds.pinouts.length) return ds.pinouts[0].page;
  return (ds.sections&&ds.sections.pinout)||1; }

function card(p){
  const el=document.createElement('div'); el.className='card '+(p.datasheet?'has':'no');
  const ds=p.datasheet;
  let h='<div class="meta"><span class="pn">'+esc(p.part)+'</span>'+
    (p.value?'<span class="val">'+esc(p.value)+'</span>':'')+
    '<span style="flex:1"></span><span class="src">'+esc(p.source)+'</span></div>';
  if(p.lcsc && p.lcsc!==p.part) h+='<div class="lcsc">LCSC '+esc(p.lcsc)+'</div>';
  if(ds){
    h+='<div class="dsname">'+esc(ds.pdf)+(ds.page_count?' · '+ds.page_count+'p':'')+'</div>';
    const thumbs=(ds.pinouts||[]).filter(f=>f.path);
    if(thumbs.length){ h+='<div class="thumbs">'+thumbs.map(f=>
        '<div><img class="thumb" loading="lazy" src="'+figURL(ds.slug,f.path)+'" '+
        'data-full="'+figURL(ds.slug,f.path)+'" title="'+esc(f.package||'')+' p'+f.page+'">'+
        '<div class="pcap">'+esc(f.package||('p'+f.page))+'</div></div>').join('')+'</div>'; }
    else if(ds.sections&&ds.sections.pinout&&ds.dsidx!=null){   // no captioned figure → render the pinout PAGE
        const pg=ds.sections.pinout, u='/preview/datasheet.png?ds='+ds.dsidx+'&page='+pg;
        h+='<div class="thumbs"><div><img class="thumb" loading="lazy" src="'+u+'" '+
           'data-full="'+u+'" title="pinout p'+pg+'"><div class="pcap">Pinout p'+pg+'</div></div></div>'; }
    h+='<div class="chips">'+
       '<button class="chip pin" data-act="ds" data-i="'+ds.dsidx+'" data-pg="'+pinoutPage(ds)+'">Datasheet ↗</button>'+
       '<button class="chip" data-act="pins" data-slug="'+esc(ds.slug)+'" data-pkg="'+esc((ds.packages&&ds.packages.length===1)?ds.packages[0]:'')+'">Pins ▾</button>';
    ['abs-max','typical-app','layout'].forEach(k=>{ const pg=ds.sections&&ds.sections[k];
       if(pg) h+='<button class="chip" data-act="ds" data-i="'+ds.dsidx+'" data-pg="'+pg+'">'+SECLABEL[k]+'</button>'; });
    h+='</div><div class="pins" style="display:none"></div>';
  } else {
    h+='<div class="miss-hint">no local datasheet · <code>kb datasheet fetch '+esc(p.part)+'</code></div>';
  }
  el.innerHTML=h;
  el.querySelectorAll('.thumb').forEach(im=>im.onclick=()=>lightbox(im.dataset.full));
  el.querySelectorAll('[data-act=ds]').forEach(b=>b.onclick=()=>openDS(+b.dataset.i,+b.dataset.pg));
  const pb=el.querySelector('[data-act=pins]');
  if(pb){ const box=el.querySelector('.pins');
    pb.onclick=()=>togglePins(pb.dataset.slug,pb.dataset.pkg,box,pb); }
  return el;
}

function render(){
  const q=$('q').value.trim().toLowerCase(), miss=$('missOnly').checked;
  const list=$('list'); list.innerHTML='';
  let shown=0;
  DATA.parts.forEach(p=>{
    if(miss && p.datasheet) return;
    if(q && !((p.part+' '+(p.value||'')+' '+(p.lcsc||'')).toLowerCase().includes(q))) return;
    list.appendChild(card(p)); shown++;
  });
  if(!shown){ const e=document.createElement('div'); e.id='empty';
    e.textContent='no parts match'; list.appendChild(e); }
}
async function load(){
  let d; try{ d=await (await fetch('/api/parts')).json(); }
  catch(e){ $('list').innerHTML='<div id="empty">could not load parts</div>'; return; }
  DATA=d; const c=d.coverage;
  $('cov').innerHTML='<b>'+c.total+'</b> parts · <span class="ok"><b>'+c.with_ds+
    '</b> with datasheet</span> · <span class="miss"><b>'+c.missing.length+'</b> missing</span>';
  render();
}
$('q').oninput=render; $('missOnly').onchange=render;
load();
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
        # Datasheet viewer: per-PDF meta (parsed TOC + page count), parsed once
        self._ds_meta: dict = {}                   # str(pdf) -> {"name","n","toc","figs"}
        self._ds_text: dict = {}                   # str(pdf) -> page texts (for in-doc search)
        # Parts tab: work-list⋈datasheets overview, cached by BOM+index mtime
        self._parts_cache: dict | None = None
        self._parts_sig_val = None

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


    # ---- datasheet viewer (reuses kb datasheet: list / TOC / page render, all cached) ----
    def datasheet_list(self) -> list[Path]:
        return dsmod._datasheet_pdfs(self.cfg.root)

    def datasheet_meta(self, idx: int):
        pdfs = self.datasheet_list()
        if not (0 <= idx < len(pdfs)):
            return None
        pdf = pdfs[idx]
        key = str(pdf)
        with self.lock:
            m = self._ds_meta.get(key)
        if m is not None:
            return (pdf, m)
        # Prefer the committed index (no poppler needed); fall back to a live parse.
        man = dsmod._load_index(self.cfg.root, pdf)
        if man is not None:
            m = {"name": str(dsmod._rel(pdf, self.cfg.root)),
                 "n": man.get("page_count", 1),
                 "toc": [tuple(r) for r in man.get("toc", [])],
                 "figs": [(pg, pks) for pg, pks in man.get("pinout_figures", [])],
                 "sections": man.get("sections", {})}
        elif shutil.which("pdftotext"):
            pages = dsmod._page_texts(pdf)
            n = len(pages)
            if n and not pages[-1].strip():
                n -= 1
            loc = dsmod._locate_pages(pages)
            toc_idx = dsmod._toc_pages(loc) | dsmod._leader_toc_pages(pages)
            sections = {k: [p for p in loc[k] if p not in toc_idx]
                        for k in ("pinout", "abs-max", "typical-app", "layout")}
            m = {"name": str(dsmod._rel(pdf, self.cfg.root)),
                 "n": max(n, 1),
                 "toc": dsmod._parse_toc(pages),
                 "figs": dsmod._pinout_figures(pages),
                 "sections": sections}
            with self.lock:
                self._ds_text[key] = pages
        else:
            return None
        with self.lock:
            self._ds_meta[key] = m
        return (pdf, m)

    def _ds_pages(self, pdf: Path) -> list[str]:
        """Per-page text, preferring the committed index (no poppler) over a live parse."""
        key = str(pdf)
        with self.lock:
            pages = self._ds_text.get(key)
        if pages is not None:
            return pages
        full = dsmod._index_dir(self.cfg.root, pdf) / "text" / "full.txt"
        if full.is_file():
            pages = full.read_text().split("\f")
        elif shutil.which("pdftotext"):
            pages = dsmod._page_texts(pdf)
        else:
            pages = []
        with self.lock:
            self._ds_text[key] = pages
        return pages

    def datasheet_search(self, idx: int, q: str) -> list[int]:
        q = (q or "").strip()
        if len(q) < 2:
            return []
        got = self.datasheet_meta(idx)
        if not got:
            return []
        pdf, _ = got
        ql = q.lower()
        return [i + 1 for i, t in enumerate(self._ds_pages(pdf)) if ql in t.lower()]

    def datasheet_png(self, idx: int, page: int, dpi: int = 150):
        if not shutil.which("pdftoppm"):
            return None
        got = self.datasheet_meta(idx)
        if not got:
            return None
        pdf, m = got
        page = max(1, min(int(page), m["n"]))
        try:
            out = dsmod._render(pdf, page, dpi, self.cfg.root / dsmod.CACHE_DIRNAME)
            return out.read_bytes()
        except Exception:  # noqa: BLE001
            return None

    def datasheet_pdf_bytes(self, idx: int):
        pdfs = self.datasheet_list()
        if not (0 <= idx < len(pdfs)):
            return None
        try:
            return pdfs[idx].read_bytes()
        except OSError:
            return None

    # ---- Parts tab: BOM/work-list joined to ingested datasheets (pinouts per part) ----
    def _parts_sig(self):
        """Cheap change signal: BOM mtime + index-root mtime (bumps on ingest/fetch)."""
        bom = self.cfg.bom
        ir = dsmod._index_root(self.cfg.root)
        bm = bom.stat().st_mtime if bom and bom.exists() else 0
        im = ir.stat().st_mtime if ir.exists() else 0
        return (bm, im)

    def parts(self) -> dict:
        sig = self._parts_sig()
        with self.lock:
            if self._parts_cache is not None and self._parts_sig_val == sig:
                return self._parts_cache
        ov = dsmod.parts_overview(self.cfg)
        # Attach each linked datasheet's index in the Datasheets-tab list, for deep-linking.
        idx_by_slug = {dsmod._slug(p): i for i, p in enumerate(self.datasheet_list())}
        for p in ov["parts"]:
            if p["datasheet"]:
                p["datasheet"]["dsidx"] = idx_by_slug.get(p["datasheet"]["slug"])
        with self.lock:
            self._parts_cache, self._parts_sig_val = ov, sig
        return ov

    def ds_figure(self, slug: str, relpath: str):
        """Bytes of a pre-rendered index figure PNG (figures/…), or None. Path-guarded:
        the slug must be a real index dir and relpath must stay under its figures/."""
        if not re.fullmatch(r"[A-Za-z0-9._]+", slug or ""):
            return None
        idir = dsmod._index_root(self.cfg.root) / slug
        target = (idir / relpath).resolve()
        figdir = (idir / "figures").resolve()
        if not str(target).startswith(str(figdir) + os.sep) or not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None

    def part_pins(self, slug: str, package: str | None):
        """Best-effort DRAFT pin table for one ingested datasheet (by index slug)."""
        man = next((m for d, m in dsmod._index_entries(self.cfg.root) if d.name == slug), None)
        if not man:
            return None
        pages = dsmod._cached_or_live_pages(self.cfg.root, self.cfg.root / man["pdf"])
        return dsmod.extract_pin_table(pages, man.get("sections", {}).get("pinout", []), package)


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

        def _qint(self, name, default=-1):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            try:
                return int(parse_qs(q).get(name, [str(default)])[0])
            except ValueError:
                return default

        def _qstr(self, name, default=""):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            return parse_qs(q).get(name, [default])[0]

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
            elif path == "/preview/schematic":
                self._send(200, SCHEMATIC_PAGE.encode(), "text/html; charset=utf-8")
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
            elif path == "/preview/parts":
                self._send(200, PARTS_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/parts":
                self._send(200, json.dumps(state.parts()).encode(), "application/json")
            elif path == "/api/part/pins":
                r = state.part_pins(self._qstr("slug"), self._qstr("package") or None)
                if r is None:
                    self._send(404, b"no such ingested datasheet", "text/plain")
                else:
                    self._send(200, json.dumps(r).encode(), "application/json")
            elif path == "/preview/ds-figure.png":
                data = state.ds_figure(self._qstr("slug"), self._qstr("path"))
                if not data:
                    self._send(404, b"figure not rendered on this host", "text/plain")
                else:
                    self._send(200, data, "image/png")
            elif path == "/preview/datasheets":
                self._send(200, DATASHEETS_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/datasheets":
                pdfs = state.datasheet_list()
                body = {"poppler": bool(shutil.which("pdftoppm")),
                        "items": [{"i": i, "name": str(dsmod._rel(p, state.cfg.root))}
                                  for i, p in enumerate(pdfs)]}
                self._send(200, json.dumps(body).encode(), "application/json")
            elif path == "/api/datasheet":
                got = state.datasheet_meta(self._qint("ds"))
                if not got:
                    self._send(404, b"no such datasheet", "text/plain")
                else:
                    self._send(200, json.dumps(got[1]).encode(), "application/json")
            elif path == "/api/datasheet/search":
                res = state.datasheet_search(self._qint("ds"), self._qstr("q"))
                self._send(200, json.dumps({"pages": res}).encode(), "application/json")
            elif path == "/preview/datasheet.png":
                data = state.datasheet_png(self._qint("ds"), self._qint("page", 1),
                                           self._qint("dpi", 150))
                if not data:
                    self._send(404, b"datasheet page not available", "text/plain")
                else:
                    self._send(200, data, "image/png")
            elif path == "/preview/datasheet.pdf":
                data = state.datasheet_pdf_bytes(self._qint("ds"))
                if not data:
                    self._send(404, b"no such datasheet", "text/plain")
                else:
                    self._send(200, data, "application/pdf")
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
