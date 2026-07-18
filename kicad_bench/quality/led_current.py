"""led-current — LED forward-current check (read-only).

For each LED with a known forward voltage and current rating, find its series
resistor through the design graph, take the loop's known end voltages, and compare
the worst-case current I = (V_loop - Vf) / R against the rating:

  * I > rating        -> error (over the datasheet forward current)
  * I > 90% of rating -> warn  (no margin)

The topology detection is deliberately narrow — a simple rail—R—LED—rail/gnd loop
with both end voltages known. Anything ambiguous (no single series resistor, another
IC on the LED net that might be a constant-current driver, an unknown drive rail) is
*skipped* and counted in one info line; guessing here would only produce false
errors. Vf / If come from extracted constraints when available, else from the
Value/notes strings ("RED 2.0V 20mA").

Idea from Pinscope's deterministic LED check (implementation is original).
Read-only. Exit 0/1/2 per kb convention.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

from ..core import config as cfgmod, conspec, graph as graphmod, partspec
from ..core.report import Result, render_and_exit

_RES_REF = re.compile(r"^R\d+$")
WARN_FRACTION = 0.9

ConstraintsLookup = Callable[[str], dict | None]


def _is_led(comp: graphmod.Component) -> bool:
    return comp.ref.startswith("LED") or "LED" in comp.value.upper()


def _led_spec(comp: graphmod.Component,
              lookup: ConstraintsLookup | None) -> tuple[float | None, float | None]:
    """(Vf volts, If_max amps) — None for anything not clearly stated."""
    key = conspec.part_key(comp) if lookup else ""
    if lookup and key:
        spec = ((lookup(key) or {}).get("spec") or {})
        vf, imax = spec.get("vf"), spec.get("if_max")
        if vf is not None and imax is not None:
            return float(vf), float(imax)
    parsed = partspec.parse_value(comp.value)
    return parsed.get("v_rating"), parsed.get("amps")


def _series_path(g: graphmod.DesignGraph, led: graphmod.Component
                 ) -> tuple[float, float, str] | None:
    """Resolve a simple rail—R—LED—rail/gnd loop.

    Returns (loop volts, series ohms, detail) or None when the topology is not
    unambiguously that shape.
    """
    nets = [g.nets.get(n) for n in dict.fromkeys(led.pins.values())]
    if len(nets) != 2 or any(n is None for n in nets):
        return None

    def far_end(net: graphmod.Net) -> tuple[float | None, float | None, str]:
        """(end volts, series ohms, detail) looking outward through this net."""
        if net.voltage is not None:            # LED pin sits directly on a known rail/gnd
            return net.voltage, 0.0, net.name
        peers = [(r, p) for r, p in net.pins if r != led.ref]
        # exactly one peer, and it must be a resistor — anything else (driver IC,
        # transistor, parallel parts) makes the loop ambiguous
        if len(peers) != 1 or not _RES_REF.match(peers[0][0]):
            return None, None, ""
        rref = peers[0][0]
        rcomp = g.components[rref]
        ohms = partspec.parse_value(rcomp.value, hint="R").get("ohms")
        if not ohms:
            return None, None, ""
        far_nets = [n for n in dict.fromkeys(rcomp.pins.values()) if n != net.name]
        if len(far_nets) != 1:
            return None, None, ""
        v = g.rail_voltage(far_nets[0])
        if v is None:
            return None, None, ""
        return v, ohms, f"{far_nets[0]} -> {rref}"

    ends = [far_end(n) for n in nets]
    if any(e[0] is None for e in ends):
        return None
    ohms = ends[0][1] + ends[1][1]
    if ohms <= 0:                              # LED straight across two rails — no limiter
        ohms = 0.0
    volts = abs(ends[0][0] - ends[1][0])
    return volts, ohms, f"{ends[0][2]} .. {ends[1][2]}"


def check(g: graphmod.DesignGraph,
          lookup: ConstraintsLookup | None = None) -> Result:
    """Pure LED forward-current check over a built design graph."""
    res = Result("LED forward current")
    checked = flagged = no_spec = no_path = 0

    for ref in sorted(g.components):
        comp = g.components[ref]
        if not _is_led(comp) or not comp.pins:
            continue
        vf, imax = _led_spec(comp, lookup)
        if vf is None or imax is None:
            no_spec += 1
            continue
        path = _series_path(g, comp)
        if path is None:
            no_path += 1
            continue
        volts, ohms, detail = path
        if volts <= vf:
            continue                            # LED can't conduct meaningfully
        if ohms == 0.0:
            flagged += 1
            res.error(f"{ref} ({comp.value}) — no series resistance in a "
                      f"{volts:g}V loop (Vf {vf:g}V)", ref, detail)
            checked += 1
            continue
        current = (volts - vf) / ohms
        checked += 1
        label = f"{ref} ({comp.value})"
        ma, lim_ma = current * 1e3, imax * 1e3
        if current > imax:
            flagged += 1
            res.error(f"{label} — {ma:.1f}mA exceeds {lim_ma:g}mA rating",
                      ref, f"I = ({volts:g}V - {vf:g}V) / {ohms:g}Ω; {detail}")
        elif current > imax * WARN_FRACTION:
            flagged += 1
            res.warn(f"{label} — {ma:.1f}mA is within {lim_ma:g}mA rating but "
                     f"leaves <10% margin",
                     ref, f"I = ({volts:g}V - {vf:g}V) / {ohms:g}Ω; {detail}")

    skipped = no_spec + no_path
    if skipped:
        res.info(f"{skipped} LED(s) skipped — {no_spec} without Vf/If data, "
                 f"{no_path} with ambiguous drive topology")
    if checked and not flagged:
        res.ok(f"all {checked} resolvable LED path(s) within rating")
    res.summary = f"{checked} checked, {flagged} flagged, {skipped} skipped"
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch or not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    from ..core import conspec
    return render_and_exit(check(graphmod.build(sch), lookup=conspec.lookup(cfg)))


def add_parser(sub):
    p = sub.add_parser("led-current",
                       help="LED forward-current check (series-R loop vs rating)")
    p.add_argument("schematic", nargs="?",
                   help="root sheet (default: project.root_sch / active board)")
    p.add_argument("--board", help="which board (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
