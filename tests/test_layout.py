"""Pure-logic tests for the routed-geometry tools — no kicad-cli required.

Tiny synthetic .kicad_pcb snippets exercise the pcbgeom parser and the analyze()
functions of the geometric audits. The DRC/subprocess paths (route-coverage ratsnest,
dru-guard regen) are covered only at their pure-logic seams (block_of, the net-in-
description regex, build_maps/classify)."""
import math

from kicad_bench.core import pcbgeom
from kicad_bench.layout import diffpair_audit, track_conformance, route_coverage

# A minimal board: one straight track by NAME, one by NUMBER (+table), an arc, a via,
# a zone, and two footprints with pads.
BOARD = """\
(kicad_pcb (version 20260306) (generator "pcbnew") (generator_version "10.0")
  (net 0 "")
  (net 3 "/B")
  (segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net "/sig/A") (uuid "a"))
  (segment (start 0 0) (end 0 1) (width 0.15) (layer "F.Cu") (net 3) (uuid "b"))
  (arc (start 1 0) (mid 1.2928 0.2928) (end 1.5 1) (width 0.2) (layer "B.Cu") (net "/B") (uuid "c"))
  (via (at 2 2) (size 0.5) (drill 0.25) (layers "F.Cu" "B.Cu") (net "/B") (uuid "d"))
  (zone (net 3) (net_name "/B") (layer "In2.Cu") (uuid "e"))
  (footprint "lib:R" (property "Reference" "R1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "/B"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "/sig/A")))
)
"""


def parse(tmp_path, text=BOARD):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    return pcbgeom.parse(p)


# -- parser ------------------------------------------------------------------
def test_parse_counts_and_net_forms(tmp_path):
    b = parse(tmp_path)
    # 2 segments + 1 arc = 3 tracks
    assert len([t for t in b.tracks if t.mid is None]) == 2
    assert len([t for t in b.tracks if t.mid is not None]) == 1
    assert len(b.vias) == 1 and len(b.zones) == 1
    # net by name and by number both resolve
    assert {t.net for t in b.tracks} == {"/sig/A", "/B"}
    assert b.footprints == {"R1": "lib:R"}


# A multi-layer zone: its own `(layers …)` spans two layers, but each `filled_polygon`
# child names a single layer. A flat regex `.search` grabbed the first *nested* layer and
# under-reported the zone as single-layer; the structural parse reads the zone's own
# `(layers …)`. Regression guard for that fix.
MULTI_ZONE = """\
(kicad_pcb (version 20260206) (generator "pcbnew")
  (net 0 "")
  (net 1 "GND")
  (zone (net 1) (net_name "GND") (layers "In1.Cu" "In2.Cu")
    (filled_polygon (layer "In1.Cu") (pts))
    (filled_polygon (layer "In2.Cu") (pts)))
)
"""


def test_multilayer_zone_reads_own_layers(tmp_path):
    z = parse(tmp_path, MULTI_ZONE).zones
    assert len(z) == 1
    assert z[0].net == "GND"                       # (net 1) resolved via the net table
    assert z[0].layers == ("In1.Cu", "In2.Cu")     # both layers, not just the first nested one


def test_net_length_and_width_stats(tmp_path):
    b = parse(tmp_path)
    assert math.isclose(b.net_length("/sig/A"), 2.0, abs_tol=1e-6)
    # /B = straight 1.0 (numeric-net seg) + arc; arc length > 0
    assert b.net_length("/B") > 1.0
    assert b.width_stats("/sig/A") == (0.2, 0.2, {0.2})
    assert b.layers_used("/B") == {"F.Cu", "B.Cu"}
    assert b.has_copper("/sig/A") and not b.has_copper("/nope")


def test_arc_length_quarter_circle(tmp_path):
    # start(1,0) mid(1.2928,0.2928) end(1.5,1) — not a clean quarter; just assert it
    # exceeds the straight chord and is finite.
    b = parse(tmp_path)
    arc = next(t for t in b.tracks if t.mid is not None)
    chord = math.dist(arc.start, arc.end)
    assert arc.length >= chord > 0


