"""Quality-tool logic tests with synthetic ERC reports / netlists (no kicad-cli)."""
from pathlib import Path

from kicad_bench.core.config import AllowRule
from kicad_bench.quality import erc_triage, netlist_audit
from kicad_bench.layout import netclass_coverage as ncc


SCH = Path("power_bias.kicad_sch")

# A realistic severity-error ERC report fragment.
REPORT = """ERC report

[power_pin_not_driven]: Input Power pin not driven by any Output Power pins
    @(100.00 mm, 50.00 mm): Symbol U5 Pin 7
[power_pin_not_driven]: Input Power pin not driven by any Output Power pins
    @(120.00 mm, 50.00 mm): Symbol U4 Pin 3
[pin_not_connected]: Hierarchical label 'VGH' in root sheet cannot be connected to non-existent parent sheet
    @(342.90 mm, 48.26 mm): Hierarchical label
[label_dangling]: Label not connected to anything
    @(200.00 mm, 30.00 mm): Label FOO
"""


def test_triage_classifies():
    # U5.7 -> allowed via config rule; U4.3 -> driven by parent (hier input);
    # VGH hier pin -> allowed partial-build; FOO dangling -> REAL.
    pin_net = {("U5", "7"): "GND", ("U4", "3"): "VIN_12V"}
    in_nets = {"VIN_12V"}                      # U4.3's net is a hier input
    allow = [AllowRule(reason="GND offset", type="power_pin_not_driven", ref="U5", pin="7")]
    res = erc_triage.triage(SCH, pin_net, in_nets, allow, report=REPORT)

    sev = {f.message.split("]")[0].lstrip("["): f.severity for f in res.findings}
    # exactly one real error (the dangling label)
    assert res.n_errors == 1
    assert any(f.severity == "error" and "label_dangling" in f.message for f in res.findings)
    # the U5.7 finding is allowed (via the config rule)
    u5 = next(f for f in res.findings if f.where == "U5.7")
    assert u5.severity == "allowed" and "GND offset" in u5.detail
    # the U4.3 finding is allowed (driven by parent)
    u4 = next(f for f in res.findings if f.where == "U4.3")
    assert u4.severity == "allowed" and "hier input" in u4.detail


def test_triage_unmapped_type_is_real():
    # A violation type with no triage logic must be REAL and tagged, never silent.
    report = ("[lib_symbol_mismatch]: Symbol doesn't match library\n"
              "    @(10 mm, 10 mm): Symbol U1\n")
    res = erc_triage.triage(SCH, {}, set(), [], report=report)
    assert res.n_errors == 1
    f = res.findings[0]
    assert f.severity == "error" and "unmapped ERC type" in f.detail
    assert "unmapped type" in res.summary


def test_triage_clean_when_only_allowed():
    pin_net = {("U5", "7"): "GND"}
    allow = [AllowRule(reason="x", type="power_pin_not_driven", ref="U5", pin="7")]
    report = ("[power_pin_not_driven]: Input Power pin not driven by any Output Power pins\n"
              "    @(1 mm, 1 mm): Symbol U5 Pin 7\n")
    res = erc_triage.triage(SCH, pin_net, set(), allow, report=report)
    assert res.passed and res.n_errors == 0


def test_netlist_audit(monkeypatch):
    counts = {"GND": ["a.1", "b.2", "c.3"], "VCC": ["d.1"], "VGL": []}
    monkeypatch.setattr("kicad_bench.core.cli.netlist_nets", lambda s: (counts, {}))
    # GND ok, VCC wrong count, VGL expected-but-empty, MISSING absent entirely
    res = netlist_audit.audit(SCH, {"GND": 3, "VCC": 2, "VGL": 1, "MISSING": 2})
    assert res.n_errors == 3                  # VCC, VGL(0!=1), MISSING
    assert not res.passed
    missing = next(f for f in res.findings if f.where == "MISSING")
    assert "MISSING from netlist" in missing.message


def test_audit_live_presence_and_diff():
    # live netlist (as kb-ipc sch --nets emits): named vs auto vs unconnected.
    live = [
        {"name": "/VBUS", "kind": "named", "members": 10},
        {"name": "GND", "kind": "named", "members": 12},
        {"name": "/TXD", "kind": "named", "members": 2},
        {"name": "Net-(D1-A)", "kind": "auto", "members": 2},
        {"name": "unconnected-(U1-NC-Pad10)", "kind": "unconnected", "members": 1},
    ]
    # expected net RXD is absent from live -> one error; auto/unconnected ignored.
    file_counts = {"VBUS": ["x.1"], "GND": ["y.1"], "STALE_FILE_NET": ["z.1"]}  # TXD only live
    res = netlist_audit.audit_live(live, {"VBUS": 10, "RXD": 2}, file_counts)
    assert res.n_errors == 1                                   # RXD missing live
    assert next(f for f in res.findings if f.where == "RXD").severity == "error"
    warns = {f.where for f in res.findings if f.severity == "warn"}
    assert "TXD" in warns          # live-only (in editor, not saved file)
    assert "STALE_FILE_NET" in warns  # file-only (gone from live editor)


