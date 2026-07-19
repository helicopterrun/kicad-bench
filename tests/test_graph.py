"""Tests for core/graph.py — net-role inference and graph assembly (pure)."""
from kicad_bench.core import graph


# ---------------------------------------------------------------------------
# infer_net_role
# ---------------------------------------------------------------------------

def test_infer_net_role_table():
    cases = {
        # ground family
        "GND": ("gnd", 0.0),
        "AGND": ("gnd", 0.0),
        "VSS": ("gnd", 0.0),
        "PWR_GND": ("gnd", 0.0),
        "/sheet1/DGND": ("gnd", 0.0),
        # signed rails
        "+3V3": ("power", 3.3),
        "+5V": ("power", 5.0),
        "+1V35": ("power", 1.35),
        "-12V": ("power", -12.0),
        "+VUSB": ("signal", None),   # no parsable voltage, no VCC-family prefix
        # named rails
        "VCC": ("power", None),
        "VCC_1V8": ("power", 1.8),
        "VDD3V3": ("power", 3.3),
        "VBUS_5V0": ("power", 5.0),
        "VBAT": ("power", None),
        "VREF_2.5V": ("power", 2.5),
        # trailing voltage token asserts a rail without a power prefix
        "HDMI_5V": ("power", 5.0),
        "TOUCH_VDD_2V8": ("power", 2.8),
        "/video/HDMI_5V": ("power", 5.0),      # judged on the leaf
        "PANEL_3.3V": ("power", 3.3),
        # ...but only anchored at the end, after a '_'
        "LVDS_D3V_P": ("signal", None),
        "TMDS_5V_CLK": ("signal", None),
        # plain signals
        "SPI_MISO": ("signal", None),
        "UART5_TX": ("signal", None),
        "/HFXIN": ("signal", None),
        "LED_DRIVE": ("signal", None),
        # V-containing signal names must not read as rails
        "VIDEO_SYNC": ("signal", None),
    }
    for name, expected in cases.items():
        assert graph.infer_net_role(name) == expected, name


def test_parse_rail_voltage_shorthand_not_greedy():
    # "1V35" is 1.35, not 1.3 + stray 5
    assert graph.infer_net_role("+1V35") == ("power", 1.35)
    # trailing V-digit runs like "3V3V" should not misparse
    kind, v = graph.infer_net_role("VCC_3V3_SW")
    assert (kind, v) == ("power", 3.3)


# ---------------------------------------------------------------------------
# build_from
# ---------------------------------------------------------------------------

COUNTS = {
    "+3V3": ["C1.1", "U1.5", "U2.6"],
    "GND": ["C1.2", "U1.2", "U2.3"],
    "UART_TX": ["U1.7", "U2.10"],
}
PIN_NET = {
    ("C1", "1"): "+3V3", ("C1", "2"): "GND",
    ("U1", "5"): "+3V3", ("U1", "2"): "GND", ("U1", "7"): "UART_TX",
    ("U2", "6"): "+3V3", ("U2", "3"): "GND", ("U2", "10"): "UART_TX",
}
COMPONENTS = [
    {"reference": "C1", "value": "100nF 16V X7R", "mpn": ""},
    {"reference": "U1", "value": "CH340E", "mpn": "CH340E"},
    # U2 intentionally missing from the structural list (netlist-only ref)
]


def _graph():
    return graph.build_from(COUNTS, PIN_NET, COMPONENTS)


def test_build_from_components_and_pins():
    g = _graph()
    assert g.components["C1"].value == "100nF 16V X7R"
    assert g.components["C1"].pins == {"1": "+3V3", "2": "GND"}
    assert g.components["U1"].mpn == "CH340E"
    # netlist-only ref still present with connectivity
    assert g.components["U2"].pins["10"] == "UART_TX"
    assert g.components["U2"].value == ""


def test_build_from_nets():
    g = _graph()
    assert g.nets["+3V3"].kind == "power"
    assert g.nets["+3V3"].voltage == 3.3
    assert g.nets["GND"].kind == "gnd"
    assert g.nets["GND"].voltage == 0.0
    assert g.nets["UART_TX"].kind == "signal"
    assert ("U2", "10") in g.nets["UART_TX"].pins


