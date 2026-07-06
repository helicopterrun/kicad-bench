"""schparse.py — read-only parsers for KiCad schematic & lib-table files.

Pure text/regex parsing (no kicad-sch-api dependency, no mutation). Consolidates
the parsing that the project's validate_sheet.py and validate_footprints.py do, so
the kicad-bench tools share one implementation.
"""
from __future__ import annotations

import re
from pathlib import Path

_HIER_LABEL = re.compile(r'\(hierarchical_label\s+"([^"]+)"(.*?)\(uuid', re.S)
_SHAPE = re.compile(r"\(shape\s+(\w+)\)")
_FOOTPRINT = re.compile(r'\(property\s+"Footprint"\s+"([^"]*)"')
_REFERENCE = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')
_VALUE = re.compile(r'\(property\s+"Value"\s+"([^"]*)"')
_LIB_ENTRY = re.compile(r'\(lib\s+\(name\s+"([^"]+)"\).*?\(uri\s+"([^"]+)"\)', re.S)
# A symbol's Value followed (within the same instance block) by its Datasheet property.
# Value precedes Datasheet in each KiCad symbol block, and the non-greedy gap stops at
# the FIRST Datasheet after the Value, so the two pair up per symbol.
_VALUE_DATASHEET = re.compile(
    r'\(property\s+"Value"\s+"([^"]*)".*?\(property\s+"Datasheet"\s+"([^"]*)"', re.S)


def input_hier_nets(sch: str | Path) -> set[str]:
    """Net names with a hierarchical INPUT/bidirectional label on this sheet —
    their driver lives in the not-yet-connected parent (partial-build artifact)."""
    text = Path(sch).read_text()
    nets: set[str] = set()
    for m in _HIER_LABEL.finditer(text):
        name, body = m.group(1), m.group(2)
        sm = _SHAPE.search(body)
        if sm and sm.group(1) in ("input", "bidirectional"):
            nets.add(name.lstrip("/"))
    return nets


def footprint_refs(sheets: list[Path]) -> dict[str, set[str]]:
    """footprint string -> set of sheet filenames referencing it (non-empty only)."""
    refs: dict[str, set[str]] = {}
    for sheet in sheets:
        try:
            text = sheet.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for fp in _FOOTPRINT.findall(text):
            if fp.strip():
                refs.setdefault(fp, set()).add(sheet.name)
    return refs


def datasheet_fields(sheets: list[Path]) -> dict[str, str]:
    """Map each symbol's Value -> its Datasheet URL across the given sheets.

    Best-effort, in the same regex style as `footprint_refs`. Only keeps entries whose
    Datasheet field is an actual URL (contains "://"), which drops the `~`/empty
    placeholders on lib_symbols templates and on parts with no datasheet set. First
    URL seen for a Value wins.
    """
    out: dict[str, str] = {}
    for sheet in sheets:
        try:
            text = sheet.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for val, ds in _VALUE_DATASHEET.findall(text):
            ds = ds.strip()
            val = val.strip()
            if val and "://" in ds:
                out.setdefault(val, ds)
    return out


_DESIGNATOR = re.compile(r"^[A-Za-z]+\d+$")


def references(sch: str | Path) -> list[str]:
    """Instance reference designators on a sheet (e.g. C45, U5, RV1).

    Filters out the `lib_symbols` template references ("C", "R", "U" with no
    number), power/virtual `#PWR` refs, and `REF**` placeholders — only real
    designators (letters followed by digits) are returned.
    """
    text = Path(sch).read_text()
    return [ref for ref in _REFERENCE.findall(text) if _DESIGNATOR.match(ref)]


_PROPERTY = re.compile(r'\(property\s+"([^"]+)"\s+"([^"]*)"')
_SYMBOL_SPLIT = re.compile(r'\n\s*\(symbol\b')


def components(sch: str | Path,
               mpn_keys: tuple[str, ...] = ("MPN", "Manufacturer Part Number")) -> list[dict]:
    """Instance components as dicts ``{reference, value, mpn}``.

    One entry per real instance symbol — a symbol whose Reference is a true designator
    (letters+digits). The `lib_symbols` templates ("C", "R", …) and power/virtual
    `#`-prefixed symbols are dropped, same as `references()`. `mpn` is the first
    non-empty value among `mpn_keys` (KiCad's `Datasheet`/`Footprint` etc. are ignored).

    Read-only text parse — needs no kicad-cli.
    """
    text = Path(sch).read_text()
    out: list[dict] = []
    for block in _SYMBOL_SPLIT.split(text)[1:]:
        props: dict[str, str] = {}
        for key, val in _PROPERTY.findall(block):
            props.setdefault(key, val)          # first (the instance's own) wins
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
    for m in _LIB_ENTRY.finditer(table.read_text()):
        nick, uri = m.group(1), m.group(2)
        uri = uri.replace("${KIPRJMOD}", str(kiprjmod))
        libs[nick] = Path(uri)
    return libs
