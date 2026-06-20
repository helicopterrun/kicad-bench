"""netlist-audit — per-net node-count audit.

ERC cannot see a net that was silently merged into GND (the L-stub bug class):
the nodes still exist, they just landed on the wrong net. This compares every
named net's node count against an expected map and fails on any mismatch or any
expected net missing from the netlist entirely.

Expected counts come from `contracts.expected_nets` in the config (a JSON map
{net_name: count}). With no expected map, the tool just prints the node-count
table (useful for building the baseline). Generalizes validate_sheet.py --expect.

Read-only. Exits 0 if all expected nets match, 1 otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import cli, config as cfgmod
from ..core.report import Result, render_and_exit


def audit(sch: Path, expect: dict[str, int] | None) -> Result:
    res = Result(f"Netlist audit: {sch.name}")
    counts, _ = cli.netlist_nets(sch)
    expect = expect or {}

    for net in sorted(expect):
        got = len(counts.get(net, []))
        if net not in counts:
            res.error(f"{net}: MISSING from netlist (expected {expect[net]})", net)
        elif got != expect[net]:
            res.error(f"{net}: {got} nodes, expected {expect[net]}", net,
                      detail="nodes: " + ", ".join(counts[net]))
        else:
            res.ok(f"{net}: {got} nodes", net)

    if not expect:
        for net in sorted(counts):
            res.info(f"{net}: {len(counts[net])} nodes", net,
                     detail=", ".join(counts[net]))
        res.summary = (f"{len(counts)} nets (no expected_nets configured — "
                       "this is a baseline dump)")
    else:
        res.summary = (f"{res.n_errors} mismatch(es) of {len(expect)} checked "
                       f"({len(counts)} nets total)")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch:
        sys.exit("error: no schematic given and no project.root_sch in config")
    if not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    expect = cfg.expected_nets
    if args.expect:
        expect = json.loads(Path(args.expect).read_text())
    try:
        res = audit(Path(sch), expect)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("netlist-audit", help="per-net node-count audit (catches GND merges)")
    p.add_argument("schematic", nargs="?", help="sheet to check (default: project.root_sch)")
    p.add_argument("--expect", help="override: JSON {net: count}")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
