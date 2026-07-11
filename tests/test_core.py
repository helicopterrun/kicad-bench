"""Pure-logic tests — no kicad-cli required."""
import json
from pathlib import Path

import pytest

from kicad_bench.core import config as cfgmod
from kicad_bench.core import kicad10, schparse


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


# -- config --------------------------------------------------------------
def test_designator_expansion(tmp_path):
    drc = write(tmp_path, "dr.json", json.dumps({
        "nets": {"A": ["VIN_12V", "GND"]},
        "diff_pairs": {"USB": {"members": ["USB_D"]}},
    }))
    cfgfile = write(tmp_path, "kicad-bench.toml", f"""
[contracts]
nets = "from:dr.json"
[contracts.designators]
buck = ["U4", "R22-R29", "L3"]
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.designators["buck"] == {"U4", "L3", "R22", "R23", "R24", "R25", "R26", "R27", "R28", "R29"}
    # nets pulled from json incl. diff-pair member expansion
    assert {"VIN_12V", "GND", "USB_D_P", "USB_D_N", "USB_DP", "USB_DN"} <= cfg.nets


def test_allow_rule_requires_reason(tmp_path):
    bad = write(tmp_path, "kicad-bench.toml", """
[[erc.allow]]
type = "power_pin_not_driven"
ref = "U5"
""")
    with pytest.raises(SystemExit):
        cfgmod.load(bad)


def test_allow_rule_matching(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[[erc.allow]]
type = "power_pin_not_driven"
ref = "U5"
pin = "7"
reason = "GND offset"
""")
    cfg = cfgmod.load(cfgfile)
    rule = cfg.allow_rules[0]
    assert rule.matches("power_pin_not_driven", "msg", "U5", "7")
    assert not rule.matches("power_pin_not_driven", "msg", "U5", "8")
    assert not rule.matches("pin_not_driven", "msg", "U5", "7")


