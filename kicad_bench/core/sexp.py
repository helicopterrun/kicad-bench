"""sexp.py — a thin, format-agnostic S-expression reader over `sexpdata`.

KiCad design files (`.kicad_pcb`, `.kicad_sch`, …) are S-expressions: nested
parenthesised lists whose head is a symbol — `(footprint "lib:R" (layer "F.Cu")
(at 1 2 90) (pad "1" smd rect (at 0 0) …))`. `sexpdata` turns the text into nested
Python lists (a bare token → `Symbol`, a quoted token → `str`, a number → `int`/`float`).

This module adds the handful of accessors kicad-bench needs to walk that tree **by
structure** — "this footprint's own `at`", "this pad's `net`" — instead of by regex and
positional field-order. That makes the parse robust to KiCad reordering or adding tokens:
a new field can't be mistaken for the one after it, the way a flat `.search()` can.

Read-only. It never serialises or mutates — writers that must edit exact bytes (e.g.
`pcbgeom.iter_track_spans`) keep working on the raw text.
"""
from __future__ import annotations

import sexpdata

# A parsed node is a list `[Symbol('head'), *args]`; atoms are str / int / float / Symbol.
Node = list


def load(text: str) -> Node:
    """Parse S-expression text into a nested-list tree."""
    return sexpdata.loads(text)


def sym(x) -> str:
    """The string value of a head/atom — unwraps `Symbol('at')` → `'at'`, passes str
    through, and stringifies numbers so callers can compare heads uniformly."""
    if isinstance(x, sexpdata.Symbol):
        return x.value()
    return x if isinstance(x, str) else str(x)


def head(node) -> str | None:
    """The head symbol of a node, or None if it isn't a non-empty list."""
    if isinstance(node, list) and node:
        return sym(node[0])
    return None


def children(node, key: str) -> list[Node]:
    """Direct child lists of `node` whose head is `key` (order preserved)."""
    return [c for c in node if isinstance(c, list) and c and sym(c[0]) == key]


def first(node, key: str) -> Node | None:
    """The first direct child list with head `key`, or None."""
    for c in node:
        if isinstance(c, list) and c and sym(c[0]) == key:
            return c
    return None


def walk(node, key: str):
    """Yield every descendant list with head `key`, at any depth (pre-order)."""
    if isinstance(node, list):
        if node and sym(node[0]) == key:
            yield node
        for c in node:
            if isinstance(c, list):
                yield from walk(c, key)
