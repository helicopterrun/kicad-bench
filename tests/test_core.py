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
(lib_symbols (symbol "Device:C" (property "Reference" "C" (at 0 0 0))))
(symbol (property "Reference" "C45" (at 1 1 0)))
(symbol (property "Reference" "U5" (at 2 2 0)))
(symbol (property "Reference" "RV1" (at 3 3 0)))
(symbol (property "Reference" "#PWR01" (at 4 4 0)))
""")
    assert set(schparse.references(sch)) == {"C45", "U5", "RV1"}


def test_input_hier_nets(tmp_path):
    sch = write(tmp_path, "x.kicad_sch", """
(hierarchical_label "VIN_12V" (shape input) (at 0 0 0) (uuid abc))
(hierarchical_label "VOUT" (shape output) (at 1 0 0) (uuid def))
(hierarchical_label "SDA" (shape bidirectional) (at 2 0 0) (uuid ghi))
""")
    assert schparse.input_hier_nets(sch) == {"VIN_12V", "SDA"}
