"""Tests for the S-expression reader (core.sexp) that pcbgeom/schparse parse over."""
from kicad_bench.core import sexp

SRC = '(kicad_pcb (version 20260206) (net 0 "") (net 3 "/VIN")\n' \
      '  (footprint "lib:R" (layer "F.Cu") (at 1 2 90)\n' \
      '    (property "Reference" "R1") (property "Value" "10k")\n' \
      '    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (net 3 "/VIN"))))'


def test_load_and_head():
    root = sexp.load(SRC)
    assert sexp.head(root) == "kicad_pcb"
    assert sexp.head("not-a-list") is None and sexp.head([]) is None


def test_sym_unwraps_symbols_strings_numbers():
    root = sexp.load(SRC)
    fp = sexp.first(root, "footprint")
    assert sexp.sym(fp[1]) == "lib:R"                 # quoted string
    assert sexp.sym(sexp.first(fp, "layer")[0]) == "layer"   # symbol head
    assert sexp.sym(sexp.first(fp, "at")[1]) == "1"          # number → str


def test_children_and_first():
    fp = sexp.first(sexp.load(SRC), "footprint")
    props = sexp.children(fp, "property")
    assert [sexp.sym(p[1]) for p in props] == ["Reference", "Value"]
    assert sexp.first(fp, "nonexistent") is None
    # first() returns a DIRECT child only — the footprint's own `at`, not the pad's
    assert sexp.first(fp, "at")[1:] == [1, 2, 90]


def test_walk_is_recursive():
    root = sexp.load(SRC)
    # three `net` nodes total: two top-level declarations + one inside the pad
    nets = list(sexp.walk(root, "net"))
    assert len(nets) == 3
    # children() (direct) sees only the two top-level declarations
    assert len(sexp.children(root, "net")) == 2
