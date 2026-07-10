"""Pure-logic tests for the BOM assembly module + the pcbgeom placement extension.

No kicad-cli required: the schematic (netlist) source degrades to empty when there's no
root_sch, so build() is exercised over PCB⋈BOM only. A tiny synthetic .kicad_pcb drives
the placement parse; a small openpyxl workbook drives the BOM read.
"""
import xml.dom.minidom as minidom

import openpyxl
import pytest

from kicad_bench import bom_assembly as ba
from kicad_bench.core import pcbgeom
from kicad_bench.core.config import Config
from kicad_bench.core.pcbgeom import PadGeom, Placement


# ── pcbgeom placement extension ───────────────────────────────────────────────
# Footprints with a top-level (at x y rot), a (layer …) (side), a Value property, and a
# pad-1 with a local offset. Includes a bottom-side part and one with tracks/pads-with-nets
# so the pre-existing net extraction is regression-checked.
PCB = """\
(kicad_pcb (version 20260306) (generator "pcbnew") (generator_version "10.0")
  (net 0 "")
  (net 3 "/VIN")
  (segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net "/VIN") (uuid "s1"))
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu")
    (at 141 146.5 180)
    (property "Reference" "R1" (at 0 -1 0))
    (property "Value" "10k" (at 0 1 0))
    (pad "1" smd roundrect (at -0.51 0 180) (size 0.54 0.64) (layers "F.Cu" "F.Mask") (net 3 "/VIN"))
    (pad "2" smd roundrect (at 0.51 0 180) (size 0.54 0.64) (layers "F.Cu" "F.Mask")))
  (footprint "Package_SO:SOT23-6" (layer "B.Cu")
    (at 158.8 166 -90)
    (property "Reference" "U1" (at 0 -2 0))
    (property "Value" "USBLC6-2SC6" (at 0 2 0))
    (pad "1" smd rect (at -1.3 -0.95 270) (size 0.9 0.55) (layers "B.Cu" "B.Mask")))
  (footprint "Capacitor_SMD:C_0402" (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu")))
)
"""


def parse(tmp_path, text=PCB):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    return pcbgeom.parse(p)


def test_placement_origin_rotation_side_value(tmp_path):
    b = parse(tmp_path)
    r1 = b.placements["R1"]
    assert (r1.x, r1.y, r1.rot) == (141.0, 146.5, 180.0)
    assert r1.side == "top" and r1.value == "10k" and r1.fpid == "Resistor_SMD:R_0402"
    u1 = b.placements["U1"]
    assert u1.side == "bottom" and u1.rot == -90.0 and u1.value == "USBLC6-2SC6"


def test_placement_pad1_local_geometry(tmp_path):
    b = parse(tmp_path)
    p1 = b.placements["R1"].pad("1")
    assert (p1.lx, p1.ly, p1.lrot) == (-0.51, 0.0, 180.0)
    assert (p1.w, p1.h) == (0.54, 0.64) and p1.shape == "roundrect"
    # a footprint's own (at …) must not be confused with a property's/pad's (at …)
    c1 = b.placements["C1"]
    assert (c1.x, c1.y, c1.rot) == (100.0, 100.0, 0.0)


def test_placement_is_additive_regression(tmp_path):
    """The pre-existing net/footprint extraction is unchanged by the placement addition."""
    b = parse(tmp_path)
    assert b.footprints == {"R1": "Resistor_SMD:R_0402", "U1": "Package_SO:SOT23-6",
                            "C1": "Capacitor_SMD:C_0402"}
    assert {(p.ref, p.number, p.net) for p in b.pads} == {("R1", "1", "/VIN")}
    assert len(b.tracks) == 1 and b.tracks[0].net == "/VIN"


# ── normalize_value ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("group", [
    ("1R0", "1.0R", "1"),
    ("100nF", "0.1uF", "0.1µF"),
    ("10k", "10K", "10000"),
    ("4k7", "4.7k"),
    ("22pF NP0", "22pF"),
    ("10uF/25V", "10uF"),
])
def test_normalize_value_equivalences(group):
    norms = {ba.normalize_value(x) for x in group}
    assert len(norms) == 1, f"{group} -> {norms}"