def test_queries():
    g = _graph()
    assert g.net_of("U1", "7") == "UART_TX"
    assert g.net_of("U1", "99") is None
    assert g.net_of("ZZ", "1") is None
    assert g.rail_voltage("+3V3") == 3.3
    assert g.rail_voltage("UART_TX") is None
    n = g.neighbors("C1")
    assert ("U1", "5") in n["+3V3"] and ("U2", "6") in n["+3V3"]
    assert ("C1", "1") not in n["+3V3"]   # self excluded


# ---------------------------------------------------------------------------
# solve_rail_voltages — topology-solved rails
# ---------------------------------------------------------------------------

def _reg_graph(out_pin_name="VOUT", out_net="TOUCH_SUPPLY"):
    """An LDO (U10) feeding `out_net`, plus a decoupling cap on that rail.

    The default net name deliberately asserts NO voltage, so these tests isolate the
    datasheet path from the name-inference path.
    """
    counts = {
        "+5V": ["U10.1"],
        "GND": ["U10.2", "C9.2"],
        out_net: ["U10.5", "C9.1"],
    }
    pin_net = {}
    for net, refpins in counts.items():
        for rp in refpins:
            ref, _, pin = rp.partition(".")
            pin_net[(ref, pin)] = net
    comps = [{"reference": "U10", "value": "AP2112K-3.3", "mpn": "AP2112K-3.3TRG1"},
             {"reference": "C9", "value": "1uF 16V X7R", "mpn": ""}]
    g = graph.build_from(counts, pin_net, comps)
    cons = {"spec": {"vout": 3.3},
            "pins": [{"number": "1", "name": "VIN"},
                     {"number": "2", "name": "GND"},
                     {"number": "5", "name": out_pin_name}]}
    return g, {"AP2112K-3.3TRG1": cons}.get


def test_solve_rail_voltages_from_regulator_output_pin():
    g, lookup = _reg_graph()
    # The name says nothing (leaf starts with TOUCH, not a power prefix).
    assert g.nets["TOUCH_SUPPLY"].voltage is None
    assert g.nets["TOUCH_SUPPLY"].kind == "signal"

    assert graph.solve_rail_voltages(g, lookup) == 1
    assert g.nets["TOUCH_SUPPLY"].voltage == 3.3
    assert g.nets["TOUCH_SUPPLY"].voltage_source == "vout"
    assert g.nets["TOUCH_SUPPLY"].kind == "power"   # a regulated output is a rail


def test_solve_rail_voltages_needs_an_output_named_pin():
    # The TPS65150 shape: spec.vout is an abs-max headroom figure and the part
    # senses through FB, so there is no output pin to propagate through. Skip.
    g, lookup = _reg_graph(out_pin_name="FB")
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["TOUCH_SUPPLY"].voltage is None
    assert g.nets["TOUCH_SUPPLY"].voltage_source is None


def test_solve_rail_voltages_never_overwrites_a_named_rail():
    g, lookup = _reg_graph(out_net="+1V8")     # name says 1.8 V, datasheet says 3.3
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["+1V8"].voltage == 1.8
    assert g.nets["+1V8"].voltage_source == "netname"


def test_solve_rail_voltages_skips_ambiguous_multi_output():
    g, lookup = _reg_graph()
    # A second VOUT-named pin landing on a different net: which rail is `vout`?
    g.components["U10"].pins["6"] = "+5V"
    cons = lookup("AP2112K-3.3TRG1")
    cons["pins"].append({"number": "6", "name": "VOUT"})
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["TOUCH_SUPPLY"].voltage is None


def test_solve_rail_voltages_without_lookup_is_a_noop():
    g, _ = _reg_graph()
    assert graph.solve_rail_voltages(g, None) == 0


def test_build_from_records_netname_provenance():
    g = _graph()
    assert g.nets["+3V3"].voltage_source == "netname"
    assert g.nets["GND"].voltage_source == "netname"
    assert g.nets["UART_TX"].voltage_source is None
