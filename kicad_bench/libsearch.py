"""lib-search — full-text search over installed KiCad symbol & footprint libraries.

Answers "do I already have a symbol/footprint for X?" without opening KiCad:
indexes every `.kicad_sym` symbol (name + Description + ki_keywords properties)
and every `.pretty/*.kicad_mod` footprint (name + descr + tags) into a SQLite
FTS5 database, then queries it. Read-only on the libraries; the index lives in
the user cache dir and rebuilds with --reindex.

Sources indexed by default:
  * KiCad's bundled libraries (macOS app bundle / standard Linux paths)
  * the workspace root this command is run inside (project + shared libs),
    skipping *-backups directories

    kb lib-search "lvds serializer"
    kb lib-search --footprints "tsot-26"
    kb lib-search --reindex            # rebuild after installing new libs

Exit codes: 0 hits found, 1 no hits, 2 setup error. FTS5 is stdlib sqlite3.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

_SYSTEM_DIRS = [
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
    Path("/usr/share/kicad/symbols"), Path("/usr/share/kicad/footprints"),
]

_DB = Path.home() / ".cache" / "kicad-bench" / "libsearch.db"

_SYM_RE = re.compile(r'\(symbol\s+"([^":]+)"')
_PROP_RE = re.compile(r'\(property\s+"(Description|ki_keywords)"\s+"([^"]*)"')
_FP_DESCR = re.compile(r'\(descr\s+"([^"]*)"')
_FP_TAGS = re.compile(r'\(tags\s+"([^"]*)"')


def _iter_symbols(path: Path):
    """(lib, name, desc, keywords) for each top-level symbol in a .kicad_sym."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    lib = path.stem
    # split on top-level symbol defs; properties for a symbol appear before the
    # next (symbol " header
    heads = list(_SYM_RE.finditer(text))
    for i, m in enumerate(heads):
        # skip sub-unit defs like "R_0_1" (they repeat the parent name + _N_M)
        name = m.group(1)
        if re.fullmatch(r".+_\d+_\d+", name):
            continue
        seg = text[m.start():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        desc = kw = ""
        for pm in _PROP_RE.finditer(seg):
            if pm.group(1) == "Description":
                desc = pm.group(2)
            else:
                kw = pm.group(2)
        yield (lib, name, desc, kw)


def _iter_footprints(pretty: Path):
    for mod in pretty.glob("*.kicad_mod"):
        try:
            head = mod.read_text(errors="replace")[:4000]
        except OSError:
            continue
        d = _FP_DESCR.search(head)
        t = _FP_TAGS.search(head)
        yield (pretty.stem, mod.stem, d.group(1) if d else "", t.group(1) if t else "")


def _source_dirs(extra: list[str]) -> list[Path]:
    dirs = [d for d in _SYSTEM_DIRS if d.is_dir()]
    dirs.append(Path.cwd())
    dirs += [Path(e) for e in extra]
    return dirs


def reindex(extra: list[str]) -> tuple[int, int]:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB)
    con.execute("DROP TABLE IF EXISTS entries")
    con.execute("CREATE VIRTUAL TABLE entries USING fts5(kind, lib, name, desc, keywords, path)")
    n_sym = n_fp = 0
    seen: set[Path] = set()
    for base in _source_dirs(extra):
        for sym in base.rglob("*.kicad_sym"):
            if "-backups" in str(sym) or sym in seen:
                continue
            seen.add(sym)
            for lib, name, desc, kw in _iter_symbols(sym):
                con.execute("INSERT INTO entries VALUES ('symbol',?,?,?,?,?)",
                            (lib, name, desc, kw, str(sym)))
                n_sym += 1
        for pretty in base.rglob("*.pretty"):
            if "-backups" in str(pretty) or pretty in seen or not pretty.is_dir():
                continue
            seen.add(pretty)
            for lib, name, desc, tags in _iter_footprints(pretty):
                con.execute("INSERT INTO entries VALUES ('footprint',?,?,?,?,?)",
                            (lib, name, desc, tags, str(pretty)))
                n_fp += 1
    con.commit()
    con.close()
    return n_sym, n_fp


def query(q: str, kind: str | None, limit: int) -> list[tuple]:
    if not _DB.exists():
        return []
    con = sqlite3.connect(_DB)
    # quote each term so symbols like "0402" or hyphens don't break FTS syntax
    fts = " ".join(f'"{t}"' for t in q.split())
    sql = "SELECT kind, lib, name, desc FROM entries WHERE entries MATCH ?"
    args: list = [fts]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return rows


def run(args) -> int:
    if args.reindex:
        n_sym, n_fp = reindex(args.add or [])
        print(f"✓ indexed {n_sym} symbols + {n_fp} footprints -> {_DB}")
        if not args.query:
            return 0
    if not args.query:
        print("give a query (or --reindex)", file=sys.stderr)
        return 2
    if not _DB.exists():
        print("no index yet — run `kb lib-search --reindex` first", file=sys.stderr)
        return 2
    rows = query(args.query, "footprint" if args.footprints else
                 ("symbol" if args.symbols else None), args.limit)
    for kind, lib, name, desc in rows:
        tag = "fp " if kind == "footprint" else "sym"
        print(f"{tag}  {lib}:{name:40s} {desc[:70]}")
    if not rows:
        print("no hits")
    return 0 if rows else 1


def add_parser(sub):
    p = sub.add_parser("lib-search",
                       help="FTS search over installed symbol/footprint libraries")
    p.add_argument("query", nargs="?", help='e.g. "lvds serializer", "tsot-26"')
    p.add_argument("--symbols", action="store_true", help="symbols only")
    p.add_argument("--footprints", action="store_true", help="footprints only")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--reindex", action="store_true",
                   help="rebuild the index (system libs + cwd tree)")
    p.add_argument("--add", action="append", metavar="DIR",
                   help="extra directory to index (repeatable)")
    p.set_defaults(func=run)
