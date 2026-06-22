"""pcbgeom.py — read-only geometric parser for a KiCad-10 .kicad_pcb.

Every other kicad-bench tool treats the board as opaque (a `"(footprint " in text`
presence-check, then `kicad-cli pcb drc`). This module is the ONE place that actually
extracts track / via / zone / pad geometry, so the geometric audits — diff-pair skew,
as-routed width-vs-domain conformance, route coverage — can reason about what is really
on the copper. Pure-stdlib, read-only: it never writes the board.

Net references vary by element and by board:
  * a quoted name      `(net "GND")`            — as this project's board stores tracks/vias,
  * a numeric id       `(net 5)`                — resolved against the `(net 5 "GND")` table,
  * id + name together `(net 5 "GND")`          — as pads store them.
`parse()` handles all three and always exposes the resolved net NAME.

Net names here keep their full hierarchical path (`/video/TMDS_CLK_P`, `/LVDS_CLK_P`).
Use `leaf()` / `match_nets()` to bridge to the short names in design_rules_config.json.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

COPPER_RE = re.compile(r"\.Cu$")


# --- quote-aware balanced s-expr extraction ---------------------------------
def _balanced(text: str, start: int) -> tuple[str, int]:
    """Return (block, end_index) for the parenthesised block beginning at `start`.
    Quote-aware so net names / properties containing '(' don't desync the depth."""
    depth = 0
    in_str = False
    esc = False
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], n


def _iter_blocks(text: str, keyword: str):
    """Yield each `(keyword …)` block in `text` (non-nested element keywords)."""
    for m in re.finditer(r"\(" + re.escape(keyword) + r"\b", text):
        yield _balanced(text, m.start())[0]


