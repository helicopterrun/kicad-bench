"""Tests for core/graph.py — net-role inference and graph assembly (pure)."""
import pytest
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


def test_vout_is_not_propagated_through_a_current_sense_fb():
    """The TPS61165 shape: an LED driver whose spec.vout (38 V) is a compliance
    maximum, not a rail, and whose FB is a current-sense node with ONE resistor to
    ground. Neither the output-pin rule (no VOUT pin) nor the fixed-output rule
    (a lone sense resistor is not "FB tied to the rail") may fire."""
    counts = {"LED_CATHODE": ["U6.1", "R80.1"], "GND": ["R80.2", "U6.2"]}
    pin_net = {("U6", "1"): "LED_CATHODE", ("R80", "1"): "LED_CATHODE",
               ("R80", "2"): "GND", ("U6", "2"): "GND"}
    comps = [{"reference": "U6", "value": "TPS61165", "mpn": "TPS61165DBVR"},
             {"reference": "R80", "value": "10", "mpn": ""}]
    g = graph.build_from(counts, pin_net, comps)
    lookup = {"TPS61165DBVR": {"spec": {"vout": 38},
                               "pins": [{"number": "1", "name": "FB"},
                                        {"number": "2", "name": "GND"}]}}.get
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["LED_CATHODE"].voltage is None


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


# ---------------------------------------------------------------------------
# feedback-divider solving
# ---------------------------------------------------------------------------

def _fb_graph(rtop="1M", rbot="130k", vref=1.146, fb_pin="FB",
              bot_net="GND", mpn="TPS65150PWPR"):
    """A boost regulator sensing its output rail through an Rtop/Rbot divider.

    Modelled on the TPS65150 AVDD loop of a real board: 1.00 MOhm / 130 kOhm off a
    1.146 V reference, which the design docs work out to 9.96 V.
    """
    counts = {
        "AVDD": ["U5.9", "R13.1"],
        "FB": ["U5.1", "R13.2", "R19.2"],
        bot_net: ["R19.1", "U5.19"],
    }
    pin_net = {}
    for net, refpins in counts.items():
        for rp in refpins:
            ref, _, pin = rp.partition(".")
            pin_net[(ref, pin)] = net
    comps = [{"reference": "U5", "value": "TPS65150", "mpn": mpn},
             {"reference": "R13", "value": rtop, "mpn": ""},
             {"reference": "R19", "value": rbot, "mpn": ""}]
    g = graph.build_from(counts, pin_net, comps)
    cons = {"spec": {"vout": 15, "vref": vref},
            "pins": [{"number": "1", "name": fb_pin},
                     {"number": "9", "name": "SUP"},
                     {"number": "19", "name": "GND"}]}
    return g, {mpn: cons}.get


def test_divider_solves_a_positive_rail():
    g, lookup = _fb_graph()
    assert g.nets["AVDD"].voltage is None          # 'AVDD' asserts no number
    assert graph.solve_rail_voltages(g, lookup) == 1
    assert g.nets["AVDD"].voltage == pytest.approx(9.96, abs=0.02)
    assert g.nets["AVDD"].voltage_source == "divider"


def test_divider_without_a_vref_stays_unknown():
    """Skip, never guess — the doc's rule and the reason we don't sweep common Vrefs."""
    g, lookup = _fb_graph(vref=None, mpn="NOSUCHPART123")
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["AVDD"].voltage is None


def test_divider_falls_back_to_the_family_table():
    """No extracted vref, but the MPN prefix is a known family (AP632x -> 0.8 V)."""
    g, lookup = _fb_graph(rtop="10k", rbot="2k", vref=None, mpn="AP63200")
    assert graph.solve_rail_voltages(g, lookup) == 1
    assert g.nets["AVDD"].voltage == pytest.approx(0.8 * (1 + 10000 / 2000), abs=0.01)


import pytest as _pytest


@_pytest.mark.parametrize("pin", ["FBN", "FBP"])
def test_secondary_feedback_loops_are_recognised_but_not_solved(pin):
    """A multi-output part's other loops have their OWN references, which the single
    scalar spec.vref cannot carry. Measured on a real TPS65150: FB 1.146 V, FBP
    1.214 V, FBN 1.213 V — reusing one across all three put a rail 5.6% out."""
    g, lookup = _fb_graph(fb_pin=pin)
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["AVDD"].voltage is None


def test_divider_bottom_leg_must_reach_ground():
    """A bottom leg landing on a live node is a bias network, not a rail divider."""
    g, lookup = _fb_graph(bot_net="VCOM_WIPER")
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["AVDD"].voltage is None


def test_three_resistors_on_the_fb_node_is_ambiguous():
    g, lookup = _fb_graph()
    g.components["R99"] = graph.Component(ref="R99", value="10k",
                                          pins={"1": "FB", "2": "GND"})
    g.nets["FB"].pins.append(("R99", "1"))
    assert graph.solve_rail_voltages(g, lookup) == 0


def test_unparsable_resistor_value_skips():
    g, lookup = _fb_graph(rtop="DNP")
    assert graph.solve_rail_voltages(g, lookup) == 0


def test_fb_tied_straight_to_the_rail_is_a_fixed_output_part():
    """The AP63203 case: no divider, FB on the output rail, voltage from the datasheet."""
    counts = {"BUCK_OUT": ["U4.1", "L3.2"], "GND": ["U4.4"]}
    pin_net = {("U4", "1"): "BUCK_OUT", ("L3", "2"): "BUCK_OUT", ("U4", "4"): "GND"}
    comps = [{"reference": "U4", "value": "AP63203", "mpn": "AP63203"},
             {"reference": "L3", "value": "4.7uH", "mpn": ""}]
    g = graph.build_from(counts, pin_net, comps)
    lookup = {"AP63203": {"spec": {"vout": 3.3},
                          "pins": [{"number": "1", "name": "FB"},
                                   {"number": "4", "name": "GND"}]}}.get
    assert graph.solve_rail_voltages(g, lookup) == 1
    assert g.nets["BUCK_OUT"].voltage == 3.3
    assert g.nets["BUCK_OUT"].voltage_source == "vout"


def test_absurd_solved_voltage_is_rejected():
    """A 1000:1 divider off a 1.146 V ref implies ~1.1 kV — that's a parse error, not a rail."""
    g, lookup = _fb_graph(rtop="1M", rbot="1k")
    assert graph.solve_rail_voltages(g, lookup) == 0
    assert g.nets["AVDD"].voltage is None
