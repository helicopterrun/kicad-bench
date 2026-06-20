"""netclass-coverage — the ACTUAL per-net class assignment in the project.

KiCad does not class nets from any planning document; it resolves each net's class
from the project's `net_settings` (in the .kicad_pro):
  * an explicit per-net entry in `netclass_assignments` wins, else
  * the matching `netclass_patterns` rule applies (wildcard match), else
  * the net falls to **Default**.

This tool reports that REAL assignment — how many nets actually land on a non-Default
class vs. silently inherit Default — and then cross-references the *planning* config
(`fab.config` domains) to surface the gap: nets the plan wants on a power/bias class
but the project currently leaves on Default. The earlier version only reported the
planning side, which is not what KiCad applies.

Read-only. Exits 0 by default; `--strict --min-coverage F` fails when the fraction of
nets the plan wants classed that are ACTUALLY non-Default drops below F.
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path

from ..core import cli, config as cfgmod
from ..core.report import Result

DEFAULT_CLASS = "Default"


def read_net_settings(cfg: cfgmod.Config) -> dict:
    """Pull net_settings (classes, patterns, assignments) from the .kicad_pro."""
    pro = None
    if cfg.pcb:
        cand = cfg.pcb.with_suffix(".kicad_pro")
        if cand.exists():
            pro = cand
    if pro is None and cfg.root_sch:
        cand = cfg.root_sch.with_suffix(".kicad_pro")
        if cand.exists():
            pro = cand
    if pro is None:
        return {}
    try:
        return json.loads(pro.read_text()).get("net_settings", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _pattern_matches(pattern: str, net: str) -> bool:
    """KiCad net-class pattern match. Per the KiCad 10 docs a pattern is matched as
    BOTH a glob (`*` = any chars incl. none, `?` = one char) AND a wxWidgets-style
    regex — a net matches if either flavour matches. We approximate that with
    fnmatch (glob, anchored) OR re.search (regex, partial), which covers the doc's
    own examples (e.g. regex `net*` also matching `ne`)."""
    if fnmatch.fnmatchcase(net, pattern):
        return True
    try:
        return re.search(pattern, net) is not None
    except re.error:
        return False


def resolve_classes(net: str, settings: dict) -> set[str]:
    """All non-default classes a net actually gets (KiCad: a net is assigned to
    *every* matching pattern's class, plus any explicit per-net assignment). An
    empty set means only the implicit Default applies."""
    out: set[str] = set()
    val = (settings.get("netclass_assignments") or {}).get(net)
    if isinstance(val, list):
        out.update(val)
    elif isinstance(val, str):
        out.add(val)
    for entry in settings.get("netclass_patterns") or []:
        pat, klass = entry.get("pattern"), entry.get("netclass")
        if pat and klass and _pattern_matches(pat, net):
            out.add(klass)
    out.discard(DEFAULT_CLASS)
    return out


def real_nets(sch: Path) -> list[str]:
    counts, _ = cli.netlist_nets(sch)
    return sorted(n.split("/")[-1] for n in counts
                  if n and not n.startswith("unconnected-"))


def analyze(sch: Path, cfg: cfgmod.Config, fab_cfg: dict | None):
    """Return (Result, stats). stats: n_total, n_default, n_assigned, plan_gap."""
    settings = read_net_settings(cfg)
    classes = [c.get("name") for c in settings.get("classes", [])]
    has_rules = bool(settings.get("netclass_patterns") or settings.get("netclass_assignments"))

    nets = real_nets(sch)
    by_class: dict[str, list[str]] = {}
    for n in nets:
        classes = resolve_classes(n, settings)
        key = ", ".join(sorted(classes)) if classes else DEFAULT_CLASS
        by_class.setdefault(key, []).append(n)
    n_default = len(by_class.get(DEFAULT_CLASS, []))
    n_assigned = len(nets) - n_default

    res = Result("Net-class coverage (actual project assignment)")
    res.info(f"classes defined in project: {', '.join(classes) or '(none)'}")
    if not has_rules:
        res.warn(f"NO netclass patterns or assignments in the project — "
                 f"all {len(nets)} nets fall to {DEFAULT_CLASS}",
                 detail="define patterns in Board Setup → Net Classes before routing")
    for klass in sorted(by_class):
        tag = "info" if klass != DEFAULT_CLASS else "warn"
        getattr(res, tag)(f"{klass}: {len(by_class[klass])} net(s)")

    # Cross-reference the planning config: which nets a non-default domain wants.
    plan_gap: list[tuple[str, str]] = []
    if fab_cfg:
        plan_class: dict[str, str] = {}
        for dom, group in fab_cfg.get("nets", {}).items():
            if dom == "GND":
                continue
            for net in group:
                plan_class[net] = dom
        for spec in fab_cfg.get("diff_pairs", {}).values():
            for base in spec.get("members", []):
                for nm in (f"{base}_P", f"{base}_N", f"{base}P", f"{base}N"):
                    plan_class[nm] = "diff-pair"
        present = set(nets)
        for net, dom in sorted(plan_class.items()):
            if net in present and not resolve_classes(net, settings):
                plan_gap.append((net, dom))
        for net, dom in plan_gap:
            res.warn(f"{net}: plan wants '{dom}' class, project leaves it on {DEFAULT_CLASS}", net)

    stats = dict(n_total=len(nets), n_default=n_default, n_assigned=n_assigned,
                 plan_wanted=len(plan_gap) + (n_assigned if fab_cfg else 0),
                 plan_gap=len(plan_gap))
    res.summary = (f"{n_assigned}/{len(nets)} nets on a non-Default class; "
                   f"{n_default} on {DEFAULT_CLASS}"
                   + (f"; {len(plan_gap)} planned net(s) still unclassed" if fab_cfg else ""))
    return res, stats


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = cfg.root_sch
    if not sch or not sch.exists():
        sys.exit("error: project.root_sch missing or not found")
    fab_cfg = None
    if cfg.fab_config and cfg.fab_config.exists():
        fab_cfg = json.loads(cfg.fab_config.read_text())
    try:
        res, stats = analyze(sch, cfg, fab_cfg)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    print(res.render())
    if args.strict and fab_cfg:
        wanted = stats["plan_wanted"]
        if wanted and ((wanted - stats["plan_gap"]) / wanted) < args.min_coverage:
            return 1
    return 0


def add_parser(sub):
    p = sub.add_parser("netclass-coverage", help="report ACTUAL per-net class assignment + plan gap")
    p.add_argument("--strict", action="store_true", help="fail if planned-net coverage below --min-coverage")
    p.add_argument("--min-coverage", type=float, default=0.9, help="fraction for --strict (default 0.9)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
