"""footprints.py — resolve a "Lib:Name" footprint string to a real .kicad_mod.

Ported from the project's validate_footprints.py so block-review and commit-gate
share one resolver. Resolution order for "Lib:Name":
  1. the .pretty dir registered under `Lib` in the project fp-lib-table
  2. the KiCad system footprint dir  <system>/Lib.pretty/Name.kicad_mod

On a miss, returns a difflib close-match suggestion so typos/version drift are
obvious. Read-only.
"""
from __future__ import annotations

import difflib
import os
import platform
from pathlib import Path


def system_footprint_dir() -> Path | None:
    env = os.environ.get("KICAD9_FOOTPRINT_DIR") or os.environ.get("KICAD_FOOTPRINT_DIR")
    if env:
        return Path(env)
    sysname = platform.system()
    if sysname == "Darwin":
        p = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
    elif sysname == "Windows":
        p = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "KiCad" / "10.0" / "share" / "kicad" / "footprints"
    else:
        p = Path("/usr/share/kicad/footprints")
    return p if p.exists() else None


def resolve_path(fp: str, fp_libs: dict[str, Path], system: Path | None) -> Path | None:
    """Return the .kicad_mod path for "Lib:Name", or None if unresolved."""
    if ":" not in fp:
        return None
    lib, name = fp.split(":", 1)
    for d in ([fp_libs[lib]] if lib in fp_libs else []) + (
            [system / f"{lib}.pretty"] if system else []):
        cand = d / f"{name}.kicad_mod"
        if cand.exists():
            return cand
    return None


def pad_count(mod_path: Path) -> int:
    """Count real (named) pads in a .kicad_mod."""
    import re
    text = mod_path.read_text(encoding="utf-8", errors="ignore")
    return len([p for _, p, _ in
                re.findall(r'\(pad\s+("?)([^"\s\)]*)\1\s+(\w+)', text) if p])


def resolve(fp: str, fp_libs: dict[str, Path], system: Path | None) -> tuple[bool, str]:
    if ":" not in fp:
        return False, "not namespaced — expected 'Lib:Name'"
    lib, name = fp.split(":", 1)

    search_dirs: list[Path] = []
    if lib in fp_libs:
        search_dirs.append(fp_libs[lib])
    if system is not None:
        search_dirs.append(system / f"{lib}.pretty")

    for d in search_dirs:
        if (d / f"{name}.kicad_mod").exists():
            return True, "OK"

    for d in search_dirs:
        if d.exists():
            names = [f.stem for f in d.glob("*.kicad_mod")]
            close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
            if close:
                return False, f"'{name}' not in {lib} — did you mean: {', '.join(close)}"
            return False, f"'{name}' not found in {lib}.pretty"

    known = list(fp_libs.keys())
    if system is not None and system.exists():
        known += [d.stem for d in system.glob("*.pretty")]
    close = difflib.get_close_matches(lib, known, n=3, cutoff=0.5)
    if close:
        return False, f"library '{lib}' not registered — close: {', '.join(close)}"
    return False, f"library '{lib}' not found in fp-lib-table or system footprints"
