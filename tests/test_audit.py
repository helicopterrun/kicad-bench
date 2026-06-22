"""Pure-logic tests for the consolidated audit + the netclass full-path fix."""
from kicad_bench import audit
from kicad_bench.core.report import Result
from kicad_bench.layout import route_coverage, netclass_coverage as ncc


# -- DRC summary collapses many items into one line per type -----------------
def test_drc_summary_groups_by_type():
    drc = {
        "violations": [
            {"type": "shorting_items", "severity": "error", "description": "x"},
            {"type": "shorting_items", "severity": "error", "description": "y"},
            {"type": "silk_overlap", "severity": "warning", "description": "z"},
        ],
        "unconnected_items": [{"items": []}, {"items": []}],
    }
    res = audit._drc_summary(drc)
    assert any(f.severity == "error" and "shorting_items: 2" in f.message for f in res.findings)
    assert any(f.severity == "warn" and "silk_overlap: 1" in f.message for f in res.findings)
    assert res.n_errors == 1                       # one error-type line (for 2 violations)
    assert "2 error(s)" in res.summary and "2 unrouted" in res.summary


def test_ratsnest_from_drc_shared_path():
    drc = {"unconnected_items": [
        {"items": [{"description": "Pad 1 [/VIN_3V3] of U2 on F.Cu"},
                   {"description": "Pad 2 [/VIN_3V3] of U3 on F.Cu"}]},
        {"items": [{"description": "Pad 1 [GND] of C1 on F.Cu"},
                   {"description": "Pad 2 [GND] of C2 on F.Cu"}]},
    ]}
    c = route_coverage.ratsnest_from_drc(drc)
    assert c["/VIN_3V3"] == 1 and c["GND"] == 1


# -- consolidated render -----------------------------------------------------
def test_render_pass_and_fail():
    ok = Result("Check A"); ok.ok("fine")
    bad = Result("Check B"); bad.error("boom", where="U5")
    out = audit.render([("Schematic", ok), ("Schematic", bad)], full=False)
    assert "FAIL" in out and "boom" in out and "U5" in out and "1 check(s) with errors" in out

    clean = audit.render([("Schematic", ok)], full=False)
    assert "PASS" in clean and "0 check(s) with errors" in clean


# -- the netclass leaf-vs-fullpath fix (why audit resolves on full names) -----
def test_resolve_classes_fullpath_not_leaf():
    s = {"netclass_patterns": [{"pattern": "/VGH", "netclass": "BIAS"}]}
    assert ncc.resolve_classes("/VGH", s) == {"BIAS"}     # KiCad matches the full net name
    assert ncc.resolve_classes("VGH", s) == set()         # the leaf would MISS — the old bug


def test_pcbgeom_leaf_helper():
    assert ncc.pcbgeom_leaf("/video/TMDS_CLK_P") == "TMDS_CLK_P"
    assert ncc.pcbgeom_leaf("GND") == "GND"


# -- sidecar JSON serializer -------------------------------------------------
def test_sections_to_dict_shape():
    from pathlib import Path
    from kicad_bench.core import config as cfgmod
    ok = Result("A"); ok.ok("fine")
    bad = Result("B"); bad.error("boom", where="U5"); bad.warn("meh"); bad.info("fyi")
    cfg = cfgmod.Config(path=Path("x"), root=Path("."), pcb=None)
    d = audit.sections_to_dict([("Sec", ok), ("Sec", bad)], cfg)
    assert d["verdict"] == {"err_checks": 1, "warn_checks": 0, "clean": 1, "total": 2}
    checks = d["sections"][0]["checks"]
    assert d["sections"][0]["section"] == "Sec" and len(checks) == 2
    b = next(c for c in checks if c["title"] == "B")
    assert b["glyph"] == "error" and b["n_errors"] == 1
    assert any(f["severity"] == "error" and f["where"] == "U5" for f in b["findings"])
    # info findings are carried (sidecar can filter them client-side)
    assert any(f["severity"] == "info" for f in b["findings"])
