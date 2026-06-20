"""Quality-tool logic tests with synthetic ERC reports / netlists (no kicad-cli)."""
from pathlib import Path

from kicad_bench.core.config import AllowRule
from kicad_bench.quality import erc_triage, netlist_audit


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
