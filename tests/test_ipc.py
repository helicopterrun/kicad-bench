"""Tests for core/ipc.py — closed-form IPC transmission-line and current math (pure)."""
import pytest

from kicad_bench.core import ipc

# JLC04161H-7628, the stackup LVDS Driver V1 is built on, read from its own .kicad_pcb:
# F.Cu 0.035 mm over 0.0918 mm prepreg at epsilon_r 4.6.
JLC = dict(h=0.0918, t=0.035, er=4.6)


def test_lvds_pair_matches_fab_target():
    """0.1128 mm / 0.2032 mm gap on JLC04161H is sold as a 100 ohm pair."""
    z0 = ipc.microstrip_z0(0.1128, JLC["h"], JLC["t"], JLC["er"])
    zdiff = ipc.differential_z(z0, 0.2032, JLC["h"])
    assert 95.0 < zdiff < 103.0
    assert zdiff == pytest.approx(98.9, abs=0.5)


def test_usb_pair_matches_fab_target():
    """The wider 0.1443 mm trace is the 90 ohm USB geometry."""
    z0 = ipc.microstrip_z0(0.1443, JLC["h"], JLC["t"], JLC["er"])
    zdiff = ipc.differential_z(z0, 0.2032, JLC["h"])
    assert zdiff == pytest.approx(86.6, abs=0.5)


def test_ipc2221_one_amp_reference_point():
    """1 A, 1 oz, 10 C rise, external ~= 11.8 mil is the textbook anchor."""
    mm = ipc.width_for_current(1.0, copper_oz=1.0, temp_rise_c=10.0)
    assert mm / 0.0254 == pytest.approx(11.8, abs=0.5)


def test_ipc2221_internal_needs_more_copper():
    """Inner layers only conduct, so k halves and the trace must be wider."""
    assert (ipc.width_for_current(2.0, internal=True)
            > ipc.width_for_current(2.0, internal=False))


def test_ipc2221_monotonic_in_current_and_thickness():
    assert ipc.width_for_current(3.0) > ipc.width_for_current(1.0)
    assert ipc.width_for_current(1.0, copper_oz=2.0) < ipc.width_for_current(1.0)


def test_wider_trace_lowers_impedance():
    """Property check — catches a sign or inversion error the point tests would pass."""
    narrow = ipc.microstrip_z0(0.10, JLC["h"], JLC["t"], JLC["er"])
    wide = ipc.microstrip_z0(0.30, JLC["h"], JLC["t"], JLC["er"])
    assert wide < narrow


def test_higher_er_lowers_impedance():
    assert (ipc.microstrip_z0(0.1128, JLC["h"], JLC["t"], 4.6)
            < ipc.microstrip_z0(0.1128, JLC["h"], JLC["t"], 3.0))


def test_uncoupled_pair_approaches_twice_z0():
    """As the gap opens the coupling term decays; Zdiff -> 2*Z0."""
    z0 = ipc.microstrip_z0(0.1128, JLC["h"], JLC["t"], JLC["er"])
    assert ipc.differential_z(z0, 50.0, JLC["h"]) == pytest.approx(2 * z0, rel=1e-6)
    # ...and a tight pair couples down well below that.
    assert ipc.differential_z(z0, 0.05, JLC["h"]) < 2 * z0 * 0.75


def test_stripline_is_lower_impedance_than_microstrip():
    """Fully embedded in dielectric, with two reference planes."""
    assert (ipc.stripline_z0(0.1128, 0.5, JLC["t"], JLC["er"])
            < ipc.microstrip_z0(0.1128, 0.5, JLC["t"], JLC["er"]))


@pytest.mark.parametrize("call", [
    lambda: ipc.microstrip_z0(0, 0.09, 0.035, 4.6),
    lambda: ipc.microstrip_z0(0.1, -0.09, 0.035, 4.6),
    lambda: ipc.stripline_z0(0.1, 0.5, 0.035, 0),
    lambda: ipc.differential_z(50.0, 0, 0.09),
    lambda: ipc.width_for_current(0),
    lambda: ipc.width_for_current(1.0, temp_rise_c=0),
])
def test_non_physical_geometry_raises(call):
    with pytest.raises(ValueError):
        call()


def test_unknown_geometry_name_raises():
    with pytest.raises(ValueError):
        ipc.differential_z(50.0, 0.2, 0.09, geometry="coplanar")
