"""Tests for core/partspec.py — value-string spec parsing (pure)."""
import pytest

from kicad_bench.core import partspec


@pytest.mark.parametrize("value,expected", [
    ("100nF 16V X7R", {"farads": 100e-9, "v_rating": 16.0, "dielectric": "X7R"}),
    ("4.7uF", {"farads": 4.7e-6}),
    ("4u7", {"farads": 4.7e-6}),
    ("2n2 50V C0G", {"farads": 2.2e-9, "v_rating": 50.0, "dielectric": "C0G"}),
    ("10uF 6.3V", {"farads": 10e-6, "v_rating": 6.3}),
    ("1pF NP0", {"farads": 1e-12, "dielectric": "C0G"}),   # NP0 normalizes to C0G
])
def test_capacitor_values(value, expected):
    got = partspec.parse_value(value)
    for k, v in expected.items():
        assert got[k] == pytest.approx(v), (value, k)


@pytest.mark.parametrize("value,ohms", [
    ("10k", 10_000.0),
    ("4k7", 4_700.0),
    ("100R", 100.0),
    ("0R05", 0.05),
    ("2.2M", 2.2e6),
])
def test_resistor_values(value, ohms):
    assert partspec.parse_value(value)["ohms"] == pytest.approx(ohms)


def test_bare_number_needs_hint():
    # KiCad convention: an R symbol with Value "330" means 330 Ω — but only with the hint.
    assert "ohms" not in partspec.parse_value("330")
    assert partspec.parse_value("330", hint="R")["ohms"] == 330.0


def test_led_style_notes():
    got = partspec.parse_value("RED", notes="2.0V 20mA")
    assert got["v_rating"] == 2.0
    assert got["amps"] == pytest.approx(0.020)


def test_ua_current():
    assert partspec.parse_value("", notes="150uA")["amps"] == pytest.approx(150e-6)


def test_unknown_stays_absent():
    got = partspec.parse_value("DNP")
    assert got == {}


@pytest.mark.parametrize("dielectric,value,notes,expected", [
    ("X7R", "", "", "ceramic"),
    (None, "100uF", "tantalum bulk", "tantalum"),
    (None, "470uF", "aluminum electrolytic", "electrolytic"),
    (None, "100nF", "", "ceramic"),   # default
])
def test_dielectric_class(dielectric, value, notes, expected):
    assert partspec.dielectric_class(dielectric, value, notes) == expected
