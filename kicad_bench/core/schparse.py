"""schparse.py — read-only parsers for KiCad schematic & lib-table files.

Structural S-expression parsing over `core.sexp` (no kicad-sch-api, no mutation): symbols,
labels, and lib-table entries are read as tree nodes with named children, so a Value is
never mispaired with the wrong symbol's Datasheet the way a positional `.*?` regex can, and
a reordered/added KiCad token can't desync the read. Consolidates the parsing the project's
validate_sheet.py / validate_footprints.py did, so the kicad-bench tools share one path.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import sexp

# A real instance designator: letters+digits (R1, U5, RV1). Excludes the `lib_symbols`
# templates ("R", "C"), power/virtual `#PWR`, and `REF**` placeholders.
_DESIGNATOR = re.compile(r"^[A-Za-z]+\d+$")


def _load(path: str | Path):
    return sexp.load(Path(path).read_text())


def _sym_props(symbol) -> dict[str, str]:
    """A symbol's own `(property "Name" "Value")` children as {name: value} — first
    occurrence wins, matching the instance's own value over any inherited default."""
    props: dict[str, str] = {}
    for p in sexp.children(symbol, "property"):
        if len(p) >= 3:
            props.setdefault(sexp.sym(p[1]), sexp.sym(p[2]))
    return props


def input_hier_nets(sch: str | Path) -> set[str]:
    """Net names with a hierarchical INPUT/bidirectional label on this sheet —
    their driver lives in the not-yet-connected parent (partial-build artifact)."""
    nets: set[str] = set()
    for lbl in sexp.walk(_load(sch), "hierarchical_label"):
        shape = sexp.first(lbl, "shape")
        if len(lbl) >= 2 and shape and len(shape) >= 2 and sexp.sym(shape[1]) in ("input", "bidirectional"):
            nets.add(sexp.sym(lbl[1]).lstrip("/"))
    return nets


def footprint_refs(sheets: list[Path]) -> dict[str, set[str]]:
    """footprint string -> set of sheet filenames referencing it (non-empty only)."""
    refs: dict[str, set[str]] = {}
    for sheet in sheets:
        try:
            root = _load(sheet)
        except (OSError, UnicodeDecodeError):
            continue
        for symbol in sexp.walk(root, "symbol"):
            fp = _sym_props(symbol).get("Footprint", "").strip()
            if fp:
                refs.setdefault(fp, set()).add(sheet.name)
    return refs


def datasheet_fields(sheets: list[Path]) -> dict[str, str]:
    """Map each symbol's Value -> its Datasheet URL across the given sheets.

    Only entries whose Datasheet is a real URL (contains "://") are kept, which drops the
    `~`/empty placeholders on `lib_symbols` templates and on parts with no datasheet set.
    First URL seen for a Value wins. Reading Value and Datasheet from the SAME symbol node
    avoids the cross-symbol mispairing a positional regex could produce.
    """
    out: dict[str, str] = {}
    for sheet in sheets:
        try:
            root = _load(sheet)
        except (OSError, UnicodeDecodeError):
            continue
        for symbol in sexp.walk(root, "symbol"):
            props = _sym_props(symbol)
            val, ds = props.get("Value", "").strip(), props.get("Datasheet", "").strip()
            if val and "://" in ds:
                out.setdefault(val, ds)
    return out


def references(sch: str | Path) -> list[str]:
    """Instance reference designators on a sheet (e.g. C45, U5, RV1), in document order.

    Filters out the `lib_symbols` template references ("C", "R", "U" with no number),
    power/virtual `#PWR` refs, and `REF**` placeholders — only real designators
    (letters followed by digits) are returned.
    """
    out: list[str] = []
    for symbol in sexp.walk(_load(sch), "symbol"):
        ref = _sym_props(symbol).get("Reference", "")
        if _DESIGNATOR.match(ref):
            out.append(ref)
    return out


def components(sch: str | Path,
               mpn_keys: tuple[str, ...] = ("MPN", "Manufacturer Part Number")) -> list[dict]:
    """Instance components as dicts ``{reference, value, mpn}``.

    One entry per real instance symbol — a symbol whose Reference is a true designator
    (letters+digits). The `lib_symbols` templates ("C", "R", …) and power/virtual
    `#`-prefixed symbols are dropped, same as `references()`. `mpn` is the first
    non-empty value among `mpn_keys` (KiCad's `Datasheet`/`Footprint` etc. are ignored).

    Read-only structural parse — needs no kicad-cli.
    """
    out: list[dict] = []
    for symbol in sexp.walk(_load(sch), "symbol"):
        props = _sym_props(symbol)
        ref = props.get("Reference", "")
        if not _DESIGNATOR.match(ref):
            continue
        mpn = ""
        for key in mpn_keys:
            if props.get(key, "").strip():
                mpn = props[key].strip()
                break
        out.append({"reference": ref, "value": props.get("Value", "").strip(), "mpn": mpn})
    return out


def parse_fp_lib_table(table: Path, kiprjmod: Path) -> dict[str, Path]:
    """nickname -> .pretty dir, expanding ${KIPRJMOD}."""
    libs: dict[str, Path] = {}
    if not table.exists():
        return libs
    for lib in sexp.walk(sexp.load(table.read_text()), "lib"):
        name, uri = sexp.first(lib, "name"), sexp.first(lib, "uri")
        if name and len(name) >= 2 and uri and len(uri) >= 2:
            libs[sexp.sym(name[1])] = Path(sexp.sym(uri[1]).replace("${KIPRJMOD}", str(kiprjmod)))
    return libs


def lib_nicknames(text: str) -> list[str]:
    """The `(lib (name "<nick>") … (uri …))` nicknames declared in a sym-/fp-lib-table's
    text, in document order (empty on a partial/invalid table). A stable public entry point
    for tooling that reports which libraries a table already registers — kept public
    precisely so consumers don't reach into this module's regex internals."""
    try:
        root = sexp.load(text)
    except Exception:  # noqa: BLE001 — an incomplete table simply has no complete entries
        return []
    out: list[str] = []
    for lib in sexp.walk(root, "lib"):
        name, uri = sexp.first(lib, "name"), sexp.first(lib, "uri")
        if name and len(name) >= 2 and uri and len(uri) >= 2:
            out.append(sexp.sym(name[1]))
    return out