def test_via_fields(tmp_path):
    v = parse(tmp_path).vias[0]
    assert v.net == "/B" and v.at == (2.0, 2.0)
    assert v.size == 0.5 and v.drill == 0.25 and v.layers == ("F.Cu", "B.Cu")


def test_match_nets_and_leaf(tmp_path):
    b = parse(tmp_path)
    assert pcbgeom.leaf("/video/TMDS_CLK_P") == "TMDS_CLK_P"
    assert b.match_nets("A") == ["/sig/A"]          # leaf match through hierarchy
    assert b.match_nets("B") == ["/B"]              # '/'-prefixed match


def test_iter_track_spans_offsets_enable_surgical_edit(tmp_path):
    b = parse(tmp_path)
    text = (tmp_path / "b.kicad_pcb").read_text()
    spans = [s for s in pcbgeom.iter_track_spans(text) if s.net == "/B" and s.width == 0.15]
    assert len(spans) == 1
    s, e = spans[0].width_span
    assert text[s:e] == "(width 0.15)"
    edited = text[:s] + "(width 0.4)" + text[e:]
    # only that token changed; markers intact
    assert edited.count("(width 0.15)") == 0
    assert "(version 20260306)" in edited and '(generator_version "10.0")' in edited


# -- diffpair-audit ----------------------------------------------------------
DP_FAB = {"diff_pairs": {"LVDS": {"target_impedance_ohm": 100, "track_width": 0.1128,
                                  "gap": 0.2, "members": ["CLK", "D0"]}}}

DP_BOARD = """\
(kicad_pcb (version 20260306) (generator "pcbnew")
  (segment (start 0 0) (end 10 0) (width 0.1128) (layer "F.Cu") (net "/CLK_P") (uuid "1"))
  (segment (start 0 1) (end 10.5 1) (width 0.1128) (layer "F.Cu") (net "/CLK_N") (uuid "2"))
  (segment (start 0 2) (end 5 2) (width 0.1128) (layer "F.Cu") (net "/D0_P") (uuid "3"))
  (segment (start 5 2) (end 8 2) (width 0.1128) (layer "In1.Cu") (net "/D0_P") (uuid "4"))
  (segment (start 0 3) (end 8 3) (width 0.1128) (layer "F.Cu") (net "/D0_N") (uuid "5"))
)
"""