# --- small field extractors -------------------------------------------------
_AT = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)")
_START = re.compile(r"\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
_MID = re.compile(r"\(mid\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
_END = re.compile(r"\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
_WIDTH = re.compile(r"\(width\s+(-?[\d.]+)\)")
_LAYER = re.compile(r'\(layer\s+"([^"]*)"\)')
_LAYERS = re.compile(r'\(layers\s+([^)]*)\)')
_SIZE1 = re.compile(r"\(size\s+(-?[\d.]+)\)")
_DRILL = re.compile(r"\(drill\s+(-?[\d.]+)\)")
# net forms, most specific first
_NET_NUMNAME = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_NET_NAME = re.compile(r'\(net\s+"([^"]*)"\)')
_NET_NUM = re.compile(r"\(net\s+(\d+)\)")
_NET_DECL = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_REF = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')


def _pt(rx: re.Pattern, block: str):
    m = rx.search(block)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _net_of(block: str, table: dict[int, str]) -> str:
    m = _NET_NUMNAME.search(block)
    if m:
        return m.group(2)
    m = _NET_NAME.search(block)
    if m:
        return m.group(1)
    m = _NET_NUM.search(block)
    if m:
        return table.get(int(m.group(1)), "")
    return ""


# --- elements ---------------------------------------------------------------
@dataclass
class Track:
    net: str
    layer: str
    width: float
    start: tuple[float, float]
    end: tuple[float, float]
    mid: tuple[float, float] | None = None   # set for arc tracks

    @property
    def is_copper(self) -> bool:
        return bool(COPPER_RE.search(self.layer))

    @property
    def length(self) -> float:
        if self.mid is None:
            return math.dist(self.start, self.end)
        return _arc_length(self.start, self.mid, self.end)


@dataclass
class Via:
    net: str
    at: tuple[float, float]
    size: float
    drill: float
    layers: tuple[str, ...]


@dataclass
class Pad:
    ref: str
    number: str
    net: str


@dataclass
class Zone:
    net: str
    layers: tuple[str, ...]


@dataclass
class Board:
    path: Path
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    footprints: dict[str, str] = field(default_factory=dict)  # ref -> fpid

    # -- net views ----------------------------------------------------------
    @property
    def net_names(self) -> set[str]:
        names = {t.net for t in self.tracks} | {v.net for v in self.vias}
        names |= {p.net for p in self.pads} | {z.net for z in self.zones}
        names.discard("")
        return names

    def tracks_by_net(self) -> dict[str, list[Track]]:
        out: dict[str, list[Track]] = {}
        for t in self.tracks:
            out.setdefault(t.net, []).append(t)
        return out

    def vias_by_net(self) -> dict[str, list[Via]]:
        out: dict[str, list[Via]] = {}
        for v in self.vias:
            out.setdefault(v.net, []).append(v)
        return out

    def pads_by_net(self) -> dict[str, list[Pad]]:
        out: dict[str, list[Pad]] = {}
        for p in self.pads:
            out.setdefault(p.net, []).append(p)
        return out

    def net_length(self, net: str) -> float:
        """Total copper-track centerline length on `net` (mm).

        Centerline only — excludes via barrels and pad entry, so absolute length is
        approximate, but the per-net total is exact-enough to compare P vs N skew.
        """
        return sum(t.length for t in self.tracks if t.net == net and t.is_copper)

    def width_stats(self, net: str) -> tuple[float, float, set[float]]:
        """(min, max, {distinct}) copper-track widths on `net`; (0,0,set()) if none."""
        ws = [round(t.width, 4) for t in self.tracks if t.net == net and t.is_copper]
        if not ws:
            return (0.0, 0.0, set())
        return (min(ws), max(ws), set(ws))

    def layers_used(self, net: str) -> set[str]:
        return {t.layer for t in self.tracks if t.net == net}

    def has_copper(self, net: str) -> bool:
        return any(t.net == net for t in self.tracks) or any(v.net == net for v in self.vias)

    # -- name bridging ------------------------------------------------------
    def match_nets(self, short: str) -> list[str]:
        """Board net names matching a short/config name: exact, '/'-prefixed, or
        leaf-equal (handles hierarchical paths like /video/TMDS_CLK_P)."""
        out = []
        for n in self.net_names:
            if n == short or n == f"/{short}" or leaf(n) == short:
                out.append(n)
        return sorted(out)


# --- arc length -------------------------------------------------------------
def _arc_length(a, b, c) -> float:
    """Length of the circular arc through points a, mid b, c."""
    (x1, y1), (x2, y2), (x3, y3) = a, b, c
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:  # collinear → treat as straight
        return math.dist(a, c)
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.dist((ux, uy), a)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    sweep = abs(a3 - a1)
    if sweep > math.pi:
        sweep = 2 * math.pi - sweep
    return r * sweep


def leaf(net: str) -> str:
    """Last path component of a hierarchical net name ('/video/TMDS_CLK_P' -> 'TMDS_CLK_P')."""
    return net.rstrip("/").split("/")[-1]


@dataclass
class TrackSpan:
    """A track's location in the raw file text — for surgical writers (e.g. widening an
    under-floor track) that must edit exactly one segment without re-serialising the board.
    Read-only data; pcbgeom never mutates. `width_span` is the (start, end) of the
    `(width …)` token in the file."""
    kind: str                       # "segment" | "arc"
    net: str
    width: float
    layer: str
    width_span: tuple[int, int]


def iter_track_spans(text: str, table: dict[int, str] | None = None):
    """Yield a TrackSpan for every copper segment/arc in `text`, with the byte offset of
    its `(width …)` token, so a writer can replace just that token."""
    if table is None:
        table = {int(num): name for num, name in _NET_DECL.findall(text)}
    for kw in ("segment", "arc"):
        for m in re.finditer(r"\(" + kw + r"\b", text):
            blk, _ = _balanced(text, m.start())
            wm = _WIDTH.search(blk)
            lay = _LAYER.search(blk)
            if not wm or not lay:
                continue
            yield TrackSpan(kw, _net_of(blk, table), float(wm.group(1)), lay.group(1),
                            (m.start() + wm.start(), m.start() + wm.end()))


# --- parse ------------------------------------------------------------------
def parse(path: str | Path) -> Board:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    board = Board(path=p)

    # net-number table (declarations: `(net 5 "GND")`)
    table = {int(num): name for num, name in _NET_DECL.findall(text)}

    for blk in _iter_blocks(text, "segment"):
        s, e, w = _pt(_START, blk), _pt(_END, blk), _WIDTH.search(blk)
        lay = _LAYER.search(blk)
        if s and e and w and lay:
            board.tracks.append(Track(_net_of(blk, table), lay.group(1),
                                      float(w.group(1)), s, e))

    for blk in _iter_blocks(text, "arc"):
        s, m, e, w = _pt(_START, blk), _pt(_MID, blk), _pt(_END, blk), _WIDTH.search(blk)
        lay = _LAYER.search(blk)
        if s and m and e and w and lay:
            board.tracks.append(Track(_net_of(blk, table), lay.group(1),
                                      float(w.group(1)), s, e, mid=m))

    for blk in _iter_blocks(text, "via"):
        at = _pt(_AT, blk)
        size = _SIZE1.search(blk)
        drill = _DRILL.search(blk)
        lm = _LAYERS.search(blk)
        layers = tuple(re.findall(r'"([^"]*)"', lm.group(1))) if lm else ()
        if at and size:
            board.vias.append(Via(_net_of(blk, table), at,
                                  float(size.group(1)),
                                  float(drill.group(1)) if drill else 0.0, layers))

    for blk in _iter_blocks(text, "zone"):
        lay = _LAYER.search(blk)
        lm = _LAYERS.search(blk)
        layers = ((lay.group(1),) if lay else
                  tuple(re.findall(r'"([^"]*)"', lm.group(1))) if lm else ())
        board.zones.append(Zone(_net_of(blk, table), layers))

    # footprints — capture reference and per-pad nets (no rotated geometry needed)
    for blk in _iter_blocks(text, "footprint"):
        rm = _REF.search(blk)
        ref = rm.group(1) if rm else ""
        idm = re.match(r'\(footprint\s+"([^"]*)"', blk)
        if ref:
            board.footprints[ref] = idm.group(1) if idm else ""
        for pad in _iter_blocks(blk, "pad"):
            pm = re.match(r'\(pad\s+"([^"]*)"', pad)
            net = _net_of(pad, table)
            if net:
                board.pads.append(Pad(ref, pm.group(1) if pm else "", net))

    return board
