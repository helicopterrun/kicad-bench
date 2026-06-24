"""datasheet — locate & render LOCAL datasheet PDF pages (read-only, offline).

Everything reads the repo's OWN datasheet PDFs, never the web. Text is cheap, so `locate`
uses `pdftotext` to point you at the page that holds a given section. Pinouts, absolute-max
tables, typical-application circuits and recommended-layout figures are *images*, though —
`view` renders just those pages to a cached PNG that the (vision-capable) MAIN agent opens
with the Read tool. The cheap Sonnet `quick-questions` agent should stay on text and DEFER
figures to the main agent.

`toc` parses the document's OWN table of contents, so it navigates any doc that has one —
datasheets AND application notes / explainer docs alike, across manufacturers (TI's "....."
and ST's ". . ." leaders both parse). `--toc` then renders a section by its real title/number.

Rendering is cached + idempotent (a given page+dpi is never rendered twice), so figures are
fetched once and reused. The cache (`.datasheet-cache/` at the repo root) self-ignores via an
inner `.gitignore`, so it never shows up in git. Any `query` may also be a direct path to a
PDF (for an ad-hoc app note that isn't in the repo).

  kb datasheet list                       # datasheet PDFs in the repo
  kb datasheet toc tps65150               # the document's own table of contents
  kb datasheet locate tps65150            # datasheet section -> page (pinout/abs-max/…)
  kb datasheet view tps65150 --section pinout --package LQFP48
  kb datasheet view tps65150 --toc "device summary"     # navigate by any TOC section
  kb datasheet view /tmp/szza036c.pdf --toc 3.2         # query can be a direct PDF path

Requires poppler-utils (pdftotext/pdftoppm): apt-get install poppler-utils / brew install poppler.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .core import config as cfgmod

CACHE_DIRNAME = ".datasheet-cache"
MAX_VIEW_PAGES = 8

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
_TOC_LINE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(.+?)\s*(?:\.[ \t]*){3,}\s*(\d+)\s*$")


def _parse_toc(pages: list[str]) -> list[tuple[str, str, int]]:
    """Parse the document's own table of contents into (section, title, page) rows. Works
    for any PDF that has one — datasheets AND application notes / explainer docs, across
    manufacturers (TI, ST, …). Page numbers are the printed numbers, which equal the PDF page
    index in the TI/ST docs we read (no front-matter offset)."""
    start = None
    for i, t in enumerate(pages[:8]):
        if _TOC_HEAD.search(t) or sum(bool(_TOC_LINE.match(ln)) for ln in t.splitlines()) >= 3:
            start = i
            break
    if start is None:
        return []
    out: list[tuple[str, str, int]] = []
    for t in pages[start:start + 15]:
        rows = [m for m in (_TOC_LINE.match(ln) for ln in t.splitlines()) if m]
        if not rows and out:
            break                      # past the end of the TOC
        out += [(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip(), int(m.group(3)))
                for m in rows]
    return out


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


def _root(args) -> Path:
    return cfgmod.load_or_exit(getattr(args, "config", None)).root


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
        toc = _parse_toc(_page_texts(pdf))
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
        pages = _page_texts(pdf)
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
        toc = _parse_toc(_page_texts(pdf))
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
        txt = _page_texts(pdf)
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

    p.set_defaults(func=lambda a: (p.print_help() or 0))