def test_normalize_value_distinguishes_real_differences():
    assert ba.normalize_value("10k") != ba.normalize_value("4.7k")
    assert ba.normalize_value("1M") != ba.normalize_value("1m")   # mega vs milli (case matters)
    assert ba.normalize_value("100nF") != ba.normalize_value("100uF")
    assert ba.normalize_value("") == ""


# ── value_check ───────────────────────────────────────────────────────────────
def test_value_check_match_format_and_mismatch():
    assert ba._value_check("10k", "10k", "10k")["level"] == "match"
    fd = ba._value_check("1R0", "1.0R", "1")
    assert fd["level"] == "format-diff" and not fd["sch_pcb_mismatch"]
    mm = ba._value_check("10k", "4.7k", "10k")
    assert mm["level"] == "mismatch" and mm["sch_pcb_mismatch"]


def test_value_check_ignores_absent_sources():
    # BOM-only value, no sch/pcb — still a clean match on the one present source
    assert ba._value_check(None, None, "10k")["level"] == "match"
    # sch vs bom disagree but sch/pcb do not both exist → not a sch_pcb_mismatch
    r = ba._value_check("10k", None, "100k")
    assert r["level"] == "mismatch" and not r["sch_pcb_mismatch"]


def test_orientation_sensitive_prefixes():
    assert ba._orientation_sensitive("U3") and ba._orientation_sensitive("ESD2")
    assert ba._orientation_sensitive("D1") and ba._orientation_sensitive("J5")
    assert not ba._orientation_sensitive("R10") and not ba._orientation_sensitive("C4")


# ── orient_svg ────────────────────────────────────────────────────────────────
def _pl(side, rot=0.0):
    return Placement(ref="U1", fpid="lib:U", x=0, y=0, rot=rot, side=side, value="x",
                     pads=[PadGeom("1", lx=1.0, ly=0.0, lrot=0.0, w=0.5, h=0.5, shape="rect"),
                           PadGeom("2", lx=-1.0, ly=0.0, lrot=0.0, w=0.5, h=0.5, shape="rect")])


def test_orient_svg_wellformed_and_highlights_pin1():
    svg = ba.orient_svg(_pl("top"))
    minidom.parseString(svg)                      # well-formed XML
    assert "#e5484d" in svg                       # pad-1 highlight color
    assert ">1</text>" in svg                     # pad-1 label
    assert "top · 0°" in svg                       # side + rotation annotation


def test_orient_svg_bottom_mirrors_x():
    # pad 1 sits at local +x; top places it at +1, bottom mirrors to -1
    assert "translate(1.000 0.000)" in ba.orient_svg(_pl("top"))
    assert "translate(-1.000 0.000)" in ba.orient_svg(_pl("bottom"))


# ── alternates: storage, merge, export ────────────────────────────────────────
def test_alternates_roundtrip_and_delete(tmp_path):
    key = "10uF|C_0805"
    ba.write_alternates(tmp_path, key, [{"mpn": "C1", "lcsc": "C100", "reason": "cheap"}])
    assert ba.read_alternates(tmp_path)[key][0]["mpn"] == "C1"
    assert (tmp_path / ".cockpit" / "bom_alternates.json").is_file()
    ba.write_alternates(tmp_path, key, [])        # empty list deletes the key
    assert key not in ba.read_alternates(tmp_path)


def test_alternates_write_ignores_blank_mpn(tmp_path):
    ba.write_alternates(tmp_path, "k", [{"mpn": "", "reason": "x"}, {"mpn": "GOOD"}])
    assert [a["mpn"] for a in ba.read_alternates(tmp_path)["k"]] == ["GOOD"]


def test_library_alternates_require_footprint_compatibility():
    # two 10k parts: a pot (with an alt) and a resistor. The pot's alternate must NOT
    # leak onto the resistor line just because both values normalize to 10000.
    # the real approved package is free-text ("SMD-3P,…") that shares no token with the
    # KiCad footprint — so matching must fall back to the MPN, which carries "3224W".
    approved = {
        ba.normalize_value("10k"): [
            {"mpn": "3224W-1-103E", "package": "SMD-3P,4.8x3.5mm", "value": "10k",
             "alt_mpn_1": "TC33X-2-103E", "alt_mpn_2": "", "notes": "trimpot"},
        ],
    }
    prim, alts = ba._library_alternates("10k", "R_0402_1005Metric", approved)
    assert (prim, alts) == ("", [])                               # resistor line: no match
    prim, alts = ba._library_alternates("10k", "Potentiometer_Bourns_3224W_Vertical", approved)
    assert prim == "3224W-1-103E" and [a["mpn"] for a in alts] == ["TC33X-2-103E"]


