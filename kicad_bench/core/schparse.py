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


_DESIGNATOR = re.compile(r"^[A-Za-z]+\d+$")


def references(sch: str | Path) -> list[str]:
    """Instance reference designators on a sheet (e.g. C45, U5, RV1).

    Filters out the `lib_symbols` template references ("C", "R", "U" with no
    number), power/virtual `#PWR` refs, and `REF**` placeholders — only real
    designators (letters followed by digits) are returned.
    """
    text = Path(sch).read_text()
    return [ref for ref in _REFERENCE.findall(text) if _DESIGNATOR.match(ref)]


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