def test_diffpair_skew_and_layer(tmp_path):
    b = parse(tmp_path, DP_BOARD)
    res = diffpair_audit.analyze(b, DP_FAB, max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    msgs = " ".join(f.message for f in res.findings)
    # CLK: P=10, N=10.5 -> skew 0.5 > 0.15 budget
    assert "skew" in msgs
    # D0_P spans F.Cu + In1.Cu -> layer flag (both sides routed, so not "half-done")
    assert "single outer layer" in msgs


# E_P is routed; E_N exists only as a pad (no copper) -> half-done.
HALF_BOARD = """\
(kicad_pcb (version 20260306)
  (segment (start 0 0) (end 4 0) (width 0.1) (layer "F.Cu") (net "/E_P") (uuid "1"))
  (footprint "lib:U" (property "Reference" "U1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "/E_N")))
)
"""


def test_diffpair_half_done(tmp_path):
    fab = {"diff_pairs": {"X": {"track_width": 0.1, "members": ["E"]}}}
    res = diffpair_audit.analyze(parse(tmp_path, HALF_BOARD), fab, 0.15, 0.02, 5.0)
    assert any("half-done" in f.message for f in res.findings)


# -- track-conformance -------------------------------------------------------
TC_FAB = {
    "domains": {"PWR_3V3": {"track_width": 0.4, "via_diameter": 0.5, "via_drill": 0.25}},
    "nets": {"PWR_3V3": ["VIN_3V3"]},
    "diff_pairs": {},
}

TC_BOARD = """\
(kicad_pcb (version 20260306)
  (segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/VIN_3V3") (uuid "1"))
  (zone (net 3) (net_name "/POURED") (layer "In2.Cu") (uuid "z"))
  (footprint "lib:C" (property "Reference" "C1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "In2.Cu") (net "/POURED")))
)
"""


def test_conformance_flags_underfloor_and_respects_poured(tmp_path):
    b = parse(tmp_path, TC_BOARD)
    res = track_conformance.analyze(b, TC_FAB, default_floor=0.2)
    # VIN_3V3 routed at 0.25 < 0.4 floor -> a warn
    assert any(f.severity == "warn" and "VIN_3V3" in f.message for f in res.findings)
    # POURED has a zone, no tracks -> not flagged for width
    assert not any("POURED" in f.message and f.severity == "warn" for f in res.findings)


def test_build_maps_and_classify():
    nd, floor, via = track_conformance.build_maps({
        "domains": {"PWR_12V": {"track_width": 0.5, "via_diameter": 0.6, "via_drill": 0.3}},
        "nets": {"PWR_12V": ["VIN_12V"]},
        "diff_pairs": {"USB": {"track_width": 0.1443, "members": ["USB_D"]}},
    })
    assert floor["PWR_12V"] == 0.5 and floor["USB"] == 0.1443
    assert track_conformance.classify("/VIN_12V", nd) == "PWR_12V"
    assert track_conformance.classify("/touch/USB_DP", nd) == "USB"
    assert track_conformance.classify("/whatever", nd) == "Default"


# -- route-coverage seams ----------------------------------------------------
def test_block_of():
    assert route_coverage.block_of("/video/TMDS_CLK_P") == "video"
    assert route_coverage.block_of("/VIN_12V") == "(top)"
    assert route_coverage.block_of("GND") == "(top)"


def test_net_in_description_regex():
    m = route_coverage._NET_IN_DESC.search("Pad 1 [/VIN_3V3] of U2 on F.Cu")
    assert m.group(1) == "/VIN_3V3"


# ---------------------------------------------------------------------------
# stackup parsing (the impedance geometry source)
# ---------------------------------------------------------------------------

STACKUP_BOARD = """\
(kicad_pcb (version 20260306) (generator "pcbnew")
  (setup
    (stackup
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (epsilon_r 3.3))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.0918) (epsilon_r 4.6))
      (layer "In1.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 2" (type "core") (thickness 1.265) (epsilon_r 4.7))
      (layer "In2.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 3" (type "prepreg") (thickness 0.0918) (epsilon_r 4.6))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (net 0 "")
)
"""


def test_stackup_parsed(tmp_path):
    b = parse(tmp_path, STACKUP_BOARD)
    assert b.stackup is not None
    assert b.stackup.copper_layers() == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_stackup_microstrip_geometry(tmp_path):
    """F.Cu references the nearest copper below it, across the prepreg only."""
    b = parse(tmp_path, STACKUP_BOARD)
    assert b.stackup.microstrip_geometry("F.Cu") == (0.0918, 4.6, 0.035)


def test_stackup_bottom_layer_walks_the_other_way(tmp_path):
    """B.Cu has no dielectric below it — the reference plane is inboard, above."""
    b = parse(tmp_path, STACKUP_BOARD)
    assert b.stackup.microstrip_geometry("B.Cu") == (0.0918, 4.6, 0.035)


def test_stackup_weighted_er_across_a_sandwich(tmp_path):
    """In1.Cu sits over the thick core alone, so its epsilon_r is that layer's."""
    b = parse(tmp_path, STACKUP_BOARD)
    h, er, t = b.stackup.microstrip_geometry("In1.Cu")
    assert (h, er, t) == (1.265, 4.7, 0.0152)


def test_stackup_unknown_layer_is_none(tmp_path):
    b = parse(tmp_path, STACKUP_BOARD)
    assert b.stackup.microstrip_geometry("In7.Cu") is None


def test_board_without_stackup_is_none(tmp_path):
    assert parse(tmp_path).stackup is None


def test_stackup_missing_epsilon_r_skips_rather_than_guessing(tmp_path):
    text = STACKUP_BOARD.replace(
        '(layer "dielectric 1" (type "prepreg") (thickness 0.0918) (epsilon_r 4.6))',
        '(layer "dielectric 1" (type "prepreg") (thickness 0.0918))')
    b = parse(tmp_path, text)
    assert b.stackup is not None
    assert b.stackup.microstrip_geometry("F.Cu") is None


def test_diffpair_impedance_skips_without_a_stackup(tmp_path):
    """No stackup -> one aggregated info line, never a guessed FR4 warning."""
    res = diffpair_audit.analyze(parse(tmp_path, DP_BOARD), DP_FAB,
                                 max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    msgs = " ".join(f.message for f in res.findings)
    assert "without a computed impedance" in msgs
    assert "computed" not in " ".join(
        f.message for f in res.findings if f.severity == "warn")


DP_STACK_FAB = {"diff_pairs": {"LVDS": {"target_impedance_ohm": 100,
                                        "track_width": 0.1128, "gap": 0.2032,
                                        "members": ["CLK"]}}}


def _stacked_dp_board(tmp_path):
    """DP_BOARD's copper plus a real stackup, so impedance can be computed."""
    text = DP_BOARD.replace('(kicad_pcb (version 20260306) (generator "pcbnew")',
                            '(kicad_pcb (version 20260306) (generator "pcbnew")\n'
                            + STACKUP_BOARD.split("(setup", 1)[1]
                              .rsplit("(net 0", 1)[0].join(("  (setup", "")), 1)
    return parse(tmp_path, text)


def test_diffpair_computes_impedance_from_the_board_stackup(tmp_path):
    res = diffpair_audit.analyze(_stacked_dp_board(tmp_path), DP_STACK_FAB,
                                 max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    msgs = " ".join(f.message for f in res.findings)
    assert "computed 98.9" in msgs
    # 98.9 vs a 100 ohm target is inside the default 10% band -> no warning.
    assert not any("outside" in f.message for f in res.findings)


def test_diffpair_warns_when_impedance_misses_target(tmp_path):
    fab = {"diff_pairs": {"LVDS": {**DP_STACK_FAB["diff_pairs"]["LVDS"],
                                   "target_impedance_ohm": 50}}}
    res = diffpair_audit.analyze(_stacked_dp_board(tmp_path), fab,
                                 max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    assert any(f.severity == "warn" and "outside" in f.message for f in res.findings)
    assert res.n_errors == 0          # physics is warn-biased, never an error


def test_diffpair_skips_when_the_pour_is_closer_than_the_plane(tmp_path):
    """Coplanar-dominated geometry: the microstrip model doesn't describe it."""
    fab = {"diff_pairs": {"LVDS": {**DP_STACK_FAB["diff_pairs"]["LVDS"],
                                   "coplanar_clearance": 0.05}}}   # < h of 0.0918
    res = diffpair_audit.analyze(_stacked_dp_board(tmp_path), fab,
                                 max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    msgs = " ".join(f.message for f in res.findings)
    assert "computed 98.9" not in msgs
    assert "without a computed impedance" in msgs


def test_diffpair_still_computes_when_the_plane_is_closer(tmp_path):
    """Pour farther than the plane -> the plane dominates and the model holds."""
    fab = {"diff_pairs": {"LVDS": {**DP_STACK_FAB["diff_pairs"]["LVDS"],
                                   "coplanar_clearance": 0.2032}}}  # > h of 0.0918
    res = diffpair_audit.analyze(_stacked_dp_board(tmp_path), fab,
                                 max_skew=0.15, width_tol=0.02, max_bus_spread=5.0)
    assert "computed 98.9" in " ".join(f.message for f in res.findings)
