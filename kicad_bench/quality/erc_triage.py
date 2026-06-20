"""erc-triage — run ERC and classify each violation REAL vs. ALLOWED.

ALLOWED buckets (all carry a reason in the report):
  1. partial-build hier pin — pin_not_connected on a hierarchical label whose
     parent sheet doesn't exist yet (leaf captured before integration);
  2. driven-by-parent — (power_)pin_not_driven on a pin whose net is a
     hierarchical INPUT on this sheet (the driver is the not-yet-wired parent);
  3. configured allow-rule — a documented project false-positive from
     `[ [erc.allow] ]`, e.g. the power:GND 0.47 mm offset on U5.7.

Everything else is REAL and fails. The classification logic (1)+(2) mirrors the
project's validate_sheet.py so behaviour is identical; (3) is the config layer.

Read-only. Exits 0 if no REAL errors, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ..core import cli, config as cfgmod, schparse
from ..core.report import Result, render_and_exit

# A violation block: "[type]: message" then an optional "; ..." then "@(x,y): where".
_BLOCK = re.compile(r"\[(\w+)\]:\s*(.+?)\n(?:\s*;.*\n)?\s*@\(([^)]*)\):\s*(.+)")
_SYMPIN = re.compile(r"Symbol\s+(\S+)\s+Pin\s+(\S+)")


def triage(sch: Path, pin_net: dict, in_nets: set, allow_rules: list,
           report: str | None = None) -> Result:
    res = Result(f"ERC triage: {sch.name}")
    if report is None:
        report = cli.erc_report(sch)
    n_total = 0
    for vtype, msg, loc, where in _BLOCK.findall(report):
        n_total += 1
        msg = msg.strip()
        loc, where = loc.strip(), where.strip()
        ref = pin = None
        pm = _SYMPIN.search(where)
        if pm:
            ref, pin = pm.group(1), pm.group(2)

        # (1) partial-build hier pin
        partial_hier = (
            vtype == "pin_not_connected"
            and "Hierarchical label" in msg
            and "non-existent parent sheet" in msg
        )
        # (2) driven by not-yet-wired parent
        driven_by_parent = False
        if vtype in ("pin_not_driven", "power_pin_not_driven") and ref:
            net = pin_net.get((ref, pin))
            driven_by_parent = net in in_nets
        # (3) configured allow-rule
        rule = next((r for r in allow_rules if r.matches(vtype, msg, ref, pin)), None)

        line = f"[{vtype}] {msg}"
        whereloc = f"{ref}.{pin}" if ref else loc
        if partial_hier:
            res.allowed(line, whereloc, "partial-build: hier pin, parent not wired yet")
        elif driven_by_parent:
            net = pin_net.get((ref, pin))
            res.allowed(line, whereloc, f"partial-build: net {net} is a hier input (driven by parent)")
        elif rule:
            res.allowed(line, whereloc, f"allow-rule: {rule.reason}")
        else:
            res.error(line, whereloc)

    n_allowed = sum(1 for f in res.findings if f.severity == "allowed")
    if res.n_errors:
        res.summary = f"{res.n_errors} real, {n_allowed} allowed (of {n_total})"
    else:
        res.summary = f"ERC clean — {n_allowed} allowed partial-build/known exception(s)"
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch:
        sys.exit("error: no schematic given and no project.root_sch in config")
    if not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    sch = Path(sch)
    try:
        _, pin_net = cli.netlist_nets(sch)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    in_nets = schparse.input_hier_nets(sch)
    res = triage(sch, pin_net, in_nets, cfg.allow_rules)
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("erc-triage", help="run ERC, classify real vs. known-allowed")
    p.add_argument("schematic", nargs="?", help="sheet to check (default: project.root_sch)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
