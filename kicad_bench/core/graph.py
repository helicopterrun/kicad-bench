"""graph.py — queryable bipartite design graph (components ↔ nets).

Built once per audit from the shared netlist export (`cli.netlist_nets`) joined to the
structural component list (`schparse.components`). This is the substrate the
datasheet-grounded checks (cap-derating, led-current, pin-mux) and the `kb review`
engine query, instead of each re-deriving connectivity from raw exports.

Net *roles* (ground / power / signal) and nominal rail voltages are inferred from net
names — `+3V3`, `VCC_1V8`, `GND` — the same convention the house templates use for
power nets. Inference is deliberately conservative: a name that doesn't clearly assert
a rail stays a `signal` with no voltage, and downstream checks must treat a missing
voltage as "unknown", never as 0.

`solve_rail_voltages()` is an optional second pass that resolves what the names leave
unknown, from the circuit itself: a regulator's datasheet output voltage propagated
through the pin the datasheet calls an output. It only ever fills in unknowns — a
named rail is the designer's assertion and always wins — and every net records how
its voltage was established in `voltage_source`.

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
# "..._5V" / "..._2V8" / "..._3.3V" as a trailing rail assertion.
_SUFFIX_V = re.compile(r"_(\d+V\d+|\d+(?:\.\d+)?V)$")


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

    # A name *ending* in a voltage token asserts a rail even with no power prefix —
    # "HDMI_5V", "TOUCH_VDD_2V8". The designer wrote the voltage down; read it.
    # Anchored to the end and requiring the separating '_' so signal names that merely
    # contain a digit-V run ("LVDS_D3V_P") can't match.
    if _SUFFIX_V.search(upper):
        v = _parse_rail_voltage(upper)
        if v is not None:
            return "power", v

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
    # How `voltage` was established: "netname" (inferred from the net's own name),
    # "vout" (a regulator's datasheet output voltage, propagated through its output
    # pin) or None when the voltage is unknown. Consumers that report a voltage to a
    # human should say which — a name-inferred rail is an assertion by the designer,
    # a computed one is an assertion by the circuit.
    voltage_source: str | None = None


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
        g.nets[net_name] = Net(name=net_name, kind=kind, voltage=voltage, pins=pins,
                               voltage_source="netname" if voltage is not None else None)

    return g


# ---------------------------------------------------------------------------
# Topology-solved rail voltages
# ---------------------------------------------------------------------------

# Pin names that carry a regulator's *regulated output*. Deliberately short: a
# datasheet output voltage may only be propagated through a pin the datasheet itself
# calls an output. Sense pins (FB/ADJ) are excluded — on an adjustable part the rail
# is set by the external divider, not by any single datasheet number.
_OUT_PIN_NAMES = {"VOUT", "OUT", "VO"}


def solve_rail_voltages(g: DesignGraph,
                        lookup=None) -> int:
    """Resolve rail voltages that net names don't assert, from regulator topology.

    A post-pass over an already-built graph rather than a parameter on `build_from`:
    it keeps the builder pure (no config, no datasheet index) and leaves every
    existing call site working. Callers that hold a constraints lookup — the
    datasheet-grounded gates already do — call this right after building.

    Only nets whose voltage is still unknown are ever written, so an explicit rail
    name always wins over a datasheet number. `lookup` is `conspec.lookup(cfg)`;
    without one there is nothing to solve and this is a no-op.

    Returns the number of nets newly resolved.
    """
    if lookup is None:
        return 0
    from . import conspec

    resolved = 0
    for ref in sorted(g.components):
        comp = g.components[ref]
        key = conspec.part_key(comp)
        if not key:
            continue
        cons = lookup(key)
        if not cons:
            continue
        vout = (cons.get("spec") or {}).get("vout")
        if vout is None:
            continue

        # Every net reachable through a pin the datasheet names as an output.
        out_nets = set()
        for p in cons.get("pins") or []:
            name = (p.get("name") or "").strip().upper()
            if name in _OUT_PIN_NAMES:
                net = comp.pins.get(str(p.get("number")))
                if net:
                    out_nets.add(net)
        # Multi-output part (or an ambiguous pin table): we cannot tell which rail
        # `vout` describes. Skip rather than pick one.
        if len(out_nets) != 1:
            continue

        net = g.nets.get(out_nets.pop())
        if net is None or net.voltage is not None:
            continue
        net.voltage = float(vout)
        net.voltage_source = "vout"
        if net.kind == "signal":
            net.kind = "power"          # a regulated output is a rail, whatever it's called
        resolved += 1
    return resolved


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