def test_block_for_designator(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[contracts.designators]
buck = ["R22-R23"]
video = ["R40-R41"]
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.block_for_designator("R22") == "buck"
    assert cfg.block_for_designator("R40") == "video"
    assert cfg.block_for_designator("R99") is None


# -- product / multi-board config ---------------------------------------
def test_single_board_backward_compat(tmp_path):
    """A pre-multi-board config loads unchanged: top-level fields resolve as before,
    and one implicit board mirrors them."""
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[project]
root_sch = "Proj/Proj.kicad_sch"
pcb      = "Proj/Proj.kicad_pcb"
[contracts]
nets = ["VIN_12V", "GND"]
[fab]
config  = "dr.json"
stackup = "JLC04161H-7628"
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.root_sch == tmp_path / "Proj/Proj.kicad_sch"
    assert cfg.pcb == tmp_path / "Proj/Proj.kicad_pcb"
    assert cfg.nets == {"VIN_12V", "GND"}
    assert cfg.fab_config == tmp_path / "dr.json"
    assert cfg.stackup == "JLC04161H-7628"
    # exactly one implicit board mirroring the top-level fields
    assert len(cfg.boards) == 1
    assert cfg.active_board == "main"
    assert cfg.boards[0].root_sch == cfg.root_sch
    assert cfg.boards[0].stackup == cfg.stackup
    assert cfg.product is None


def test_multi_board_parse_and_active(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[product]
name = "wildlife-cam"
firmware = "firmware"

[[boards]]
name     = "main-board"
root_sch = "hardware/main-board/main-board.kicad_sch"
pcb      = "hardware/main-board/main-board.kicad_pcb"

[[boards]]
name     = "sensor-board"
root_sch = "hardware/sensor-board/sensor-board.kicad_sch"
pcb      = "hardware/sensor-board/sensor-board.kicad_pcb"
""")
    cfg = cfgmod.load(cfgfile)
    assert [b.name for b in cfg.boards] == ["main-board", "sensor-board"]
    assert cfg.product is not None
    assert cfg.product.name == "wildlife-cam"
    assert cfg.product.firmware == tmp_path / "firmware"
    assert cfg.product.releases == tmp_path / "releases"   # defaulted
    # top-level fields mirror the first board
    assert cfg.active_board == "main-board"
    assert cfg.root_sch == tmp_path / "hardware/main-board/main-board.kicad_sch"


def test_per_board_fab_fallback_and_override(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[fab]
config  = "shared_dr.json"
stackup = "PRODUCT-DEFAULT"

[[boards]]
name = "inherits"
pcb  = "a.kicad_pcb"

[[boards]]
name = "overrides"
pcb  = "b.kicad_pcb"
[boards.fab]
stackup = "BOARD-SPECIAL"
""")
    cfg = cfgmod.load(cfgfile)
    inh, ovr = cfg.board("inherits"), cfg.board("overrides")
    # inherits product-level fab
    assert inh.stackup == "PRODUCT-DEFAULT"
    assert inh.fab_config == tmp_path / "shared_dr.json"
    # overrides stackup but still inherits the product-level config path
    assert ovr.stackup == "BOARD-SPECIAL"
    assert ovr.fab_config == tmp_path / "shared_dr.json"


def test_activate_switches_board_and_rejects_unknown(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[[boards]]
name = "a"
pcb  = "a.kicad_pcb"
[[boards]]
name = "b"
pcb  = "b.kicad_pcb"
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.pcb == tmp_path / "a.kicad_pcb"
    cfg.activate("b")
    assert cfg.active_board == "b"
    assert cfg.pcb == tmp_path / "b.kicad_pcb"
    with pytest.raises(SystemExit):
        cfg.activate("nope")


def test_approved_parts_parsed(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[approved_parts]
csv = "approved_parts.csv"
allow_missing_mpn = true
unapproved = "error"
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.approved_parts is not None
    assert cfg.approved_parts.csv == tmp_path / "approved_parts.csv"
    assert cfg.approved_parts.allow_missing_mpn is True
    assert cfg.approved_parts.unapproved == "error"


def test_approved_parts_absent(tmp_path):
    cfgfile = write(tmp_path, "kicad-bench.toml", """
[project]
pcb = "x.kicad_pcb"
""")
    cfg = cfgmod.load(cfgfile)
    assert cfg.approved_parts is None


# -- kicad10 patch -------------------------------------------------------
def test_kicad10_patch():
    src = '(kicad_sch (version 20250114) (generator_version "9.0") (paper "A3")'
    out = kicad10.patch_paper(kicad10.patch_version(src), "B")
    assert "(version 20260306)" in out
    assert '(generator_version "10.0")' in out
    assert '(paper "B")' in out


# -- schparse ------------------------------------------------------------
def test_references_filters_templates(tmp_path):
    # lib_symbols template "C"/"R" must be filtered; only real designators kept.
    sch = write(tmp_path, "x.kicad_sch", """
(kicad_sch
  (lib_symbols (symbol "Device:C" (property "Reference" "C" (at 0 0 0))))
  (symbol (property "Reference" "C45" (at 1 1 0)))
  (symbol (property "Reference" "U5" (at 2 2 0)))
  (symbol (property "Reference" "RV1" (at 3 3 0)))
  (symbol (property "Reference" "#PWR01" (at 4 4 0))))
""")
    assert set(schparse.references(sch)) == {"C45", "U5", "RV1"}


def test_lib_nicknames():
    table = ('(fp_lib_table (version 7)\n'
             '  (lib (name "House")(type "KiCad")(uri "${KIPRJMOD}/House.pretty")(options "")(descr ""))\n'
             '  (lib (name "0_External")(type "KiCad")(uri "${KIPRJMOD}/0_External.pretty")(options "")(descr "")))')
    assert schparse.lib_nicknames(table) == ["House", "0_External"]
    assert schparse.lib_nicknames("") == []                 # partial/invalid table → no entries
    assert schparse.lib_nicknames("(fp_lib_table (version 7))") == []


def test_input_hier_nets(tmp_path):
    sch = write(tmp_path, "x.kicad_sch", """
(kicad_sch
  (hierarchical_label "VIN_12V" (shape input) (at 0 0 0) (uuid abc))
  (hierarchical_label "VOUT" (shape output) (at 1 0 0) (uuid def))
  (hierarchical_label "SDA" (shape bidirectional) (at 2 0 0) (uuid ghi)))
""")
    assert schparse.input_hier_nets(sch) == {"VIN_12V", "SDA"}
