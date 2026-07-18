"""graph.py — queryable bipartite design graph (components ↔ nets).

Built once per audit from the shared netlist export (`cli.netlist_nets`) joined to the
structural component list (`schparse.components`). This is the substrate the
datasheet-grounded checks (cap-derating, led-current, pin-mux) and the `kb review`
engine query, instead of each re-deriving connectivity from raw exports.

Net *roles* (ground / power / signal) and nominal rail voltages are inferred from net
names only — `+3V3`, `VCC_1V8`, `GND` — the same convention the house templates use for
power nets. Inference is deliberately conservative: a name that doesn't clearly assert
a rail stays a `signal` with no voltage, and downstream checks must treat a missing
voltage as "unknown", never as 0.

Read-only; plain stdlib dataclasses (kicad-bench core stays dependency-light).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Net-role inference
# ---------------------------------------------------------------------------

_GROUND_NAMES = {
    "GND", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "GNDPWR", "GNDREF",
    "VSS", "AVSS", "DVSS", "PVSS", "EARTH", "CHASSIS", "0V",
}
_POWER_PREFIXES = (
    "VCC", "VDD", "VBUS", "VBAT", "VBATT", "VIN", "VOUT", "VSYS", "VREG",
    "VREF", "PWR_", "V+", "AVDD", "DVDD", "PVDD",
)

# "3V3" / "1V35" digit-V-digit shorthand; "-5V" / "12V" / "3.3V" plain volts.
_SHORTHAND_V = re.compile(r"(?<![\d.])(-?)(\d+)V(\d+)(?![\dV])")
_PLAIN_V = re.compile(r"(?<![\d.])(-?)(\d+(?:\.\d+)?)\s*V(?![\dA-Za-z])")


def _parse_rail_voltage(name: str) -> float | None:
    """Extract a nominal voltage from a rail name, or None.

    Handles the two house spellings: shorthand ``3V3``/``1V35`` (decimal point
    replaced by V) and plain ``5V``/``3.3V``/``-12V``.
    """
    m = _SHORTHAND_V.search(name)
    if m:
        sign, whole, frac = m.groups()
        return float(f"{sign}{whole}.{frac}")
    m = _PLAIN_V.search(name)
    if m:
        sign, num = m.groups()
        return float(f"{sign}{num}")
    return None


def infer_net_role(name: str) -> tuple[str, float | None]:
    """Classify a net name -> ("gnd" | "power" | "signal", nominal volts | None)."""
    upper = name.lstrip("/").upper()
    # Hierarchical paths: judge the leaf name.
    upper = upper.rsplit("/", 1)[-1]

    if upper in _GROUND_NAMES or upper.endswith("_GND"):
        return "gnd", 0.0

    # "+3V3", "+5V", "-12V" — an explicit rail sign always means power.
    if upper.startswith(("+", "-")):
        v = _parse_rail_voltage(upper)
        if v is not None:
            return "power", v

    if upper.startswith(_POWER_PREFIXES):
        return "power", _parse_rail_voltage(upper)

    return "signal", None


# ---------------------------------------------------------------------------
# Graph model
# ---------------------------------------------------------------------------

@dataclass
class Component:
    ref: str
    value: str = ""
    mpn: str = ""
    pins: dict[str, str] = field(default_factory=dict)   # pin number -> net name


@dataclass
class Net:
    name: str
    kind: str = "signal"                                  # "gnd" | "power" | "signal"
    voltage: float | None = None                          # nominal volts (gnd -> 0.0)
    pins: list[tuple[str, str]] = field(default_factory=list)  # [(ref, pin), ...]


@dataclass
class DesignGraph:
    components: dict[str, Component] = field(default_factory=dict)
    nets: dict[str, Net] = field(default_factory=dict)

    def net_of(self, ref: str, pin: str) -> str | None:
        c = self.components.get(ref)
        return c.pins.get(pin) if c else None

    def rail_voltage(self, net_name: str) -> float | None:
        net = self.nets.get(net_name)
        return net.voltage if net else None

    def neighbors(self, ref: str) -> dict[str, list[tuple[str, str]]]:
        """net name -> [(peer_ref, peer_pin), ...] for every net `ref` touches."""
        out: dict[str, list[tuple[str, str]]] = {}
        c = self.components.get(ref)
        if not c:
            return out
        for net_name in set(c.pins.values()):
            net = self.nets.get(net_name)
            if not net:
                continue
            out[net_name] = [(r, p) for (r, p) in net.pins if r != ref]
        return out


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_from(counts: dict[str, list[str]],
               pin_net: dict[tuple[str, str], str],
               components: list[dict]) -> DesignGraph:
    """Assemble a DesignGraph from already-extracted data (pure — testable without
    kicad-cli).

    `counts`/`pin_net` are `cli.netlist_nets` output; `components` is
    `schparse.components` output (``{reference, value, mpn}``). Components that appear
    only in the netlist (e.g. sheets not walked) still get a bare entry so
    connectivity queries never miss a ref.
    """
    g = DesignGraph()

    for c in components:
        ref = c["reference"]
        g.components[ref] = Component(ref=ref, value=c.get("value", ""),
                                      mpn=c.get("mpn", ""))

    for (ref, pin), net_name in pin_net.items():
        g.components.setdefault(ref, Component(ref=ref)).pins[pin] = net_name

    for net_name, refpins in counts.items():
        kind, voltage = infer_net_role(net_name)
        pins: list[tuple[str, str]] = []
        for rp in refpins:
            ref, _, pin = rp.partition(".")
            pins.append((ref, pin))
        g.nets[net_name] = Net(name=net_name, kind=kind, voltage=voltage, pins=pins)

    return g


def build(sch: str | Path) -> DesignGraph:
    """Build the graph for a schematic hierarchy.

    One netlist export (whole hierarchy) + a structural parse of every sheet in the
    root sheet's directory (value/MPN live on the instance symbols, per sheet).
    """
    from . import cli, schparse
    sch = Path(sch)
    counts, pin_net = cli.netlist_nets(sch)
    comps: list[dict] = []
    seen: set[str] = set()
    for sheet in sorted(sch.parent.glob("*.kicad_sch")):
        for c in schparse.components(sheet):
            if c["reference"] not in seen:
                seen.add(c["reference"])
                comps.append(c)
    return build_from(counts, pin_net, comps)