# -- netclass resolution (KiCad net_settings semantics) ------------------
# KiCad 10: a net is assigned to EVERY matching pattern's class (not last-wins),
# patterns match as glob OR regex, and netclass_assignments map net -> [classes].
def test_resolve_classes_default_when_unmatched():
    assert ncc.resolve_classes("ANYTHING", {}) == set()        # only implicit Default


def test_resolve_classes_pattern_glob():
    s = {"netclass_patterns": [{"pattern": "VCC*", "netclass": "Power"}]}
    assert ncc.resolve_classes("VCC3V3", s) == {"Power"}
    assert ncc.resolve_classes("GND", s) == set()


def test_resolve_classes_explicit_assignment_is_a_list():
    # netclass_assignments maps a full net name -> LIST of class strings.
    s = {"netclass_assignments": {"SCL": ["I2C"], "SDA": ["I2C", "Sense"]}}
    assert ncc.resolve_classes("SCL", s) == {"I2C"}
    assert ncc.resolve_classes("SDA", s) == {"I2C", "Sense"}
    assert ncc.resolve_classes("OTHER", s) == set()


def test_pattern_regex_zero_or_more_footgun():
    # Faithful to KiCad: a pattern is glob OR regex, so 'X*' (regex: zero-or-more X)
    # matches every net. Documented KiCad behaviour, reproduced deliberately.
    s = {"netclass_patterns": [{"pattern": "X*", "netclass": "Power"}]}
    assert ncc.resolve_classes("SCL", s) == {"Power"}     # matches via regex empty-match


def test_resolve_classes_all_matching_apply():
    # A net matching two patterns gets BOTH classes (KiCad 9/10 behaviour).
    s = {"netclass_patterns": [
        {"pattern": "USB_*", "netclass": "USBpwr"},
        {"pattern": "USB_D*", "netclass": "USBdiff"},
    ]}
    assert ncc.resolve_classes("USB_DP", s) == {"USBpwr", "USBdiff"}


# -- dru-lint ------------------------------------------------------------
from kicad_bench.layout import dru_lint


def test_dru_lint_clean(tmp_path):
    p = tmp_path / "ok.kicad_dru"
    p.write_text('(version 1)\n(rule "r" (constraint track_width (min 0.2mm)) '
                 '(condition "A.NetName == \'X\'"))\n')
    res = dru_lint.lint(p)
    assert res.passed and res.n_errors == 0


def test_dru_lint_catches_errors(tmp_path):
    p = tmp_path / "bad.kicad_dru"
    # bad version (warn), unknown constraint (err), unknown severity (err), unbalanced (err)
    p.write_text('(version 2)\n(rule "r" (severity bogus) '
                 '(constraint not_real (min 1mm))\n')
    res = dru_lint.lint(p)
    assert not res.passed
    msgs = " ".join(f.message for f in res.findings)
    assert "not_real" in msgs and "bogus" in msgs and "unbalanced" in msgs


def test_dru_lint_netclass_advisory(tmp_path):
    p = tmp_path / "nc.kicad_dru"
    p.write_text('(version 1)\n(rule "r" (constraint clearance (min 1mm)) '
                 '(condition "A.NetClass == \'Power\'"))\n')
    res = dru_lint.lint(p)
    assert res.passed  # advisory is a warning, not an error
    assert any("hasNetclass" in f.message for f in res.findings)


# -- netclass-sync generator ---------------------------------------------
from kicad_bench.layout import netclass_sync


def test_netclass_sync_build():
    fab = {"domains": {"PWR": {"track_width": 0.5, "via_diameter": 0.6,
                              "via_drill": 0.3, "internal_clearance": 0.2}},
           "nets": {"PWR": ["VIN", "V5"]},
           "diff_pairs": {"LVDS": {"track_width": 0.11, "gap": 0.2, "members": ["LVDS_CLK"]}}}
    present = {"VIN", "LVDS_CLK_P", "LVDS_CLK_N"}        # V5 planned but absent
    classes, assignments, planned, missing = netclass_sync.build_settings(fab, present)
    assert {c["name"] for c in classes} == {"PWR", "LVDS"}
    assert assignments["VIN"] == ["PWR"]
    assert assignments["LVDS_CLK_P"] == ["LVDS"] and assignments["LVDS_CLK_N"] == ["LVDS"]
    assert "V5" in missing and "VIN" not in missing
    pwr = next(c for c in classes if c["name"] == "PWR")
    assert pwr["track_width"] == 0.5 and pwr["clearance"] == 0.2 and pwr["via_drill"] == 0.3
    lvds = next(c for c in classes if c["name"] == "LVDS")
    assert lvds["diff_pair_gap"] == 0.2 and lvds["diff_pair_width"] == 0.11
