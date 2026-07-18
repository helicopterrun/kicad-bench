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
