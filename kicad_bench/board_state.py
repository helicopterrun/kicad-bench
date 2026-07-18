"""board_state — per-board live state: audit + preview + datasheet + parts caches.

One `BoardState` owns everything the dashboard shows for a single board (a resolved
`Config`): the whole-repo audit, the schematic-PDF / 2D-SVG / per-layer / 3D-PNG previews,
the datasheet viewer index, and the BOM⋈datasheet parts overview. Every result is cached
in-process and keyed by a file mtime, guarded by one `threading.Lock`, so opening several
tabs (or polling) never triggers a redundant `kicad-cli` run. Read-only — it never writes a
design file.

This was extracted from `sidecar.py` so the legacy stdlib `kb sidecar` and the FastAPI
cockpit (`kp cockpit`) share ONE implementation. It must stay free of any `kicad_parts`
import so `kicad_bench` remains a leaf the cockpit can depend on (no dependency cycle).

Discipline preserved from the original:
  * the ~2s 2D SVG renders OUTSIDE the lock, so it never stalls audit/mtime requests;
  * the ~30s 3D raytrace runs on a daemon thread and NEVER blocks a request — callers poll
    `pcb_3d_state()` for ready|rendering|error|none and read the PNG when ready.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from .core import cli
from .core import config as cfgmod
from .core import pcbgeom
from . import audit
from . import bom_assembly as bommod
from . import datasheet as dsmod


class BoardState:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache: dict | None = None
        self.cache_mtime = None
        self._audit_running = False        # background audit in flight?
        self._audit_error: str | None = None
        self._audit_hydrated = False       # loaded the disk-persisted result yet?
        self._review_running = False       # background LLM review in flight?
        self._review_error: str | None = None
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
        # BOM tab: sch⋈pcb⋈bom⋈alternates assembly, cached by sch+pcb+bom+alt mtime
        self._bom_cache: dict | None = None
        self._bom_sig_val = None
        # PCB placements (pin-1 orientation), cached by board mtime
        self._placements: dict | None = None
        self._placements_mtime = None

    def _mtime(self):
        p = self.cfg.pcb
        return p.stat().st_mtime if p and p.exists() else 0

    def _compute_audit(self) -> dict:
        return audit.sections_to_dict(audit.run_all(self.cfg), self.cfg)

    def _audit_sig(self) -> list:
        """What the audit is cached against: [pcb mtime, newest sch mtime]. DRC reads the
        PCB and ERC the schematic, so a change to either makes the result stale."""
        return [self._mtime(), self.sch_mtime()]

    def _audit_disk(self) -> Path:
        return self.cfg.root / ".audit-cache" / "result.json"

    def _hydrate_audit(self):
        """Load the last persisted audit into memory once, so a PASS/FAIL survives a
        restart (shown 'stale' if the board changed since). Call under the lock."""
        if self._audit_hydrated:
            return
        self._audit_hydrated = True
        if self.cache is not None:
            return
        try:
            rec = json.loads(self._audit_disk().read_text())
        except (OSError, ValueError):
            return
        if isinstance(rec, dict) and rec.get("data") is not None:
            self.cache, self.cache_mtime = rec["data"], rec.get("sig")

    def _save_audit(self, sig, data):
        """Persist the result (gitignored, regenerable) so it survives a restart."""
        d = self._audit_disk().parent
        try:
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                (d / ".gitignore").write_text("*\n")     # never committed
            tmp = d / "result.json.tmp"
            tmp.write_text(json.dumps({"sig": sig, "data": data}))
            tmp.replace(self._audit_disk())
        except OSError:
            pass

    def audit(self, force: bool) -> dict:
        """Blocking audit. Runs kicad-cli DRC/ERC OUTSIDE the lock so a ~19s audit never
        stalls other requests that need the board state."""
        sig = self._audit_sig()
        with self.lock:
            self._hydrate_audit()
            if not force and self.cache is not None and self.cache_mtime == sig:
                return self.cache
        data = self._compute_audit()
        with self.lock:
            self.cache, self.cache_mtime = data, sig
        self._save_audit(sig, data)
        return data

    def audit_peek(self) -> dict:
        """Report the last audit WITHOUT triggering a run — for on-demand auditing, so a
        page load never spawns kicad-cli. status: ready | stale | running | error | none.
        'stale' means the board changed since the cached result (offer a re-run)."""
        sig = self._audit_sig()
        with self.lock:
            self._hydrate_audit()
            if self._audit_running:
                return {"status": "running"}
            if self.cache is not None:
                fresh = self.cache_mtime == sig
                return {"status": "ready" if fresh else "stale", "stale": not fresh, **self.cache}
            if self._audit_error is not None:
                return {"status": "error", "error": self._audit_error}
            return {"status": "none"}

    def audit_state(self, force: bool = False) -> dict:
        """Kick off the audit on a background daemon thread (mirrors pcb_3d_state) and
        report {status:'running'}; the client polls audit_peek until it flips to ready."""
        sig = self._audit_sig()
        with self.lock:
            self._hydrate_audit()
            if not force and self.cache is not None and self.cache_mtime == sig:
                return {"status": "ready", **self.cache}
            if self._audit_running:
                return {"status": "running"}
            self._audit_running = True
            self._audit_error = None
        threading.Thread(target=self._audit_bg, args=(sig,), daemon=True).start()
        return {"status": "running"}

    def _audit_bg(self, sig):
        err = data = None
        try:
            data = self._compute_audit()
        except Exception as e:  # noqa: BLE001 — surface the failure, never crash the thread
            err = str(e)
        with self.lock:
            if data is not None:
                self.cache, self.cache_mtime, self._audit_error = data, sig, None
            else:
                self._audit_error = err
            self._audit_running = False
        if data is not None:
            self._save_audit(sig, data)      # outside the lock — disk IO

    def changes(self) -> dict:
        """Recent design history for the sidecar Changes tab: the project's last
        git commits plus the most recent `ksir compile` netlist diff (written to
        <repo>/output/ksir_last_diff.txt). Lets the human see WHAT the assistant
        changed semantically, not just a refreshed picture."""
        import subprocess
        base = (self.cfg.root_sch or self.cfg.pcb)
        if not base:
            return {"commits": [], "last_diff": ""}
        wd = str(base.parent)
        commits: list[dict] = []
        try:
            r = subprocess.run(
                ["git", "log", "-15", "--pretty=%h%x00%cr%x00%s"],
                capture_output=True, text=True, cwd=wd, timeout=10)
            for line in r.stdout.splitlines():
                parts = line.split("\x00")
                if len(parts) == 3:
                    commits.append({"hash": parts[0], "when": parts[1],
                                    "subject": parts[2]})
        except Exception:  # noqa: BLE001 — the tab must never break the dashboard
            pass
        last_diff = ""
        try:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, cwd=wd, timeout=10)
            top = Path(r.stdout.strip()) if r.returncode == 0 else base.parent
            f = top / "output" / "ksir_last_diff.txt"
            if f.exists():
                age = time.time() - f.stat().st_mtime
                last_diff = f.read_text()[:20000]
                last_diff = f"# written {int(age // 60)} min ago\n{last_diff}"
        except Exception:  # noqa: BLE001
            pass
        return {"commits": commits, "last_diff": last_diff}

    # -- datasheet review (kb review) — clone of the audit peek/state/bg trio --

    def _review_disk(self) -> Path:
        # committed review artifact (written by review.run_review), unlike the
        # regenerable .audit-cache — page-cited findings travel with the design
        return self.cfg.root / ".cockpit" / "review.json"

    def review_peek(self) -> dict:
        """Report the last review WITHOUT triggering a run.
        status: ready | stale | running | error | none."""
        with self.lock:
            if self._review_running:
                return {"status": "running"}
            if self._review_error is not None:
                return {"status": "error", "error": self._review_error}
        p = self._review_disk()
        if not p.is_file():
            return {"status": "none"}
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return {"status": "error", "error": "unreadable review.json"}
        fresh = abs(rec.get("sch_mtime", 0) - self.sch_mtime()) < 1e-6
        return {"status": "ready" if fresh else "stale", "stale": not fresh, **rec}

    def review_state(self, force: bool = False) -> dict:
        """Kick off `kb review` on a background daemon thread; the client polls
        review_peek until it flips to ready. Needs the llm extra + API key —
        failures surface as status:error, never crash the thread."""
        peek = self.review_peek()
        if not force and peek["status"] == "ready":
            return peek
        with self.lock:
            if self._review_running:
                return {"status": "running"}
            self._review_running = True
            self._review_error = None
        threading.Thread(target=self._review_bg, daemon=True).start()
        return {"status": "running"}

    def _review_bg(self):
        err = None
        try:
            import anthropic
            from . import review as reviewmod
            client = anthropic.Anthropic()
            reviewmod.run_review(self.cfg, client, reviewmod.review_model(self.cfg),
                                 progress=lambda *a: None)
        except Exception as e:  # noqa: BLE001 — surface, never crash the thread
            err = str(e)
        with self.lock:
            self._review_error = err
            self._review_running = False

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

    # ---- BOM tab: sch⋈pcb⋈bom⋈alternates assembly + pin-1 orientation ----
    def _bom_sig(self):
        """Change signal: schematic + PCB + BOM + alternates-file mtimes. Editing any
        source (or an alternate) invalidates the assembled BOM."""
        def mt(p):
            return p.stat().st_mtime if p and p.exists() else 0
        alt = bommod._alternates_path(self.cfg.root)
        return (self.sch_mtime(), self._mtime(), mt(self.cfg.bom), mt(alt))

    def bom(self) -> dict:
        """Assembled, value-checked, alternate-annotated BOM (see bom_assembly.build).
        Cached by the combined source mtimes; the ~1s netlist export runs outside the
        lock so it never stalls other tabs."""
        sig = self._bom_sig()
        with self.lock:
            if self._bom_cache is not None and self._bom_sig_val == sig:
                return self._bom_cache
        data = bommod.build(self.cfg)
        with self.lock:
            self._bom_cache, self._bom_sig_val = data, sig
        return data

    def placements(self) -> dict:
        """ref -> pcbgeom.Placement, board-mtime cached (shared by the BOM tab's
        orientation SVGs). Empty dict if there's no board or the parse fails."""
        m = self._mtime()
        with self.lock:
            if self._placements is not None and self._placements_mtime == m:
                return self._placements
        pcb = self.cfg.pcb
        try:
            pls = pcbgeom.parse(pcb).placements if (pcb and pcb.exists()) else {}
        except Exception:  # noqa: BLE001 — a parse failure must not break the tab
            pls = {}
        with self.lock:
            self._placements, self._placements_mtime = pls, m
        return pls

    def orient_svg(self, ref: str) -> str | None:
        """The pin-1 orientation SVG for one designator, or None if it isn't placed."""
        pl = self.placements().get(ref)
        return bommod.orient_svg(pl) if pl else None

    def bom_csv(self) -> str:
        """Fab-ready assembly BOM CSV (with an Approved Alternates column)."""
        return bommod.export_csv(self.cfg)

    def set_alternates(self, key: str, alternates: list) -> dict:
        """Persist one line's user alternates to .cockpit/bom_alternates.json and drop
        the BOM cache so the next read reflects the change."""
        bommod.write_alternates(self.cfg.root, key, alternates)
        with self.lock:
            self._bom_cache = self._bom_sig_val = None
        return self.bom()

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
