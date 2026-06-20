"""netclass-coverage — which nets have an explicit class vs. fall to Default.

Reads the actual netlist and the fab/design-rules JSON (the same config
gen_design_rules.py consumes), then reports every real net that is NOT covered by
a domain or diff-pair member list. Unclassed nets silently inherit the Default
class width — fine for logic, dangerous for power/bias — so this is the gate that
says "set these before routing."

Read-only (reporting). Exits 0 always unless --strict, which fails when coverage
is below --min-coverage (default off). Generalizes preflight_layout.check_netclass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import cli, config as cfgmod
from ..core.report import Result


def classed_nets(fab_cfg: dict) -> set[str]:
    out: set[str] = set()
    for group in fab_cfg.get("nets", {}).values():
        out |= set(group)
    for spec in fab_cfg.get("diff_pairs", {}).values():
        for base in spec.get("members", []):
            out |= {f"{base}_P", f"{base}_N", f"{base}P", f"{base}N"}
    return out


def coverage(sch: Path, fab_cfg: dict) -> Result:
    res = Result("Net-class coverage")
    classed = classed_nets(fab_cfg)
    counts, _ = cli.netlist_nets(sch)
    real = {n.split("/")[-1] for n in counts
            if n and not n.startswith("unconnected-")}

    covered = sorted(real & classed)
    uncovered = sorted(real - classed)
    for n in uncovered:
        res.warn(f"{n}: no explicit class (falls to Default)", n)
    res.info(f"{len(covered)}/{len(real)} nets explicitly classed")
    res.summary = (f"{len(uncovered)} unclassed net(s) — assign a class or set the "
                   "Default class width before routing")
    return res, len(covered), len(real)


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if not cfg.fab_config or not cfg.fab_config.exists():
        sys.exit("error: fab.config (design-rules JSON) not configured or missing")
    sch = cfg.root_sch
    if not sch or not sch.exists():
        sys.exit("error: project.root_sch missing or not found")
    fab_cfg = json.loads(cfg.fab_config.read_text())
    try:
        res, cov, total = coverage(sch, fab_cfg)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    print(res.render())
    if args.strict and total and (cov / total) < args.min_coverage:
        return 1
    return 0


def add_parser(sub):
    p = sub.add_parser("netclass-coverage", help="report nets with no explicit net class")
    p.add_argument("--strict", action="store_true", help="fail if coverage below --min-coverage")
    p.add_argument("--min-coverage", type=float, default=0.9, help="fraction for --strict (default 0.9)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
