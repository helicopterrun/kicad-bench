"""geom.py — board-outline bounding box from a .kicad_pcb (stdlib-only).

Pricing needs one geometric fact the copper-focused `core/pcbgeom` parser doesn't
surface: the **board outline area**. This reads the Edge.Cuts graphics directly from the
raw file text (like `pcbgeom.iter_track_spans`, we stay off `sexpdata` to keep the core
stdlib-only) and returns the outline's axis-aligned bounding box.

Scope / known limits (rough-estimate tool, flagged not hidden):
  * Handles board-level `gr_line` / `gr_rect` / `gr_arc` / `gr_poly` / `gr_curve` on the
    `Edge.Cuts` layer, which is how ~all outlines are drawn.
  * Arc *bulge* is not integrated — arc endpoints/mid are included, so a board whose extent
    is defined by an arc's far side can read slightly small. Fine for a bbox estimate.
  * An outline drawn as `fp_*` graphics inside a footprint (a board-outline footprint) is
    NOT transformed by the footprint's placement and is ignored — `extract_spec` warns.
Read-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MM_PER_IN = 25.4

_GR_HEADS = ("gr_line", "gr_rect", "gr_arc", "gr_poly", "gr_curve")
# coordinate atoms that carry an (x y) pair inside a graphic block
_COORD = re.compile(r"\((?:start|end|mid|center|xy)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


@dataclass(frozen=True)
class BBox:
    w_mm: float
    h_mm: float

    @property
    def area_mm2(self) -> float:
        return self.w_mm * self.h_mm

    @property
    def w_in(self) -> float:
        return self.w_mm / MM_PER_IN

    @property
    def h_in(self) -> float:
        return self.h_mm / MM_PER_IN

    @property
    def area_in2(self) -> float:
        return self.w_in * self.h_in


def _balanced_blocks(text: str, head: str):
    """Yield the substring of each top-level `(head …)` s-expression block."""
    key = "(" + head
    i = 0
    n = len(text)
    while True:
        i = text.find(key, i)
        if i < 0:
            return
        # ensure it's a real head token, not a prefix (e.g. gr_line vs gr_lin…)
        after = text[i + len(key)]
        if after not in " \t\n(":
            i += len(key)
            continue
        depth = 0
        j = i
        while j < n:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    yield text[i : j + 1]
                    break
            j += 1
        i = j + 1


def board_outline_bbox(pcb_path: str | Path) -> BBox | None:
    """Bounding box of the Edge.Cuts outline, or None if no outline graphics are found."""
    text = Path(pcb_path).read_text()
    xs: list[float] = []
    ys: list[float] = []
    for h in _GR_HEADS:
        for blk in _balanced_blocks(text, h):
            if '"Edge.Cuts"' not in blk:
                continue
            for mx, my in _COORD.findall(blk):
                xs.append(float(mx))
                ys.append(float(my))
    if not xs:
        return None
    return BBox(w_mm=max(xs) - min(xs), h_mm=max(ys) - min(ys))
