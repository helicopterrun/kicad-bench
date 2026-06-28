"""datasheet — harvest, ingest, and read datasheets without re-parsing PDFs.

A PDF is opened (via poppler) exactly ONCE, at `ingest`, which extracts each kind of data
into its own cheap file under `.datasheet-index/<slug>/`:
  * text/full.txt + text/pNNN.txt — full and per-page text (committed)
  * index.json — metadata + parsed TOC + section→page map + the figure list (committed)
  * figures/*.png — the pinout / typical-app / layout DIAGRAMS, pre-rendered under
    meaningful names (gitignored — regenerable). An inner `.gitignore` keeps text+JSON
    in git but figures/ and the download scratch dir out.

Afterwards the read commands touch only those artifacts — never the PDF — so they cost no
PDF parsing, and the text/JSON path needs no poppler at all (works on a fresh clone / in CI):
  * `search <text>` greps the per-page text,
  * `figures <part>` prints the pre-rendered diagram PNG paths for the MAIN (vision) agent,
  * `locate` / `toc` / `view` prefer the committed index, falling back to live `pdftotext`
    for an un-ingested ad-hoc PDF.

`fetch` automates harvesting: it resolves a datasheet URL (a `[datasheets]` override in
kicad-bench.toml, a KiCad symbol Datasheet field, a BOM column, or `--url` from a web
search), downloads the PDF into `Datasheets/` via stdlib urllib (honoring HTTPS_PROXY),
validates the %PDF header, then auto-ingests. `registry.json` records part→datasheet
provenance. Writes only ever land in `Datasheets/` (source) or the regenerable
`.datasheet-index/` / `.datasheet-cache/` — design files are never touched.

  kb datasheet fetch tps65150             # find + download + ingest a part's datasheet
  kb datasheet fetch --all                # every part with a known URL (symbol/BOM/override)
  kb datasheet ingest --all               # (re)build the index for PDFs already on disk
  kb datasheet search "soft-start"        # grep ingested text — no PDF, no poppler
  kb datasheet figures tps65150 --section pinout --package LQFP48   # pre-rendered PNG path
  kb datasheet toc tps65150               # the document's own table of contents
  kb datasheet locate tps65150            # section -> page (pinout/abs-max/…)
  kb datasheet view /tmp/szza036c.pdf --toc 3.2     # query can be a direct PDF path

`ingest`/`fetch` require poppler-utils (pdftotext/pdftoppm): apt-get install poppler-utils /
brew install poppler. The cheap read commands (`search`, `figures`) do not.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from .core import config as cfgmod

CACHE_DIRNAME = ".datasheet-cache"
INDEX_DIRNAME = ".datasheet-index"     # committed text+JSON; figures/ self-gitignored
MAX_VIEW_PAGES = 8

# Bump when the extraction logic changes so `ingest` knows to re-build stale indexes.
TOOL_VERSION = 1
FIGURE_DPI = 150                       # default render DPI for pre-built figure PNGs
MAX_FIGS_PER_KIND = 3                  # cap typical-app / layout pages rendered per doc
_HTTP_UA = "Mozilla/5.0 (compatible; kicad-bench datasheet fetch)"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# Section -> locator regex (case-insensitive, matched against each page's text).
SECTION_PATTERNS = {
    "pinout":      r"pin (configuration|function|assignment|description)s?|pin layout|terminal (configuration|function)",
    "abs-max":     r"absolute maximum",
    "typical-app": r"typical application|application (information|circuit|schematic)",
    "layout":      r"layout (guideline|consideration|example|recommendation)|recommended.*layout|pcb layout|land pattern|package outline",
}


def _need(tool: str) -> None:
    if not shutil.which(tool):
        sys.exit(f"error: '{tool}' not found — install poppler-utils "
                 f"(apt-get install poppler-utils / brew install poppler)")


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]", "", s.lower())


def _datasheet_pdfs(root: Path) -> list[Path]:
    """Local datasheet PDFs: Datasheets/, Imported CAD/<#>/datasheet.pdf, and repo-root
    brief(s). Skips the render cache and the regenerable output/ dir."""
    out: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)

    if (root / "Datasheets").is_dir():
        for p in sorted((root / "Datasheets").glob("*.pdf")):
            add(p)
    if (root / "Imported CAD").is_dir():
        for p in sorted((root / "Imported CAD").glob("*/datasheet.pdf")):
            add(p)
    for p in sorted(root.glob("*.pdf")):
        add(p)
    return out


def _match(query: str, pdfs: list[Path]) -> list[Path]:
    q = _norm(query)
    exact = [p for p in pdfs if _norm(p.stem) == q]
    if exact:
        return exact                   # an exact stem match wins over substring hits
    return [p for p in pdfs if q in _norm(p.parent.name + p.stem)]


def _page_texts(pdf: Path) -> list[str]:
    _need("pdftotext")
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                       capture_output=True, text=True)
    return r.stdout.split("\f")


# A package-pinout FIGURE caption, e.g. "Figure 7. LQFP48 package pinout" -> "LQFP48".
# These pages hold the actual pin-placement DIAGRAM, so rank them above the multi-page
# pin-description TABLE (which only matches the generic "pin description" wording).
_FIG_PINOUT = re.compile(
    r"figure\s+\d+\s*[.:]?\s*([A-Za-z0-9/_+\- ]+?)\s*(?:package\s+)?pin[\s-]?out", re.I)
# A real pin-placement diagram page shows a handful of packages; a page that lists this
# many distinct pinout captions is the "List of figures" index, not a diagram — skip it.
_MAX_FIG_PER_PAGE = 4


def _locate_pages(pages: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for name, pat in SECTION_PATTERNS.items():
        rx = re.compile(pat, re.I)
        out[name] = [i + 1 for i, t in enumerate(pages) if rx.search(t)]
    return out


def _locate(pdf: Path) -> dict[str, list[int]]:
    return _locate_pages(_page_texts(pdf))


def _pinout_figures(pages: list[str]) -> list[tuple[int, list[str]]]:
    """Pages carrying a package-pinout FIGURE caption, with the package name(s) on each —
    the diagrams to render, as opposed to the pin-description table pages."""
    out: list[tuple[int, list[str]]] = []
    for i, t in enumerate(pages):
        pkgs: list[str] = []
        for m in _FIG_PINOUT.finditer(t):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .")
            if name and len(name) <= 32 and name.lower() != "package" and name not in pkgs:
                pkgs.append(name)
        if pkgs and len(pkgs) < _MAX_FIG_PER_PAGE:
            out.append((i + 1, pkgs))
    return out


# Table-of-contents line: "<section#> <title> <dotted leaders> <page>". The leader is >=3
# dots, each optionally trailed by spaces, so it matches both TI's "....." and ST's ". . .".
_TOC_HEAD = re.compile(r"^\s*(?:table of )?contents\s*$", re.I)
# One TOC entry: "<section#> <title> <dotted leaders> <page>". Unanchored + finditer so a
# two-column TOC line (TI style, where pdftotext -layout merges both columns) yields BOTH
# entries instead of swallowing the second column's page into the first entry's title.
_TOC_ENTRY = re.compile(r"(\d+(?:\.\d+)*)\s+(.+?)(?:\.[ \t]*){3,}\s*(\d+)")


def _toc_entries(text: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for ln in text.splitlines():
        for m in _TOC_ENTRY.finditer(ln):
            title = re.sub(r"\s+", " ", m.group(2)).strip()
            if title:
                out.append((m.group(1), title, int(m.group(3))))
    return out


def _parse_toc(pages: list[str]) -> list[tuple[str, str, int]]:
    """Parse the document's own table of contents into (section, title, page) rows, sorted by
    section number. Works for any PDF that has one — datasheets AND application notes /
    explainer docs, across manufacturers (TI, ST, …), including TI's two-column TOC layout.
    Page numbers are the printed numbers, which equal the PDF page index in the docs we read."""
    start = None
    for i, t in enumerate(pages[:8]):
        if _TOC_HEAD.search(t) or len(_toc_entries(t)) >= 3:
            start = i
            break
    if start is None:
        return []
    rows: list[tuple[str, str, int]] = []
    for t in pages[start:start + 15]:
        e = _toc_entries(t)
        if not e and rows:
            break                      # past the end of the TOC
        rows += e

    def _key(r):
        try:
            return tuple(int(x) for x in r[0].split("."))
        except ValueError:
            return (9999,)
    rows.sort(key=_key)
    return rows


def _rel(pdf: Path, root: Path):
    try:
        return pdf.relative_to(root)
    except ValueError:
        return pdf


def _resolve_pdfs(query: str, root: Path) -> list[Path]:
    """A direct path to an existing PDF (for ad-hoc app notes / explainer docs), else a
    repo-datasheet name/LCSC# match."""
    p = Path(query).expanduser()
    if p.suffix.lower() == ".pdf" and p.is_file():
        return [p]
    return _match(query, _datasheet_pdfs(root))


