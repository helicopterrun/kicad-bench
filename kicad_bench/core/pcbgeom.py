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

from . import sexp

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


# --- raw-text scanners for iter_track_spans --------------------------------
# parse() below reads the structured S-expression tree; these regexes remain only for
# iter_track_spans, which must locate the exact BYTES of a `(width …)` token to rewrite one
# track in place without re-serialising the board.
_WIDTH = re.compile(r"\(width\s+(-?[\d.]+)\)")
_LAYER = re.compile(r'\(layer\s+"([^"]*)"\)')
# net forms, most specific first
_NET_NUMNAME = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_NET_NAME = re.compile(r'\(net\s+"([^"]*)"\)')
_NET_NUM = re.compile(r"\(net\s+(\d+)\)")
_NET_DECL = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')


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
class PadGeom:
    """One pad in a footprint's LOCAL frame (offsets before the footprint is rotated
    or side-mirrored). Used only for the pin-1 orientation visual, never by the net /
    geometry audits."""
    number: str
    lx: float          # local x offset from the footprint origin (mm)
    ly: float          # local y offset (mm)
    lrot: float        # local pad rotation (deg)
    w: float           # pad width (mm)
    h: float           # pad height (mm)
    shape: str         # rect | roundrect | circle | oval | …


@dataclass
class Placement:
    """A footprint's placement on the board: origin, rotation, side, value, and the
    local pad geometry. Additive to the net-oriented parse — the pin-1 orientation
    feature reads this; nothing else does."""
    ref: str
    fpid: str
    x: float           # footprint origin, board coords (mm)
    y: float
    rot: float         # footprint rotation (deg, as stored)
    side: str          # "top" (F.Cu) | "bottom" (B.Cu)
    value: str
    pads: list[PadGeom] = field(default_factory=list)

    def pad(self, number: str) -> "PadGeom | None":
        return next((p for p in self.pads if p.number == number), None)


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
    placements: dict[str, "Placement"] = field(default_factory=dict)  # ref -> Placement

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
# Element extraction walks the parsed S-expression tree (see core.sexp): each field is a
# named DIRECT child of its element, so — unlike a flat regex `.search` — a footprint's own
# `(at …)` can't be confused with a pad's, and a reordered/added token can't desync the read.
# `iter_track_spans` (byte-offset surgical edits) stays on the raw text below.
def _net_of_node(node, table: dict[int, str]) -> str:
    """Resolve an element's net name, mirroring the three stored forms:
    `(net N "name")` → name · `(net "name")` → name · `(net N)` → table[N]."""
    n = sexp.first(node, "net")
    if n is None:
        return ""
    if len(n) >= 3:                       # (net N "name")
        return sexp.sym(n[2])
    if len(n) == 2:
        return n[1] if isinstance(n[1], str) else table.get(int(n[1]), "")
    return ""


def _xy(node, key: str):
    n = sexp.first(node, key)
    return (float(n[1]), float(n[2])) if n and len(n) >= 3 else None


def _num(node, key: str):
    """The first number of `(key N …)`, or None if the field is absent (presence, not
    truthiness — a legitimate 0 width must not read as missing)."""
    n = sexp.first(node, key)
    return float(n[1]) if n and len(n) >= 2 else None


def _prop(node, name: str) -> str:
    """A footprint `(property "<name>" "<value>")` value, or ""."""
    for p in sexp.children(node, "property"):
        if len(p) >= 3 and sexp.sym(p[1]) == name:
            return sexp.sym(p[2])
    return ""


def parse(path: str | Path) -> Board:
    p = Path(path)
    board = Board(path=p)
    root = sexp.load(p.read_text(encoding="utf-8", errors="ignore"))

    # net-number table from every `(net N "name")` (top-level declarations + inline).
    table: dict[int, str] = {}
    for n in sexp.walk(root, "net"):
        if len(n) >= 3 and isinstance(n[1], (int, float)):
            table[int(n[1])] = sexp.sym(n[2])

    for node in root[1:]:
        kind = sexp.head(node)
        if kind in ("segment", "arc"):
            s, e, w, lay = _xy(node, "start"), _xy(node, "end"), _num(node, "width"), sexp.first(node, "layer")
            mid = _xy(node, "mid") if kind == "arc" else None
            if s and e and w is not None and lay and (kind == "segment" or mid):
                board.tracks.append(Track(_net_of_node(node, table), sexp.sym(lay[1]), w, s, e, mid=mid))
        elif kind == "via":
            at, size = _xy(node, "at"), _num(node, "size")
            lm = sexp.first(node, "layers")
            if at and size is not None:
                layers = tuple(sexp.sym(x) for x in lm[1:]) if lm else ()
                board.vias.append(Via(_net_of_node(node, table), at, size, _num(node, "drill") or 0.0, layers))
        elif kind == "zone":
            lay, lm = sexp.first(node, "layer"), sexp.first(node, "layers")
            layers = ((sexp.sym(lay[1]),) if lay else
                      tuple(sexp.sym(x) for x in lm[1:]) if lm else ())
            board.zones.append(Zone(_net_of_node(node, table), layers))
        elif kind == "footprint":
            _footprint(node, board, table)

    return board


def _footprint(node, board: Board, table: dict[int, str]) -> None:
    """Capture a footprint's ref→fpid, its net-bearing pads, and its placement geometry."""
    ref = _prop(node, "Reference")
    fpid = sexp.sym(node[1]) if len(node) >= 2 else ""
    if ref:
        board.footprints[ref] = fpid
    for pad in sexp.children(node, "pad"):
        net = _net_of_node(pad, table)
        if net:
            board.pads.append(Pad(ref, sexp.sym(pad[1]) if len(pad) >= 2 else "", net))
    if ref:
        board.placements[ref] = _placement(node, ref, fpid)


def _placement(node, ref: str, fpid: str) -> "Placement":
    """A footprint's origin/rotation/side/value + local pad geometry, read from its direct
    children (the footprint's own `at`/`layer`, not a pad's or property's)."""
    at = sexp.first(node, "at")
    lay = sexp.first(node, "layer")
    pl = Placement(
        ref=ref, fpid=fpid,
        x=float(at[1]) if at and len(at) >= 3 else 0.0,
        y=float(at[2]) if at and len(at) >= 3 else 0.0,
        rot=float(at[3]) if at and len(at) >= 4 else 0.0,
        side="bottom" if (lay and sexp.sym(lay[1]) == "B.Cu") else "top",
        value=_prop(node, "Value"),
    )
    for pad in sexp.children(node, "pad"):
        pat, psz = sexp.first(pad, "at"), sexp.first(pad, "size")
        pl.pads.append(PadGeom(
            number=sexp.sym(pad[1]) if len(pad) >= 2 else "",
            lx=float(pat[1]) if pat and len(pat) >= 3 else 0.0,
            ly=float(pat[2]) if pat and len(pat) >= 3 else 0.0,
            lrot=float(pat[3]) if pat and len(pat) >= 4 else 0.0,
            w=float(psz[1]) if psz and len(psz) >= 3 else 0.0,
            h=float(psz[2]) if psz and len(psz) >= 3 else 0.0,
            shape=sexp.sym(pad[3]) if len(pad) >= 4 else "",
        ))
    return pl
