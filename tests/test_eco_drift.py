"""Tests for the schematic <-> PCB cross-reference gate (pure — hand-built boards)."""
from pathlib import Path

from kicad_bench.core import pcbgeom
from kicad_bench.quality import eco_drift


def board(pads, footprints=None):
    """A Board carrying only what crossref reads: pads + the placed-ref map."""
    objs = [pcbgeom.Pad(ref=r, number=n, net=net) for r, n, net in pads]
    refs = {r for r, _, _ in pads} if footprints is None else set(footprints)
    return pcbgeom.Board(path=Path("b.kicad_pcb"), pads=objs,
                         footprints={r: "lib:FP" for r in refs})


COUNTS = {
    "VIN_3V3": ["U1.5", "C1.1"],
    "GND": ["U1.2", "C1.2"],
}
PADS = [("U1", "5", "/VIN_3V3"), ("C1", "1", "/VIN_3V3"),
        ("U1", "2", "GND"), ("C1", "2", "GND")]


def _msgs(res):
    return " ".join(f.message for f in res.findings)


def test_identical_sides_pass():
    res = eco_drift.crossref(COUNTS, board(PADS))
    assert res.n_errors == 0 and res.passed
    assert any(f.severity == "ok" for f in res.findings)


def test_leading_slash_is_the_only_normalization():
    """'/VIN_3V3' on the board is 'VIN_3V3' in the netlist — not drift."""
    res = eco_drift.crossref(COUNTS, board(PADS))
    assert "VIN_3V3" not in _msgs(res)


def test_hierarchical_path_kept_whole():
    """Distinct sheets may reuse a leaf name; collapsing to the leaf would alias them."""
    counts = {"video/SW": ["U1.1"], "buck/SW": ["U2.1"]}
    pads = [("U1", "1", "/video/SW"), ("U2", "1", "/buck/SW")]
    assert eco_drift.crossref(counts, board(pads)).n_errors == 0

    # ...and a genuine swap between the two IS caught, which leaf-matching would miss.
    swapped = [("U1", "1", "/buck/SW"), ("U2", "1", "/video/SW")]
    assert eco_drift.crossref(counts, board(swapped)).n_errors == 2


def test_board_only_component_errors():
    """The live case: a cap added straight onto the board, absent from the schematic."""
    pads = PADS + [("Cw", "1", "/VIN_3V3"), ("Cw", "2", "GND")]
    res = eco_drift.crossref(COUNTS, board(pads))
    by_net = {f.where: f for f in res.findings if f.where}
    assert by_net["VIN_3V3"].severity == "error"
    assert "Cw.1" in by_net["VIN_3V3"].detail
    assert by_net["GND"].severity == "error"
    assert not res.passed


def test_renamed_net_errors_in_both_directions():
    pads = [("U1", "5", "/VIN_5V0"), ("C1", "1", "/VIN_5V0"),
            ("U1", "2", "GND"), ("C1", "2", "GND")]
    res = eco_drift.crossref(COUNTS, board(pads))
    by_net = {f.where: f for f in res.findings if f.where}
    assert by_net["VIN_3V3"].severity == "error"      # schematic-only
    assert by_net["VIN_5V0"].severity == "error"      # board-only
    assert "absent from the board" in by_net["VIN_3V3"].message
    assert "absent from the schematic" in by_net["VIN_5V0"].message


def test_moved_pad_errors():
    pads = [("U1", "5", "/VIN_3V3"), ("C1", "1", "GND"),     # C1.1 dragged onto GND
            ("U1", "2", "GND"), ("C1", "2", "GND")]
    res = eco_drift.crossref(COUNTS, board(pads))
    by_net = {f.where: f for f in res.findings if f.where}
    assert by_net["VIN_3V3"].severity == "error"
    assert "C1.1" in by_net["VIN_3V3"].detail


def test_unplaced_component_warns_but_does_not_fail():
    """Mid-layout: C1 exists in the schematic but was never placed. Not drift."""
    pads = [("U1", "5", "/VIN_3V3"), ("U1", "2", "GND")]
    res = eco_drift.crossref(COUNTS, board(pads, footprints=["U1"]))
    assert res.n_errors == 0 and res.passed
    assert any(f.severity == "warn" for f in res.findings)
    assert "unplaced" in _msgs(res)


def test_pad_cleared_on_a_placed_component_errors():
    """Same missing pin, but C1 IS on the board — that's a cleared pad, not an absence."""
    pads = [("U1", "5", "/VIN_3V3"), ("U1", "2", "GND"), ("C1", "2", "GND")]
    res = eco_drift.crossref(COUNTS, board(pads, footprints=["U1", "C1"]))
    by_net = {f.where: f for f in res.findings if f.where}
    assert by_net["VIN_3V3"].severity == "error"
    assert "C1.1" in by_net["VIN_3V3"].detail


def test_unconnected_pseudo_nets_ignored():
    """KiCad escapes an interior '/' as '{slash}' on the board, so these never match."""
    counts = dict(COUNTS, **{"unconnected-(J1-JTDO/SWO-Pad8)": ["J1.8"]})
    pads = PADS + [("J1", "8", "unconnected-(J1-JTDO{slash}SWO-Pad8)")]
    res = eco_drift.crossref(counts, board(pads))
    assert res.n_errors == 0
    assert "unconnected" not in _msgs(res)


def test_unlaid_out_board_skips_cleanly():
    res = eco_drift.crossref(COUNTS, board([]))
    assert res.n_errors == 0 and res.passed
    assert "not laid out" in _msgs(res)


def test_both_sides_empty_is_not_a_pass_claim():
    """Non-vacuous verdict: matching nothing must not read as 'in sync'."""
    res = eco_drift.crossref({}, board([]))
    assert not any(f.severity == "ok" for f in res.findings)
