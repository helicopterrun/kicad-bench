"""Tests for the datasheet-grounded deterministic gates (pure — synthetic graphs)."""
import pytest

from kicad_bench.core import graph as graphmod
from kicad_bench.quality import cap_derating, led_current, pin_mux


def make_graph(counts, components):
    """Build a graph from counts + component metadata; pin_net derived from counts."""
    pin_net = {}
    for net, refpins in counts.items():
        for rp in refpins:
            ref, _, pin = rp.partition(".")
            pin_net[(ref, pin)] = net
    return graphmod.build_from(counts, pin_net, components)


# ---------------------------------------------------------------------------
# cap-derating
# ---------------------------------------------------------------------------

def _cap_graph(value, rail="+5V"):
    return make_graph(
        {rail: ["C1.1"], "GND": ["C1.2"]},
        [{"reference": "C1", "value": value, "mpn": ""}],
    )


def test_cap_within_derating_passes():
    res = cap_derating.check(_cap_graph("100nF 16V X7R"))
    assert res.n_errors == 0
    assert not any(f.severity == "warn" for f in res.findings)


def test_cap_over_absolute_rating_errors():
    # 5V across a 4V part
    res = cap_derating.check(_cap_graph("10uF 4V"))
    assert res.n_errors == 1
    assert "over absolute rating" in res.findings[0].detail


def test_cap_derating_violation_warns_not_errors():
    # 5V across a 6.3V ceramic: limit = 6.3 * 0.8 = 5.04 -> pass;
    # tighten the factor to force the warn tier.
    res = cap_derating.check(_cap_graph("10uF 6.3V"), factors={"ceramic": 0.5})
    assert res.n_errors == 0
    warns = [f for f in res.findings if f.severity == "warn"]
    assert len(warns) == 1 and "derating" in warns[0].message


def test_cap_tantalum_factor():
    # 5V across a 6.3V tantalum: limit 3.15V -> warn under the default 0.5 factor
    g = make_graph({"+5V": ["C1.1"], "GND": ["C1.2"]},
                   [{"reference": "C1", "value": "100uF 6.3V tantalum", "mpn": ""}])
    res = cap_derating.check(g)
    assert any(f.severity == "warn" and "tantalum" in f.message for f in res.findings)


def test_cap_unknown_rating_skipped_info():
    res = cap_derating.check(_cap_graph("100nF"))
    assert res.n_errors == 0
    assert any("skipped" in f.message for f in res.findings if f.severity == "info")


def test_cap_between_two_rails_uses_delta():
    # cap between +12V and +5V sees 7V, not 12V
    g = make_graph({"+12V": ["C1.1"], "+5V": ["C1.2"]},
                   [{"reference": "C1", "value": "1uF 8V"}])
    res = cap_derating.check(g)   # 7V: > 8*0.8=6.4 -> warn, < 8 -> not error
    assert res.n_errors == 0
    assert any(f.severity == "warn" for f in res.findings)


def test_cap_constraints_lookup_preferred():
    g = _cap_graph("100nF")   # no rating in the value string
    lookup = {"GRM155R71C104KA88": {"spec": {"v_rating": 4.0, "dielectric": "X7R"}}}.get
    g.components["C1"].mpn = "GRM155R71C104KA88"
    res = cap_derating.check(g, lookup=lookup)
    assert res.n_errors == 1   # 5V across an extracted 4V rating


def test_cap_on_unknown_rail_is_skipped_not_evaluated_at_zero():
    """A cap between GND and an *unknown* rail must skip, not report 0 V across it.

    Regression: the single-known-net branch assumed the far side sits at 0 V, which
    inverts when the known side IS ground — a 10 uF/25 V part on an unnamed 18 V rail
    was silently passing as "0 V across a 25 V part".
    """
    g = make_graph(
        {"VGH": ["C1.1"], "GND": ["C1.2"]},          # VGH asserts no voltage
        [{"reference": "C1", "value": "1uF 50V X7R", "mpn": ""}],
    )
    op, source = cap_derating._operating_voltage(g, g.components["C1"])
    assert op is None and source == ""
    res = cap_derating.check(g)
    assert res.n_errors == 0
    assert "1 without a known net voltage" in " ".join(f.message for f in res.findings)


def test_cap_on_known_rail_still_assumes_zero_far_side():
    """The original reasoning is untouched when the *known* side is a real rail."""
    g = make_graph(
        {"+5V": ["C1.1"], "SOME_SIGNAL": ["C1.2"]},
        [{"reference": "C1", "value": "1uF 16V X7R", "mpn": ""}],
    )
    op, source = cap_derating._operating_voltage(g, g.components["C1"])
    assert op == 5.0 and source == "+5V"


# ---------------------------------------------------------------------------
# led-current
# ---------------------------------------------------------------------------

