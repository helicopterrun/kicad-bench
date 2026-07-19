"""cap-derating — capacitor voltage-derating check (read-only).

For every capacitor whose rated voltage is known, compare the worst-case operating
voltage (from the design graph's inferred rail voltages) against the rating derated
per dielectric class:

  * operating > rated                      -> error (over absolute rating)
  * operating > rated x derating factor    -> warn  (inside rating, outside policy)

Ratings come from extracted per-MPN constraints when available, else are parsed from
the schematic Value/notes strings ("100nF 16V X7R"). Caps with no known rating or no
known net voltage are *skipped* and counted in one info line — this check never
guesses. Factors live in the `[derating]` config section (defaults: ceramic 0.8,
tantalum 0.5, electrolytic 0.8).

Idea from Pinscope's deterministic derating table (implementation is original).
Read-only. Exit 0/1/2 per kb convention (warnings never fail).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

from ..core import config as cfgmod, conspec, graph as graphmod, partspec
from ..core.report import Result, render_and_exit

DEFAULT_FACTORS = {"ceramic": 0.8, "tantalum": 0.5, "electrolytic": 0.8}

_CAP_REF = re.compile(r"^C\d+$")

# Optional per-MPN constraints lookup (mpn -> constraints dict); wired to the
# extracted-constraints store when available (kb constraints), else None.
ConstraintsLookup = Callable[[str], dict | None]


def _cap_rating(comp: graphmod.Component,
                lookup: ConstraintsLookup | None) -> tuple[float | None, str]:
    """(rated volts | None, dielectric class) for a capacitor."""
    key = conspec.part_key(comp) if lookup else ""
    if lookup and key:
        cons = lookup(key)
        spec = (cons or {}).get("spec") or {}
        v = spec.get("v_rating")
        if v is not None:
            return float(v), partspec.dielectric_class(spec.get("dielectric"), comp.value)
    parsed = partspec.parse_value(comp.value)
    return (parsed.get("v_rating"),
            partspec.dielectric_class(parsed.get("dielectric"), comp.value))


def _operating_voltage(g: graphmod.DesignGraph,
                       comp: graphmod.Component) -> tuple[float | None, str]:
    """Worst-case DC across the cap from known net voltages: (volts | None, detail)."""
    nets = set(comp.pins.values())
    known: list[tuple[str, float]] = []
    for net_name in nets:
        v = g.rail_voltage(net_name)
        if v is not None:
            known.append((net_name, v))
    if not known:
        return None, ""
    if len(known) == 1:
        # One known rail, other side unknown — assume the far side can sit at 0 V
        # (worst plausible case for a rail-connected cap; still conservative).
        name, v = known[0]
        if v == 0.0 and len(nets) > 1:
            # ...but the *known* side is ground and the rail is the unknown one. The
            # "far side sits at 0 V" reasoning inverts here and would report 0 V across
            # the part — a silent pass for a cap that may be sitting on a 20 V rail.
            # Unknown is unknown: skip.
            return None, ""
        return abs(v), name
    vmax = max(known, key=lambda kv: kv[1])
    vmin = min(known, key=lambda kv: kv[1])
    return vmax[1] - vmin[1], f"{vmax[0]} - {vmin[0]}"


def check(g: graphmod.DesignGraph,
          factors: dict[str, float] | None = None,
          lookup: ConstraintsLookup | None = None) -> Result:
    """Pure derating check over a built design graph."""
    factors = {**DEFAULT_FACTORS, **(factors or {})}
    res = Result("Capacitor derating")
    checked = flagged = no_rating = no_voltage = 0

    for ref in sorted(g.components):
        comp = g.components[ref]
        if not _CAP_REF.match(ref):
            continue
        rated, dclass = _cap_rating(comp, lookup)
        if rated is None:
            no_rating += 1
            continue
        op, source = _operating_voltage(g, comp)
        if op is None:
            no_voltage += 1
            continue
        checked += 1
        factor = factors.get(dclass, 0.8)
        limit = rated * factor
        where = ref
        label = f"{ref} ({comp.value or comp.mpn})"
        if op > rated:
            flagged += 1
            res.error(f"{label} — {op:g}V across a {rated:g}V part",
                      where, f"nets: {source}; over absolute rating")
        elif op > limit:
            flagged += 1
            res.warn(f"{label} — {op:g}V exceeds {factor:.0%} derating of {rated:g}V "
                     f"({dclass}: limit {limit:g}V)",
                     where, f"nets: {source}")

    skipped = no_rating + no_voltage
    if skipped:
        res.info(f"{skipped} capacitor(s) skipped — "
                 f"{no_rating} without a known voltage rating, "
                 f"{no_voltage} without a known net voltage")
    if checked and not flagged:
        res.ok(f"all {checked} rated capacitor(s) within derating policy")
    res.summary = (f"{checked} checked, {flagged} flagged, {skipped} skipped "
                   f"(no rating/voltage data)")
    return res


def factors_from_cfg(cfg: cfgmod.Config) -> dict[str, float]:
    raw = cfg.raw.get("derating", {}) if isinstance(cfg.raw, dict) else {}
    return {k: float(v) for k, v in raw.items()
            if k in DEFAULT_FACTORS and isinstance(v, (int, float))}


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch or not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    g = graphmod.build(sch)
    lookup = _lookup(cfg)
    graphmod.solve_rail_voltages(g, lookup)
    return render_and_exit(check(g, factors_from_cfg(cfg), lookup))


def _lookup(cfg: cfgmod.Config) -> ConstraintsLookup:
    from ..core import conspec
    return conspec.lookup(cfg)


def add_parser(sub):
    p = sub.add_parser("cap-derating",
                       help="capacitor voltage-derating check (rated vs net voltage)")
    p.add_argument("schematic", nargs="?",
                   help="root sheet (default: project.root_sch / active board)")
    p.add_argument("--board", help="which board (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
