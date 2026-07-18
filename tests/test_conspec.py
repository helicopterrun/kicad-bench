"""Tests for the extracted-constraints store (core/conspec.py) — pure, no LLM."""
import json
from pathlib import Path

import pytest

from kicad_bench.core import conspec


class FakeCfg:
    """Just enough of Config for conspec: .root + .raw."""
    def __init__(self, root, extra=None):
        self.root = root
        self.raw = {"constraints": {"extra_index_dirs": [str(p) for p in (extra or [])]}}


def _write(index_dir: Path, mpns, **over):
    index_dir.mkdir(parents=True, exist_ok=True)
    data = {"schema": conspec.SCHEMA_VERSION, "mpns": mpns,
            "datasheet_sha256": "abc", "extracted_by": "llm", "model": "m",
            "package": None, "pins": [], "abs_max": [], "recommended": [],
            "spec": {}, "source_pages": {}}
    data.update(over)
    (index_dir / conspec.FILENAME).write_text(json.dumps(data))
    return data


# ---------------------------------------------------------------------------
# MPN matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,match", [
    ("SN75LVDS83BDGG", "SN75LVDS83B", True),     # orderable vs base
    ("sn75lvds83b", "SN75LVDS83B", True),        # case/normalization
    ("M24C02-WMN6TP", "m24c02", True),           # dashes stripped, prefix
    ("TFP401APZP", "tfp401a", True),
    ("CH340E", "CH340E", True),                  # exact short match ok
    ("CH340E", "CH340G", False),                 # different suffix, no prefix rel.
    ("R", "RT9013", False),                      # too short for family prefix
    ("", "X", False),
])
def test_mpn_matches(a, b, match):
    assert conspec.mpn_matches(a, b) is match


# ---------------------------------------------------------------------------
# Store round-trip + lookup
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    d = tmp_path / ".datasheet-index" / "slug1"
    d.mkdir(parents=True)
    conspec.save(d, {"mpns": ["ABC1234"], "pins": []})
    loaded = conspec.load_dir(d)
    assert loaded["mpns"] == ["ABC1234"]
    assert loaded["schema"] == conspec.SCHEMA_VERSION


def test_wrong_schema_version_ignored(tmp_path):
    d = tmp_path / ".datasheet-index" / "slug1"
    _write(d, ["ABC1234"], schema=999)
    assert conspec.load_dir(d) is None


def test_load_for_mpn_across_dirs(tmp_path):
    board = tmp_path / "board"
    shared = tmp_path / "shared-index"
    _write(board / ".datasheet-index" / "a", ["SN75LVDS83B"])
    _write(shared / "b", ["TFP401A"])
    cfg = FakeCfg(board, extra=[shared])
    assert conspec.load_for_mpn(cfg, "SN75LVDS83BDGG")["mpns"] == ["SN75LVDS83B"]
    assert conspec.load_for_mpn(cfg, "TFP401APZP")["mpns"] == ["TFP401A"]
    assert conspec.load_for_mpn(cfg, "NOPE999") is None


def test_load_for_mpn_prefers_longest_match(tmp_path):
    board = tmp_path / "board"
    _write(board / ".datasheet-index" / "fam", ["RT9013"], package="family")
    _write(board / ".datasheet-index" / "exact", ["RT9013-33GB"], package="exact")
    cfg = FakeCfg(board)
    assert conspec.load_for_mpn(cfg, "RT9013-33GB")["package"] == "exact"


def test_pin_tables(tmp_path):
    board = tmp_path / "board"
    _write(board / ".datasheet-index" / "mcu", ["MCU123X"], pins=[
        {"number": "10", "name": "PA2", "functions": ["UART5_RX"], "type": "bidi"},
        {"number": "11", "name": "PA3", "functions": [], "type": "bidi"},
    ])
    _write(board / ".datasheet-index" / "nofn", ["EE99887"], pins=[
        {"number": "1", "name": "A0", "functions": [], "type": "input"},
    ])

    class C:  # minimal Component stand-in
        def __init__(self, mpn): self.mpn = mpn

    comps = {"U1": C("MCU123X"), "U2": C("EE99887"), "J1": C("")}
    tables = conspec.pin_tables(FakeCfg(board), comps)
    assert tables == {"U1": {"10": ["UART5_RX"], "11": []}}   # U2 has no functions at all


def test_gates_use_lookup_end_to_end(tmp_path):
    """cap-derating reads a v_rating from the store when the value string has none."""
    from kicad_bench.core import graph as graphmod
    from kicad_bench.quality import cap_derating

    board = tmp_path / "board"
    _write(board / ".datasheet-index" / "cap", ["GRM155R71C104KA88"],
           spec={"v_rating": 4.0, "dielectric": "X7R"})
    cfg = FakeCfg(board)

    g = graphmod.build_from(
        {"+5V": ["C1.1"], "GND": ["C1.2"]},
        {("C1", "1"): "+5V", ("C1", "2"): "GND"},
        [{"reference": "C1", "value": "100nF", "mpn": "GRM155R71C104KA88"}],
    )
    res = cap_derating.check(g, lookup=conspec.lookup(cfg))
    assert res.n_errors == 1   # 5V across an extracted 4V rating


# ---------------------------------------------------------------------------
# constraints.py helpers — the LCSC->MPN designator bridge
# ---------------------------------------------------------------------------

def test_lcsc_bridge_expands_slug_parts(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kicad_bench import constraints as cmod
    from kicad_bench import datasheet as dsmod
    from kicad_bench.core import graph as graphmod

    # BOM: designator C103 -> LCSC C16772; schematic: C103 -> MPN CL05B224KO5NNNC
    monkeypatch.setattr(dsmod, "_read_xlsx_rows", lambda p: [
        ["Comment", "Designator", "LCSC PN"],
        ["220nF", "C103, C104", "C16772"],
    ])
    g = graphmod.build_from(
        {"N1": ["C103.1"]}, {("C103", "1"): "N1"},
        [{"reference": "C103", "value": "220nF", "mpn": "CL05B224KO5NNNC"}])
    bom_file = tmp_path / "bom.xlsx"; bom_file.write_text("x")
    cfg = SimpleNamespace(bom=bom_file, root=tmp_path, root_sch=None, raw={})

    lm = cmod._lcsc_to_mpns(cfg, g)
    assert lm == {"C16772": {"CL05B224KO5NNNC"}}

    # parts_overview links the datasheet only to the LCSC id; the bridge adds the MPN
    monkeypatch.setattr(dsmod, "parts_overview", lambda c: {"parts": [
        {"part": "C16772", "lcsc": "C16772",
         "datasheet": {"slug": "c16772datasheet"}},
    ]})
    sp = cmod._slug_to_parts(cfg, lm)
    assert sp == {"c16772datasheet": ["C16772", "CL05B224KO5NNNC"]}