def _led_graph(r_value="220R", rail="+3V3", led_value="LED RED 2.0V 20mA",
               extra_counts=None):
    counts = {
        rail: ["R1.1"],
        "LED_A": ["R1.2", "D1.1"],
        "GND": ["D1.2"],
    }
    counts.update(extra_counts or {})
    return make_graph(counts, [
        {"reference": "R1", "value": r_value, "mpn": ""},
        {"reference": "D1", "value": led_value, "mpn": ""},
    ])


def test_led_within_rating_passes():
    # (3.3 - 2.0) / 220 = 5.9mA < 20mA
    res = led_current.check(_led_graph())
    assert res.n_errors == 0


def test_led_over_current_errors():
    # (5 - 2.0) / 47 = 63.8mA > 20mA
    res = led_current.check(_led_graph(r_value="47R", rail="+5V"))
    assert res.n_errors == 1
    assert "exceeds" in res.findings[0].message


def test_led_marginal_warns():
    # (3.3 - 2.0) / 68 = 19.1mA — inside 20mA but < 10% margin
    res = led_current.check(_led_graph(r_value="68R"))
    assert res.n_errors == 0
    assert any(f.severity == "warn" for f in res.findings)


def test_led_no_spec_skipped():
    res = led_current.check(_led_graph(led_value="LED"))
    assert res.n_errors == 0
    assert any("skipped" in f.message for f in res.findings if f.severity == "info")


def test_led_driver_ic_on_net_skipped():
    # a third component on the LED net makes the topology ambiguous
    res = led_current.check(_led_graph(extra_counts={"LED_A": ["R1.2", "D1.1", "U9.4"]}))
    assert res.n_errors == 0
    assert any("ambiguous" in f.message for f in res.findings if f.severity == "info")


def test_led_no_series_resistance_errors():
    g = make_graph({"+3V3": ["D1.1"], "GND": ["D1.2"]},
                   [{"reference": "D1", "value": "LED RED 2.0V 20mA"}])
    res = led_current.check(g)
    assert res.n_errors == 1
    assert "no series resistance" in res.findings[0].message


# ---------------------------------------------------------------------------
# pin-mux
# ---------------------------------------------------------------------------

def test_parse_net_token():
    assert pin_mux.parse_net_token("UART5_TX") == ("UART5", "TX")
    assert pin_mux.parse_net_token("MCU-UART5-TX") == ("UART5", "TX")
    assert pin_mux.parse_net_token("/I2C0.SCL") == ("I2C0", "SCL")
    assert pin_mux.parse_net_token("SPI1_SCLK") == ("SPI1", "SCK")   # alias
    assert pin_mux.parse_net_token("HDMI_D2+") is None
    assert pin_mux.parse_net_token("+3V3") is None


def _mux_graph():
    counts = {
        "UART5_TX": ["U1.10", "J1.1"],
        "I2C0_SDA": ["U1.11", "U2.3"],
    }
    return make_graph(counts, [
        {"reference": "U1", "value": "MCU", "mpn": "MCU1"},
        {"reference": "U2", "value": "EEPROM", "mpn": "EE1"},
        {"reference": "J1", "value": "Conn", "mpn": ""},
    ])


def test_pin_mux_infeasible_errors():
    tables = {"U1": {"10": ["UART5_RX", "GPIO"], "11": ["I2C0_SDA"]}}
    res = pin_mux.check(_mux_graph(), tables)
    assert res.n_errors == 1
    assert "UART5" in res.findings[0].message and "not TX" in res.findings[0].message


def test_pin_mux_feasible_passes():
    tables = {"U1": {"10": ["UART5_TX", "UART5_RX"], "11": ["I2C0_SDA"]}}
    res = pin_mux.check(_mux_graph(), tables)
    assert res.n_errors == 0


def test_pin_mux_direction_never_checked():
    # net asserts TX, pin exposes TX — even if a peer also exposes TX (a would-be
    # direction clash), feasibility passes: direction is the reviewer's call.
    tables = {
        "U1": {"10": ["UART5_TX"], "11": ["I2C0_SDA"]},
        "J1": {"1": ["UART5_TX"]},
    }
    res = pin_mux.check(_mux_graph(), tables)
    assert res.n_errors == 0


def test_pin_mux_peer_same_peripheral_skipped():
    # U1.10 can only do UART5_RX, but the peer ALSO exposes UART5 — inter-device
    # link, net-name perspective ambiguous -> skip, no error.
    tables = {
        "U1": {"10": ["UART5_RX"], "11": ["I2C0_SDA"]},
        "J1": {"1": ["UART5_TX", "UART5_RX"]},
    }
    res = pin_mux.check(_mux_graph(), tables)
    assert res.n_errors == 0


def test_pin_mux_no_tables_info_pass():
    res = pin_mux.check(_mux_graph(), {})
    assert res.n_errors == 0
    assert any("no pin tables" in f.message for f in res.findings)