def test_library_alternates_distinctive_value_matches_without_package():
    approved = {ba.normalize_value("USBLC6-2SC6"): [
        {"mpn": "USBLC6-2SC6", "package": "", "value": "USBLC6-2SC6",
         "alt_mpn_1": "PRTR5V0U2X", "alt_mpn_2": ""}]}
    prim, alts = ba._library_alternates("USBLC6-2SC6", "SOT23-6L_STM", approved)
    assert prim == "USBLC6-2SC6" and [a["mpn"] for a in alts] == ["PRTR5V0U2X"]


def test_merge_alternates_dedupes_user_wins():
    lib = [{"mpn": "ALT1", "source": "approved"}, {"mpn": "SHARED", "source": "approved"}]
    user = [{"mpn": "SHARED", "lcsc": "C9", "source": "user"}]
    merged = ba._merge_alternates(lib, user)
    mpns = [a["mpn"] for a in merged]
    assert mpns == ["SHARED", "ALT1"]             # user first, dedup by MPN
    assert next(a for a in merged if a["mpn"] == "SHARED")["source"] == "user"


# ── build() + export_csv over PCB⋈BOM (no schematic needed) ───────────────────
def _make_bom(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Comment", "Designator", "Footprint", "LCSC PN", "Section", "Qty", "Status"])
    for r in rows:
        ws.append(r)
    wb.save(path)


def _cfg(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    bom = tmp_path / "bom.xlsx"
    _make_bom(bom, [
        ["10k", "R1", "R_0402", "C25744", "Power", 1, "Confirmed"],
        ["0.1uF", "C1", "C_0402", "C1525", "Power", 1, "Confirmed"],   # format-diff vs pcb 100nF
        ["USBLC6-2SC6", "U1", "SOT23-6", "C7437410", "USB", 1, "Confirmed"],
    ])
    return Config(path=tmp_path / "x.toml", root=tmp_path,
                  pcb=tmp_path / "b.kicad_pcb", bom=bom, root_sch=None)


def test_build_joins_and_flags(tmp_path):
    data = ba.build(_cfg(tmp_path))
    rows = {r["value"]: r for r in data["rows"]}
    assert data["summary"]["parts"] == 3 and data["summary"]["has_pcb"] and data["summary"]["has_bom"]
    assert not data["summary"]["has_sch"]                     # no root_sch → sch column absent
    # C1: BOM "0.1uF" vs PCB "100nF" — same value, different spelling
    assert rows["0.1uF"]["value_check"] == "format-diff"
    # U1 is orientation-sensitive; R1/C1 are not
    assert rows["USBLC6-2SC6"]["orientation_sensitive"]
    assert not rows["10k"]["orientation_sensitive"]


def test_build_surfaces_bom_vs_pcb_value_mismatch(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    bom = tmp_path / "bom.xlsx"
    _make_bom(bom, [["4.7k", "R1", "R_0402", "C1", "P", 1, "Confirmed"]])  # PCB says 10k
    cfg = Config(path=tmp_path / "x.toml", root=tmp_path,
                 pcb=tmp_path / "b.kicad_pcb", bom=bom, root_sch=None)
    data = ba.build(cfg)
    assert data["summary"]["value_mismatches"] == 1


def test_export_csv_has_alternates_column(tmp_path):
    cfg = _cfg(tmp_path)
    ba.write_alternates(tmp_path, ba.group_key("USBLC6-2SC6", "SOT23-6"),
                        [{"mpn": "PRTR5V0U2X", "lcsc": "C86027", "reason": "second source"}])
    # drop the BOM cache is not needed — build() reads alternates fresh each call
    csv_text = ba.export_csv(cfg)
    header, *lines = csv_text.splitlines()
    assert "Approved Alternates" in header
    usb_line = next(ln for ln in lines if "USBLC6" in ln)
    assert "PRTR5V0U2X" in usb_line
