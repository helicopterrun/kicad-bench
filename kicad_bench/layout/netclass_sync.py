"""netclass-sync — generate KiCad net_settings from the planning config.

Closes the gap netclass-coverage surfaces: the planning config
(`design_rules_config.json`) knows which nets belong to which power/bias/diff-pair
domain, but the project's `.kicad_pro` has no patterns/assignments, so every net is
Default. This emits a ready-to-merge `net_settings` block:

  * `classes` — one per planning domain (+ diff-pair), with clearance / track_width
    / via size / via drill (and diff_pair_gap/width) taken straight from the config;
  * `netclass_assignments` — an EXACT net-name -> [class] map (no wildcard pattern
    ambiguity), only for nets that actually exist in the netlist.

By default it writes the fragment to output/ and prints a summary — it does NOT
touch the project. `--write` merges the fragment into the `.kicad_pro` net_settings
(adding classes by name, setting assignments) after a `.bak` backup. It never writes
a `.kicad_sch`/`.kicad_pcb` — only project settings, and only on request.

Read-only by default. Exit 0, or 2 on setup error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import cli, config as cfgmod
from ..core.report import Result, style


def build_settings(fab_cfg: dict, present: set[str]):
    """Return (classes, assignments, planned, missing) from the planning config.

    classes: list of KiCad class dicts; assignments: {net: [class]};
    planned: every planned net; missing: planned nets absent from the netlist.
    """
    classes: list[dict] = []
    assignments: dict[str, list[str]] = {}
    planned: set[str] = set()

    domains = fab_cfg.get("domains", {})
    nets = fab_cfg.get("nets", {})
    for dom, group in nets.items():
        prof = domains.get(dom, {})
        cls = {"name": dom}
        if "internal_clearance" in prof: cls["clearance"] = prof["internal_clearance"]
        if "track_width" in prof:        cls["track_width"] = prof["track_width"]
        if "via_diameter" in prof:       cls["via_diameter"] = prof["via_diameter"]
        if "via_drill" in prof:          cls["via_drill"] = prof["via_drill"]
        classes.append(cls)
        for net in group:
            planned.add(net)
            if net in present:
                assignments[net] = [dom]

    for dp, spec in fab_cfg.get("diff_pairs", {}).items():
        cls = {"name": dp}
        if spec.get("track_width") is not None:
            cls["track_width"] = spec["track_width"]
            cls["diff_pair_width"] = spec["track_width"]
        if spec.get("gap") is not None:
            cls["diff_pair_gap"] = spec["gap"]
        if spec.get("coplanar_clearance") is not None:
            cls["clearance"] = spec["coplanar_clearance"]
        classes.append(cls)
        for base in spec.get("members", []):
            for net in (f"{base}_P", f"{base}_N"):
                planned.add(net)
                if net in present:
                    assignments[net] = [dp]

    missing = sorted(planned - present)
    return classes, assignments, planned, missing


def merge_into_pro(pro: Path, classes: list[dict], assignments: dict) -> Result:
    res = Result(f"Merge net_settings -> {pro.name}")
    data = json.loads(pro.read_text())
    ns = data.setdefault("net_settings", {})
    existing = ns.setdefault("classes", [])
    existing_names = {c.get("name") for c in existing}
    added = 0
    for cls in classes:
        if cls["name"] not in existing_names:
            existing.append(cls)
            existing_names.add(cls["name"])
            added += 1
    cur = ns.get("netclass_assignments")
    if not isinstance(cur, dict):
        cur = {}
    cur.update(assignments)
    ns["netclass_assignments"] = cur

    bak = pro.with_suffix(pro.suffix + ".bak")
    bak.write_text(pro.read_text())
    pro.write_text(json.dumps(data, indent=2) + "\n")
    res.ok(f"added {added} class(es), set {len(assignments)} net assignment(s)")
    res.info(f"backup written: {bak.name}")
    res.summary = "net_settings merged — open in KiCad to verify"
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if not cfg.fab_config or not cfg.fab_config.exists():
        sys.exit("error: fab.config (design-rules JSON) not configured or missing")
    if not cfg.root_sch or not cfg.root_sch.exists():
        sys.exit("error: project.root_sch missing or not found")
    fab_cfg = json.loads(cfg.fab_config.read_text())
    try:
        counts, _ = cli.netlist_nets(cfg.root_sch)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    present = {n.split("/")[-1] for n in counts if n and not n.startswith("unconnected-")}

    classes, assignments, planned, missing = build_settings(fab_cfg, present)

    res = Result("Net-class sync")
    res.info(f"{len(classes)} class(es) from plan; {len(assignments)} of {len(planned)} "
             f"planned nets present and assignable")
    for net in missing:
        res.warn(f"planned net not in netlist (skipped): {net}", net)
    print(res.render())

    if args.write:
        pro = cfg.pcb.with_suffix(".kicad_pro") if cfg.pcb else None
        if not pro or not pro.exists():
            sys.exit("error: cannot locate .kicad_pro to write")
        print(merge_into_pro(pro, classes, assignments).render())
        print(style.dim("  next: copy the generated .kicad_dru next to the board "
                        "(see `kb dfm-preflight`) so custom rules apply."))
    else:
        out = cfg.root / "output"
        out.mkdir(parents=True, exist_ok=True)
        dest = out / "netclass_settings.json"
        dest.write_text(json.dumps(
            {"classes": classes, "netclass_assignments": assignments}, indent=2) + "\n")
        print(style.dim(f"  wrote {dest} — merge into .kicad_pro net_settings, "
                        "or re-run with --write to merge automatically (makes a .bak)."))
    return 0


def add_parser(sub):
    p = sub.add_parser("netclass-sync", help="generate net_settings classes+assignments from the plan")
    p.add_argument("--write", action="store_true",
                   help="merge into the project's .kicad_pro (backs up to .bak first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