def _toc_pages(loc: dict[str, list[int]]) -> set[int]:
    """Pages that match >=3 sections are almost certainly a table-of-contents/index page;
    drop them so --section doesn't render the TOC."""
    c = Counter(p for ps in loc.values() for p in ps)
    return {p for p, n in c.items() if n >= 3}


def _slug(pdf: Path) -> str:
    return _norm(pdf.parent.name + "_" + pdf.stem) or pdf.stem


def _ensure_cache(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    gi = cache / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")          # self-ignore the whole cache (regenerable)


def _render(pdf: Path, page: int, dpi: int, cache: Path) -> Path:
    _need("pdftoppm")
    _ensure_cache(cache)
    outdir = cache / _slug(pdf)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"p{page:03d}@{dpi}.png"
    if target.exists():
        return target                  # idempotent — never render the same page twice
    prefix = outdir / f".tmp-p{page}-{dpi}"
    subprocess.run(["pdftoppm", "-png", "-singlefile", "-f", str(page), "-l", str(page),
                    "-r", str(dpi), str(pdf), str(prefix)],
                   check=True, capture_output=True)
    prefix.with_suffix(".png").replace(target)
    return target


# ── derived index: extract each data type once into its own cheap file ──────────
# A datasheet's PDF is opened (via poppler) exactly once, at `ingest`. Everything an
# agent needs afterwards — full text, per-page text, the parsed TOC, the section→page
# map, and the pinout/typical-app/layout DIAGRAMS as named PNGs — is written under
# `.datasheet-index/<slug>/`. `search`/`figures`/`locate`/`view` then read those
# artifacts and never touch the PDF, so they cost no PDF parsing and (for text/JSON)
# need no poppler at all. text/ + index.json are meant to be committed; figures/ and
# the download scratch dir are regenerable, so an inner .gitignore keeps them out.

def _index_root(root: Path) -> Path:
    return root / INDEX_DIRNAME


def _ensure_index_root(root: Path) -> Path:
    ir = _index_root(root)
    ir.mkdir(parents=True, exist_ok=True)
    gi = ir / ".gitignore"
    if not gi.exists():
        gi.write_text(".tmp/\n*/figures/\n")   # commit text+JSON; ignore PNGs + scratch
    return ir


def _index_dir(root: Path, pdf: Path) -> Path:
    return _index_root(root) / _slug(pdf)


def _load_index(root: Path, pdf: Path) -> dict | None:
    ij = _index_dir(root, pdf) / "index.json"
    if not ij.is_file():
        return None
    try:
        return json.loads(ij.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _index_entries(root: Path) -> list[tuple[Path, dict]]:
    """Every ingested datasheet as (index_dir, manifest). Reads only committed JSON —
    works on a fresh clone with no PDFs and no poppler present."""
    ir = _index_root(root)
    if not ir.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for d in sorted(ir.iterdir()):
        ij = d / "index.json"
        if ij.is_file():
            try:
                out.append((d, json.loads(ij.read_text())))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def _sha256(pdf: Path) -> str:
    h = hashlib.sha256()
    with pdf.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip("_") or "x"


def _real_pages(pdf: Path) -> list[str]:
    """Per-page text with the trailing empty page (pdftotext's final \\f) dropped."""
    pages = _page_texts(pdf)
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def _write_text_artifacts(pages: list[str], idir: Path) -> None:
    tdir = idir / "text"
    tdir.mkdir(parents=True, exist_ok=True)
    for old in tdir.glob("p*.txt"):     # drop stale per-page files on re-ingest
        old.unlink()
    (tdir / "full.txt").write_text("\f".join(pages))
    for i, t in enumerate(pages, 1):
        (tdir / f"p{i:03d}.txt").write_text(t)


def _render_named(pdf: Path, page: int, name: str, dpi: int, root: Path) -> Path:
    """Render `page` (cached in .datasheet-cache via _render) and copy it under a
    meaningful name into the doc's figures/ dir. One pdftoppm path, reused."""
    src = _render(pdf, page, dpi, root / CACHE_DIRNAME)
    fdir = _index_dir(root, pdf) / "figures"
    fdir.mkdir(parents=True, exist_ok=True)
    dest = fdir / name
    shutil.copyfile(src, dest)
    return dest


def _render_figures(pdf: Path, root: Path, sections: dict[str, list[int]],
                    pin_figs: list[tuple[int, list[str]]], dpi: int) -> list[dict]:
    """Pre-render the DIAGRAM pages (pinout per package, typical-app, layout) so an
    agent reads a known PNG instead of the PDF. Skips rendering when pdftoppm is
    absent — text/JSON still build, figures just stay empty until a poppler host
    re-ingests. (abs-max is a table, so it stays in text, not rendered.)"""
    figures: list[dict] = []
    if not shutil.which("pdftoppm"):
        return figures
    for page, pkgs in pin_figs:
        for pk in pkgs:
            name = f"pinout-{_safe(pk)}.png"
            _render_named(pdf, page, name, dpi, root)
            figures.append({"kind": "pinout", "package": pk, "page": page,
                            "path": f"figures/{name}"})
    for kind in ("typical-app", "layout"):
        for page in sections.get(kind, [])[:MAX_FIGS_PER_KIND]:
            name = f"{kind}-p{page:03d}.png"
            _render_named(pdf, page, name, dpi, root)
            figures.append({"kind": kind, "page": page, "path": f"figures/{name}"})
    return figures


def _build_manifest(pdf: Path, root: Path, pages: list[str], source_url: str | None,
                    dpi: int) -> dict:
    loc = _locate_pages(pages)
    toc_idx = _toc_pages(loc)
    sections = {k: [p for p in loc[k] if p not in toc_idx] for k in SECTION_PATTERNS}
    pin_figs = _pinout_figures(pages)
    figures = _render_figures(pdf, root, sections, pin_figs, dpi)
    packages = sorted({pk for _, pks in pin_figs for pk in pks})
    return {
        "schema": 1,
        "tool_version": TOOL_VERSION,
        "pdf": str(_rel(pdf, root)),
        "sha256": _sha256(pdf),
        "mpn": pdf.stem,
        "packages": packages,
        "source_url": source_url,
        "page_count": max(len(pages), 1),
        "toc": [[n, t, pg] for n, t, pg in _parse_toc(pages)],
        "sections": sections,
        "pinout_figures": [[pg, pks] for pg, pks in pin_figs],
        "figures": figures,
        "text": {"full": "text/full.txt", "pages": "text/p%03d.txt"},
    }


def _write_manifest(root: Path, pdf: Path, manifest: dict) -> None:
    idir = _index_dir(root, pdf)
    idir.mkdir(parents=True, exist_ok=True)
    (idir / "index.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def _ingest_one(pdf: Path, root: Path, source_url: str | None = None,
                dpi: int = FIGURE_DPI, force: bool = False) -> dict:
    """Extract one PDF into the derived index. Idempotent: a re-ingest of an unchanged
    file (same sha256 + tool_version) is a no-op unless --force. Returns a status dict."""
    _ensure_index_root(root)
    sha = _sha256(pdf)
    existing = _load_index(root, pdf)
    if (not force and existing
            and existing.get("sha256") == sha
            and existing.get("tool_version") == TOOL_VERSION):
        return {"status": "skipped", "manifest": existing}
    pages = _real_pages(pdf)
    idir = _index_dir(root, pdf)
    _write_text_artifacts(pages, idir)
    # carry a previously-recorded source_url forward when this call didn't supply one
    url = source_url or (existing.get("source_url") if existing else None)
    manifest = _build_manifest(pdf, root, pages, url, dpi)
    _write_manifest(root, pdf, manifest)
    return {"status": "ingested", "manifest": manifest}


# ── parts registry + work-list (which parts need a datasheet, and from where) ────

def _registry_path(root: Path) -> Path:
    return _index_root(root) / "registry.json"


def _load_registry(root: Path) -> dict:
    p = _registry_path(root)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": 1, "parts": {}}


def _save_registry(root: Path, reg: dict) -> None:
    _ensure_index_root(root)
    _registry_path(root).write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n")


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    """Rows of a BOM .xlsx as lists of strings. Prefers openpyxl (as release_prep
    does) but falls back to a stdlib zip+XML reader so ingest never hard-depends on it."""
    try:
        import openpyxl
    except ImportError:
        return _read_xlsx_rows_stdlib(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows: list[list[str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
    return rows


def _read_xlsx_rows_stdlib(path: Path) -> list[list[str]]:
    """Minimal stdlib .xlsx reader: shared strings + cell values, all worksheets."""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows: list[list[str]] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sroot.findall(f"{NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
            for sn in sorted(n for n in names
                             if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
                sd = ET.fromstring(z.read(sn)).find(f"{NS}sheetData")
                if sd is None:
                    continue
                for row in sd.findall(f"{NS}row"):
                    cells: list[str] = []
                    for c in row.findall(f"{NS}c"):
                        v = c.find(f"{NS}v")
                        val = v.text if v is not None and v.text is not None else ""
                        if c.get("t") == "s" and val != "":
                            try:
                                val = shared[int(val)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(val)
                    rows.append(cells)
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        return []
    return rows


def _bom_entries(rows: list[list[str]]) -> list[dict]:
    """Pull {mpn, lcsc, url, value} from BOM rows by matching header column names."""
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]

    def col(*needles: str) -> int | None:
        for i, h in enumerate(header):
            if any(n in h for n in needles):
                return i
        return None

    ci_lcsc = col("lcsc", "jlcpcb part", "supplier part")
    # "part number" (not bare "part #", which also matches an "LCSC Part #" column) and
    # never reuse the LCSC column as the MPN.
    ci_mpn = next((i for i in (col("manufacturer part", "mfr part", "mpn", "part number"),)
                   if i is not None and i != ci_lcsc), None)
    ci_url = col("datasheet")
    ci_val = col("comment", "value", "description")
    out: list[dict] = []
    for r in rows[1:]:
        def g(i: int | None) -> str:
            return r[i].strip() if (i is not None and i < len(r)) else ""
        e = {"mpn": g(ci_mpn), "lcsc": g(ci_lcsc), "url": g(ci_url), "value": g(ci_val)}
        if e["mpn"] or e["lcsc"] or e["url"]:
            out.append(e)
    return out


def _symbol_sheets(cfg: cfgmod.Config) -> list[Path]:
    if cfg.root_sch and cfg.root_sch.exists():
        return sorted(cfg.root_sch.parent.glob("*.kicad_sch"))
    return []


def _toml_overrides(cfg: cfgmod.Config) -> dict[str, str]:
    """Hand-edited [datasheets] section in kicad-bench.toml: part -> URL override."""
    raw = cfg.raw.get("datasheets", {})
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _work_list(cfg: cfgmod.Config) -> list[dict]:
    """Parts that want a datasheet, unioned across sources with provenance. Each entry:
    {part, source, url?, lcsc?}. Sources: [datasheets] toml overrides, KiCad symbol
    Datasheet fields, BOM rows, and per-component `Imported CAD/<part>/` dirs."""
    from .core import schparse
    out: dict[str, dict] = {}

    def add(part: str, source: str, url: str = "", lcsc: str = "") -> None:
        part = part.strip()
        if not part:
            return
        cur = out.setdefault(part, {"part": part, "source": source, "url": "", "lcsc": ""})
        if url and not cur["url"]:
            cur["url"], cur["source"] = url, source
        if lcsc and not cur["lcsc"]:
            cur["lcsc"] = lcsc

    for part, url in _toml_overrides(cfg).items():
        add(part, "toml-override", url=url)
    for val, url in schparse.datasheet_fields(_symbol_sheets(cfg)).items():
        add(val, "symbol-field", url=url)
    if cfg.bom and cfg.bom.exists():
        for e in _bom_entries(_read_xlsx_rows(cfg.bom)):
            add(e["mpn"] or e["lcsc"], "bom", url=e["url"], lcsc=e["lcsc"])
    icad = cfg.root / "Imported CAD"
    if icad.is_dir():
        for d in sorted(icad.iterdir()):
            if d.is_dir():
                add(d.name, "imported-cad")
    return list(out.values())


# ── fetch: resolve a datasheet URL and download it (stdlib urllib, via the proxy) ─

def _resolve_url(query: str, cfg: cfgmod.Config, reg: dict) -> tuple[str | None, str]:
    """Resolve a datasheet URL for `query`, in priority order. Returns (url, source)
    or (None, reason). The reason for a miss is informational — the caller reports it
    so the agent can web-search and re-run `fetch --url <found>`."""
    qn = _norm(query)
    # 1. registry / toml override
    for part, entry in reg.get("parts", {}).items():
        if _norm(part) == qn and entry.get("source_url"):
            return entry["source_url"], "registry"
    for part, url in _toml_overrides(cfg).items():
        if _norm(part) == qn:
            return url, "toml-override"
    # 2/3/4. symbol field, BOM, LCSC — via the unified work-list
    for e in _work_list(cfg):
        if _norm(e["part"]) == qn and e["url"]:
            return e["url"], e["source"]
    return (None, f"no URL known for '{query}' — web-search the datasheet and re-run "
                  f"`kb datasheet fetch {query} --url <pdf-url>`")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and Path(ca).exists():
        try:
            ctx.load_verify_locations(ca)
        except ssl.SSLError:
            pass
    return ctx


def _download(url: str, dest: Path, root: Path, attempts: int = 3) -> Path:
    """Download `url` to `dest`, validating the %PDF magic header first. urllib's default
    opener honors HTTPS_PROXY from the environment, so we never touch the proxy config.

    Reads in chunks and retries on a truncated/interrupted transfer (a re-terminating proxy
    can drop a large PDF mid-stream → IncompleteRead). A non-PDF body is NOT retried — it's a
    content/policy problem, not a transient one, so it fails fast for the caller to report."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
            buf = bytearray()
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
                expected = r.headers.get("Content-Length")
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
            data = bytes(buf)
            # A re-terminating proxy can cap a large body and close cleanly (no IncompleteRead),
            # so cross-check the promised length — never ingest a silently truncated PDF.
            if expected is not None and len(data) < int(expected):
                raise http.client.IncompleteRead(data, int(expected) - len(data))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1 + i)            # brief backoff, then re-request from the top
            continue
        if not data.startswith(b"%PDF"):
            raise ValueError(f"response is not a PDF (starts with {data[:16]!r}) — likely an "
                             "HTML interstitial, login wall, or redirect page")
        tmp = _ensure_index_root(root) / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        scratch = tmp / (dest.name + ".part")
        scratch.write_bytes(data)
        dest.parent.mkdir(parents=True, exist_ok=True)
        scratch.replace(dest)
        return dest
    raise last if last else RuntimeError("download failed")


def _canonical_pdf_name(part: str, url: str) -> str:
    stem = _safe(part) if part else _safe(Path(urlparse(url).path).stem) or "datasheet"
    return f"{stem}.pdf"


def _fetch_one(query: str, cfg: cfgmod.Config, reg: dict, url_override: str | None,
               dpi: int) -> tuple[int, str]:
    """Resolve+download+ingest one part. Returns (exit_code, message). A missing URL is
    reported (code 0, informational) rather than failing — the agent supplies it."""
    root = cfg.root
    if url_override:
        url, source = url_override, "explicit-url"
    else:
        url, source = _resolve_url(query, cfg, reg)
    if not url:
        return 0, f"· {query}: {source}"
    dest = root / "Datasheets" / _canonical_pdf_name(query, url)
    try:
        _download(url, dest, root)
    except (urllib.error.URLError, ValueError, OSError) as e:
        return 1, f"✗ {query}: download failed — {e}"
    res = _ingest_one(dest, root, source_url=url, dpi=dpi, force=True)
    man = res["manifest"]
    reg.setdefault("parts", {})[query] = {
        "mpn": man.get("mpn"),
        "source_url": url,
        "local_pdf": str(_rel(dest, root)),
        "sha256": man.get("sha256"),
        "ingested": True,
        "source": source,
    }
    return 0, f"✓ {query}: {_rel(dest, root)} ({man['page_count']}p, {len(man['figures'])} figs)"


def _root(args) -> Path:
    return cfgmod.load_or_exit(getattr(args, "config", None)).root


def _cached_or_live_pages(root: Path, pdf: Path) -> list[str]:
    """Per-page text from the committed index when present (no poppler, no PDF parse),
    else fall back to a live `pdftotext` run. Lets toc/locate/view stay cheap once a
    datasheet is ingested while still working on un-ingested ad-hoc PDFs."""
    full = _index_dir(root, pdf) / "text" / "full.txt"
    if full.is_file():
        return full.read_text().split("\f")
    return _page_texts(pdf)


def _cmd_list(args) -> int:
    root = _root(args)
    pdfs = _datasheet_pdfs(root)
    if not pdfs:
        print("no datasheet PDFs found under Datasheets/, Imported CAD/, or the repo root")
        return 0
    for p in pdfs:
        print(p.relative_to(root))
    return 0


def _cmd_toc(args) -> int:
    root = _root(args)
    hits = _resolve_pdfs(args.query, root)
    if not hits:
        print(f"no datasheet matches '{args.query}'")
        return 1
    grep = _norm(args.grep) if args.grep else None
    for pdf in hits:
        toc = _parse_toc(_cached_or_live_pages(root, pdf))
        print(f"\n{_rel(pdf, root)}:")
        if not toc:
            print("  (no parseable table of contents)")
            continue
        for num, title, pg in toc:
            if grep and grep not in _norm(title):
                continue
            indent = "  " * (num.count(".") + 1)
            print(f"{indent}{num:<8} {title}  ·  p{pg}")
    return 0


def _cmd_locate(args) -> int:
    root = _root(args)
    hits = _resolve_pdfs(args.query, root)
    if not hits:
        print(f"no datasheet matches '{args.query}'")
        return 1
    for pdf in hits:
        print(f"\n{_rel(pdf, root)}:")
        pages = _cached_or_live_pages(root, pdf)
        loc = _locate_pages(pages)
        toc = _toc_pages(loc)
        figs = _pinout_figures(pages)
        fig_pages = {p for p, _ in figs}
        if figs:                            # pinout figures (with package) first, then table
            print("  pinout figs  " + ", ".join(f"p{p} {'/'.join(pk)}" for p, pk in figs))
            tbl = [p for p in loc["pinout"] if p not in toc and p not in fig_pages]
            if tbl:
                print(f"  pinout tbl   {tbl}")
        else:
            ps = [p for p in loc["pinout"] if p not in toc]
            print(f"  {'pinout':12} {ps if ps else '—'}")
        for name in SECTION_PATTERNS:
            if name == "pinout":
                continue
            ps = [p for p in loc[name] if p not in toc]
            print(f"  {name:12} {ps if ps else '—'}")
    return 0


def _cmd_view(args) -> int:
    _need("pdftoppm")
    root = _root(args)
    hits = _resolve_pdfs(args.query, root)
    if not hits:
        print(f"no datasheet matches '{args.query}'")
        return 1
    if len(hits) > 1:
        print(f"'{args.query}' matches several PDFs — narrow the query:")
        for p in hits:
            print("  " + str(_rel(p, root)))
        return 1
    pdf = hits[0]

    pages: list[int] = []
    if args.pages:
        pages += [int(x) for x in re.split(r"[,\s]+", args.pages.strip()) if x]
    if args.toc:                       # navigate by the doc's own table of contents
        toc = _parse_toc(_cached_or_live_pages(root, pdf))
        q = args.toc.strip()
        if re.fullmatch(r"\d+(?:\.\d+)*", q):
            sel = [(n, t, pg) for n, t, pg in toc if n == q]
        else:
            qn = _norm(q)
            sel = [(n, t, pg) for n, t, pg in toc if qn in _norm(t)]
        if not sel:
            print(f"no TOC section matches '{args.toc}' — run `kb datasheet toc {args.query}`")
            return 2
        for n, t, pg in sel:
            print(f"  · {n} {t} -> p{pg}")
        pages += [pg for _, _, pg in sel]
    if args.section:
        txt = _cached_or_live_pages(root, pdf)
        loc = _locate_pages(txt)
        toc = _toc_pages(loc)
        figs = _pinout_figures(txt)
        for s in [x.strip() for x in args.section.split(",") if x.strip()]:
            if s not in SECTION_PATTERNS:
                print(f"unknown section '{s}' (known: {', '.join(SECTION_PATTERNS)})")
                return 2
            if s == "pinout" and figs:                  # render the DIAGRAM pages, not the table
                sel = figs
                if args.package:
                    q = _norm(args.package)
                    sel = [(p, pk) for p, pk in figs if any(q in _norm(x) for x in pk)]
                    if not sel:
                        avail = sorted({x for _, pk in figs for x in pk})
                        print(f"no pinout figure matches package '{args.package}' "
                              f"(available: {', '.join(avail)})")
                        return 2
                found = [p for p, _ in sel]
            else:
                found = [p for p in loc[s] if p not in toc]
            if not found:
                print(f"note: no page found for section '{s}' in {pdf.name}")
            pages += found
    if not pages:
        print("nothing to render — pass --pages N,M, --section pinout,…, or --toc <title|number>")
        return 2

    pages = sorted(set(pages))
    if len(pages) > MAX_VIEW_PAGES:
        print(f"note: {len(pages)} pages requested; rendering the first {MAX_VIEW_PAGES} "
              f"({pages[:MAX_VIEW_PAGES]}), skipping {pages[MAX_VIEW_PAGES:]}")
        pages = pages[:MAX_VIEW_PAGES]

    cache = root / CACHE_DIRNAME
    rendered = [_render(pdf, pg, args.dpi, cache) for pg in pages]
    print(f"{_rel(pdf, root)} — {len(rendered)} page(s) at {args.dpi} dpi "
          f"(cached under {CACHE_DIRNAME}/):")
    for p in rendered:
        print("  " + str(p))
    print("→ open these PNGs with the Read tool. MAIN AGENT ONLY — image reads are costly; "
          "the quick-questions (Sonnet) agent should stay on text.")
    return 0


def _cmd_ingest(args) -> int:
    root = _root(args)
    if getattr(args, "all", False):
        pdfs = _datasheet_pdfs(root)
    else:
        if not args.query:
            print("pass a datasheet name/path or --all")
            return 2
        pdfs = _resolve_pdfs(args.query, root)
    if not pdfs:
        print(f"no datasheet matches '{args.query}'" if args.query
              else "no datasheet PDFs found to ingest")
        return 1
    n_ing = n_skip = 0
    for pdf in pdfs:
        res = _ingest_one(pdf, root, dpi=args.dpi, force=args.force)
        man = res["manifest"]
        if res["status"] == "ingested":
            n_ing += 1
            print(f"✓ {_rel(pdf, root)} — {man['page_count']}p, "
                  f"{len(man['figures'])} fig(s), {len(man['toc'])} TOC entr(ies)")
        else:
            n_skip += 1
            print(f"· {_rel(pdf, root)} — up to date (use --force to rebuild)")
    print(f"\ningested {n_ing}, skipped {n_skip} → {INDEX_DIRNAME}/ "
          "(text + index.json committed; figures/ gitignored)")
    return 0


def _cmd_fetch(args) -> int:
    cfg = cfgmod.load_or_exit(getattr(args, "config", None))
    reg = _load_registry(cfg.root)
    if getattr(args, "all", False):
        work = [e for e in _work_list(cfg)]
        if not work:
            print("no parts found (configure project.bom / project.root_sch / Imported CAD)")
            return 1
        queries = [e["part"] for e in work]
    else:
        if not args.query:
            print("pass a part name/query or --all")
            return 2
        queries = [args.query]
    rc = 0
    need_search: list[str] = []
    for q in queries:
        code, msg = _fetch_one(q, cfg, reg, args.url, args.dpi)
        print(msg)
        if code:
            rc = 1
        elif msg.startswith("·"):
            need_search.append(q)
    _save_registry(cfg.root, reg)
    if need_search:
        print(f"\n{len(need_search)} part(s) need a datasheet URL — web-search each and "
              f"re-run `kb datasheet fetch <part> --url <pdf-url>`.")
    return rc


def _cmd_search(args) -> int:
    root = _root(args)
    entries = _index_entries(root)
    if not entries:
        print(f"no ingested datasheets — run `kb datasheet ingest --all` first")
        return 1
    ql = args.text.lower()
    part_q = _norm(args.part) if args.part else None
    hits = 0
    for idir, man in entries:
        name = man.get("pdf") or man.get("mpn") or idir.name
        if part_q and part_q not in _norm(man.get("mpn", "") + Path(man.get("pdf", "")).stem):
            continue
        tdir = idir / "text"
        for i in range(1, man.get("page_count", 0) + 1):
            tf = tdir / f"p{i:03d}.txt"
            if not tf.exists():
                continue
            for ln in tf.read_text().splitlines():
                if ql in ln.lower():
                    print(f"{name} · p{i} · {ln.strip()[:120]}")
                    hits += 1
    if not hits:
        print(f"no matches for '{args.text}'"
              + (f" in parts matching '{args.part}'" if args.part else ""))
    return 0


def _cmd_figures(args) -> int:
    root = _root(args)
    entries = _index_entries(root)
    q = _norm(args.part)
    matched = [(d, m) for d, m in entries
               if q in _norm(m.get("mpn", "") + Path(m.get("pdf", "")).stem)]
    if not matched:
        print(f"no ingested datasheet matches '{args.part}' — run `kb datasheet ingest --all`")
        return 1
    missing = False
    for idir, man in matched:
        figs = man.get("figures", [])
        if args.section:
            figs = [f for f in figs if f.get("kind") == args.section]
        if args.package:
            qp = _norm(args.package)
            figs = [f for f in figs if f.get("package") and qp in _norm(f["package"])]
        print(f"{man.get('pdf') or man.get('mpn')}:")
        if not figs:
            print("  (no matching pre-rendered figures)")
            continue
        for f in figs:
            png = idir / f["path"]
            tag = f["kind"] + (f" {f['package']}" if f.get("package") else "") + f" p{f['page']}"
            if png.exists():
                print(f"  {tag}: {png}")
            else:
                missing = True
                print(f"  {tag}: (not rendered on this host)")
    if missing:
        print("\nsome PNGs are gitignored/not yet rendered here — run "
              "`kb datasheet ingest <part> --force` on a host with poppler to (re)build them.")
    else:
        print("\n→ open these PNGs with the Read tool (already rendered; no PDF parse needed).")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "datasheet",
        help="locate & render local datasheet pages (text is cheap; images are main-agent-only)",
    )
    acts = p.add_subparsers(dest="action", metavar="<action>")

    pl = acts.add_parser("list", help="list datasheet PDFs in the repo")
    pl.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pl.set_defaults(func=_cmd_list)

    plo = acts.add_parser("locate", help="report section -> page candidates (text only, cheap)")
    plo.add_argument("query", help="part name / LCSC# substring, or a path to a PDF")
    plo.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    plo.set_defaults(func=_cmd_locate)

    pt = acts.add_parser("toc", help="print the document's table of contents (text only, cheap)")
    pt.add_argument("query", help="part name / LCSC# substring, or a path to a PDF (e.g. an app note)")
    pt.add_argument("--grep", help="only show TOC titles containing this substring")
    pt.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pt.set_defaults(func=_cmd_toc)

    pv = acts.add_parser("view", help="render section/pages to cached PNG(s) for the main agent")
    pv.add_argument("query", help="part name / LCSC# substring, or a path to a PDF")
    pv.add_argument("--section", help="comma list of: " + ", ".join(SECTION_PATTERNS))
    pv.add_argument("--toc", help="render a section by its TOC title or number, e.g. 'device summary' or 3.2")
    pv.add_argument("--pages", help="explicit pages, e.g. 3,4,12")
    pv.add_argument("--package", help="with --section pinout: only the figure for this package, e.g. LQFP48")
    pv.add_argument("--dpi", type=int, default=150, help="render DPI (default 150)")
    pv.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pv.set_defaults(func=_cmd_view)

    pi = acts.add_parser(
        "ingest",
        help="extract a PDF once into cheap text/JSON/PNG artifacts under "
             f"{INDEX_DIRNAME}/ (so later reads cost no PDF parsing)")
    pi.add_argument("query", nargs="?", help="part name / LCSC# substring, or a path to a PDF")
    pi.add_argument("--all", action="store_true", help="ingest every datasheet PDF in the repo")
    pi.add_argument("--force", action="store_true", help="rebuild even if the index is current")
    pi.add_argument("--dpi", type=int, default=FIGURE_DPI,
                    help=f"figure render DPI (default {FIGURE_DPI})")
    pi.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pi.set_defaults(func=_cmd_ingest)

    pf = acts.add_parser(
        "fetch",
        help="resolve a datasheet URL, download it into Datasheets/, then auto-ingest")
    pf.add_argument("query", nargs="?", help="part name / LCSC# / value to fetch")
    pf.add_argument("--all", action="store_true",
                    help="fetch for every part with a known URL (symbol field / BOM / overrides)")
    pf.add_argument("--url", help="explicit PDF URL (use after web-searching an unknown part)")
    pf.add_argument("--dpi", type=int, default=FIGURE_DPI,
                    help=f"figure render DPI (default {FIGURE_DPI})")
    pf.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pf.set_defaults(func=_cmd_fetch)

    ps = acts.add_parser(
        "search",
        help="grep ingested datasheet TEXT for a string (no PDF, no poppler needed)")
    ps.add_argument("text", help="substring to search for (case-insensitive)")
    ps.add_argument("--part", help="limit to datasheets whose name matches this substring")
    ps.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    ps.set_defaults(func=_cmd_search)

    pg = acts.add_parser(
        "figures",
        help="print pre-rendered diagram PNG paths from the index (no PDF parse)")
    pg.add_argument("part", help="part name / LCSC# substring")
    pg.add_argument("--section", choices=["pinout", "typical-app", "layout"],
                    help="only this figure kind")
    pg.add_argument("--package", help="with pinout: only the figure for this package")
    pg.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    pg.set_defaults(func=_cmd_figures)

    p.set_defaults(func=lambda a: (p.print_help() or 0))
