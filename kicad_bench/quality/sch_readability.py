"""sch-readability — objective sheet-level RULE-conformance gate for a .kicad_sch.

This is NOT an aesthetic-quality score. Calibration against a human-perfected vs. raw-
generator golden pair proved that simple geometric metrics (bbox overlaps, text proximity)
do NOT separate "clean" from "messy" — good schematics are dense, and the real defects are
sub-millimeter/subjective. So this gate checks only the things that are *objectively wrong*
regardless of taste, drawn from SCHEMATIC_RULES.md / the symbol style guide:

  * off-grid     — component anchors / labels / junctions off the 50 mil (1.27 mm) grid (Rule 1)
  * page-bounds  — a symbol's drawn body outside the usable page area
  * body-overlap — two symbols' DRAWN graphic bodies (rectangles/polylines, NOT pins) overlap
  * power-rot    — a power-port symbol rotated off its upright orientation (Rule 4)

Bodies use the embedded (lib_symbols) graphic primitives only (pins/labels excluded) — pins
legitimately reach into whitespace, so including them makes every dense sheet "overlap".
The body geometry helpers are also reused by the auto-placement spike as hard constraints.
Read-only. Advisory-by-default; --strict promotes warnings to errors.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, render_and_exit
from .symbol_style import parse_sexpr, _tag, _children, _first, _on_grid, GRID_MM

# Page B (ANSI) landscape, mm, with the standard 12.7 mm frame margin.
_PAGES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A": (279.4, 215.9),
          "B": (431.8, 279.4), "C": (558.8, 431.8)}
MARGIN = 12.7
_AREA_EPS = 0.5     # mm^2 — ignore hairline overlaps from shared edges
# Power ports may be rotated 0/90/270 to meet side rails (the accepted reference does this);
# the actual rule is "text never upside down", i.e. flag only 180°.
_POWER_BAD_ROT = {180.0}


# ── geometry: tight graphic-body bbox from the embedded (lib_symbols) ──────────
def graphic_body_extents(root) -> dict:
    """lib_id -> (minx, miny, maxx, maxy) over DRAWN graphics only (rect/polyline/circle/
    arc), pins excluded. Local symbol coordinates; transform per instance."""
    libs = _first(root, "lib_symbols")
    out: dict = {}
    if not libs:
        return out
    for sym in _children(libs, "symbol"):
        if len(sym) < 2 or not isinstance(sym[1], str):
            continue
        xs: list[float] = []
        ys: list[float] = []

        def walk(node):
            for ch in node:
                if not isinstance(ch, list):
                    continue
                t = _tag(ch)
                if t == "rectangle":
                    for k in ("start", "end"):
                        p = _first(ch, k)
                        if p:
                            xs.append(float(p[1])); ys.append(float(p[2]))
                elif t == "polyline":
                    pts = _first(ch, "pts")
                    if pts:
                        for xy in _children(pts, "xy"):
                            xs.append(float(xy[1])); ys.append(float(xy[2]))
                elif t == "circle":
                    c = _first(ch, "center"); r = _first(ch, "radius")
                    if c and r:
                        rr = float(r[1])
                        xs.append(float(c[1]) - rr); xs.append(float(c[1]) + rr)
                        ys.append(float(c[2]) - rr); ys.append(float(c[2]) + rr)
                elif t == "arc":
                    for k in ("start", "mid", "end"):
                        p = _first(ch, k)
                        if p:
                            xs.append(float(p[1])); ys.append(float(p[2]))
                elif t == "symbol":   # nested unit/body-style
                    walk(ch)
        walk(sym)
        if xs:
            out[sym[1]] = (min(xs), min(ys), max(xs), max(ys))
    return out


def instance_body_bbox(ext, px: float, py: float, rot: float):
    """Transform a local body extent to a world-coordinate (minx,miny,maxx,maxy) bbox."""
    corners = [(ext[0], ext[1]), (ext[2], ext[1]), (ext[2], ext[3]), (ext[0], ext[3])]
    a = math.radians(rot or 0.0)
    ca, sa = math.cos(a), math.sin(a)
    rx = [x * ca - y * sa for x, y in corners]
    ry = [x * sa + y * ca for x, y in corners]
    return (px + min(rx), py + min(ry), px + max(rx), py + max(ry))


def _overlap_area(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


# ── instance reads (component anchors, labels, junctions, power refs) ──────────
def _symbol_instances(root):
    """Yield (lib_id, ref, x, y, rot) for each top-level placed symbol instance."""
    sheet = root
    for sym in _children(sheet, "symbol"):
        lid = _first(sym, "lib_id")
        at = _first(sym, "at")
        if not lid or not at:
            continue
        ref = ""
        for prop in _children(sym, "property"):
            if len(prop) > 2 and prop[1] == "Reference":
                ref = prop[2]
        yield (lid[1], ref, float(at[1]), float(at[2]),
               float(at[3]) if len(at) > 3 else 0.0)


def _positions(root, tag):
    """Yield (x, y) for each top-level node of the given tag carrying an (at ...)."""
    for node in _children(root, tag):
        at = _first(node, "at")
        if at:
            yield float(at[1]), float(at[2])


# ── audit ─────────────────────────────────────────────────────────────────────
def audit(path: Path, strict: bool = False) -> Result:
    res = Result("Sheet readability (rules)" + (" — strict" if strict else ""))
    emit = res.error if strict else res.warn
    root = parse_sexpr(Path(path).read_text())

    paper = _first(root, "paper")
    pname = paper[1] if paper and len(paper) > 1 else "B"
    pw, ph = _PAGES.get(pname, _PAGES["B"])
    usable = (MARGIN, MARGIN, pw - MARGIN, ph - MARGIN)

    ext = graphic_body_extents(root)
    insts = list(_symbol_instances(root))
    bodies = []   # (ref, bbox)
    for lid, ref, x, y, rot in insts:
        # off-grid anchor (always an error — Rule 1, "no off-grid endpoints, ever")
        if not (_on_grid(x) and _on_grid(y)):
            res.error(f"{ref or lid}: anchor off 50mil grid at ({x}, {y})mm", ref or lid)
        # power port not upside-down (0/90/270 are all fine; 180 puts text upside down)
        if lid.startswith("power:") and rot in _POWER_BAD_ROT:
            emit(f"{ref or lid}: power port upside down (rotated 180°)", ref or lid)
        e = ext.get(lid)
        if not e:
            continue
        bb = instance_body_bbox(e, x, y, rot)
        bodies.append((ref or lid, bb))
        # page bounds
        if bb[0] < usable[0] or bb[1] < usable[1] or bb[2] > usable[2] or bb[3] > usable[3]:
            res.error(f"{ref or lid}: body outside usable page {pname} "
                      f"(@{bb[0]:.1f},{bb[1]:.1f}..{bb[2]:.1f},{bb[3]:.1f})", ref or lid)

    # body-on-body overlap (drawn graphics only)
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a = _overlap_area(bodies[i][1], bodies[j][1])
            if a > _AREA_EPS:
                res.error(f"{bodies[i][0]} & {bodies[j][0]}: symbol bodies overlap "
                          f"({a:.1f}mm²)", f"{bodies[i][0]}/{bodies[j][0]}")

    # off-grid labels / junctions
    for x, y in list(_positions(root, "label")) + list(_positions(root, "global_label")):
        if not (_on_grid(x) and _on_grid(y)):
            emit(f"label off 50mil grid at ({x}, {y})mm", f"@({x},{y})")
    for x, y in _positions(root, "junction"):
        if not (_on_grid(x) and _on_grid(y)):
            emit(f"junction off 50mil grid at ({x}, {y})mm", f"@({x},{y})")

    n_warn = sum(1 for f in res.findings if f.severity == "warn")
    res.summary = (f"{len(insts)} symbols on page {pname}; {res.n_errors} rule error(s)"
                   + (f", {n_warn} warning(s)" if n_warn else "")
                   + ("" if strict else " (advisory — use --strict to fail on warnings)"))
    if res.n_errors == 0 and n_warn == 0:
        res.ok(f"{len(insts)} symbols: on-grid, in-bounds, no body overlap", pname)
    return res


def run(args) -> int:
    sch = Path(args.schematic) if args.schematic else None
    if not sch:
        cfg = cfgmod.load_or_exit(args.config)
        sch = cfg.root_sch
    if not sch or not Path(sch).exists():
        sys.exit("error: no schematic given and no project.root_sch in config")
    return render_and_exit(audit(Path(sch), strict=args.strict))


def add_parser(sub):
    p = sub.add_parser("sch-readability",
                       help="objective sheet-layout rule gate (grid/bounds/body-overlap/power)")
    p.add_argument("schematic", nargs="?", help="sheet to check (default: project.root_sch)")
    p.add_argument("--strict", action="store_true", help="fail on warnings too (exit 1)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
