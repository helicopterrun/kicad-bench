"""stackup-sync — reconcile the chosen fab stackup/constraints with the project.

The project's board-setup (layer stack, min trace/space/drill/via) is the single
biggest "filled in the config but not the project file" gap before layout. This
tool reads the fab/design-rules JSON (layers, thickness, copper, fab_min) plus the
chosen stackup id and produces:
  * a recommended board-setup table (the values to enter in Board Setup), and
  * a diff against whatever the .kicad_pro/.kicad_pcb currently declare.

It is EMIT-AND-YOU-APPLY: by default it only reports; with --emit it writes a
markdown guide to output/. It never writes the .kicad_pcb (the schematic/board
design is never mutated by kicad-bench). Apply the values yourself in KiCad.

Read-only by default. Exit 0 (report), 2 (setup error).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, style


def _current_settings(cfg: cfgmod.Config) -> dict[str, str]:
    """Best-effort read of current min-rules from .kicad_pro design_settings."""
    out: dict[str, str] = {}
    pro = None
    if cfg.pcb:
        cand = cfg.pcb.with_suffix(".kicad_pro")
        if cand.exists():
            pro = cand
    if not pro:
        return out
    try:
        data = json.loads(pro.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    rules = (data.get("board", {}).get("design_settings", {}).get("rules", {}))
    for k in ("min_track_width", "min_clearance", "min_through_hole_diameter",
              "min_via_diameter"):
        if k in rules:
            out[k] = str(rules[k])
    return out


def build(cfg: cfgmod.Config) -> tuple[Result, str]:
    res = Result("Stackup sync")
    if not cfg.fab_config or not cfg.fab_config.exists():
        sys.exit("error: fab.config (design-rules JSON) not configured or missing")
    fab = json.loads(cfg.fab_config.read_text())
    fab_min = fab.get("fab_min", {})

    recommended = {
        "layers": fab.get("layers"),
        "board_thickness_mm": fab.get("board_thickness"),
        "copper_weight": fab.get("copper_weight"),
        "stackup_id": cfg.stackup or fab.get("stackup", "(unset)"),
        "min_track_width": fab_min.get("trace"),
        "min_clearance": fab_min.get("clearance"),
        "min_through_hole_diameter": fab_min.get("drill"),
        "min_via_diameter": fab_min.get("via_diameter"),
    }
    current = _current_settings(cfg)

    res.info(f"target stackup: {recommended['stackup_id']} — "
             f"{recommended['layers']}-layer, {recommended['board_thickness_mm']}mm, "
             f"{recommended['copper_weight']}")
    for key in ("min_track_width", "min_clearance",
                "min_through_hole_diameter", "min_via_diameter"):
        want = recommended[key]
        have = current.get(key)
        if have is None:
            res.warn(f"{key}: recommend {want}mm — not set in project", key)
        elif str(have) != str(want):
            res.warn(f"{key}: project has {have}mm, recommend {want}mm", key)
        else:
            res.ok(f"{key}: {have}mm matches", key)

    res.summary = "apply recommended values in KiCad → Board Setup (this tool does not write the PCB)"

    # markdown guide
    L = [f"# Board-setup guide — {fab.get('circuit_name', cfg.root.name)}", ""]
    L.append("Apply in **PCB Editor → File → Board Setup**. kicad-bench does not "
             "write the .kicad_pcb; enter these by hand.")
    L.append("")
    L.append(f"- **Stackup:** {recommended['stackup_id']}")
    L.append(f"- **Layers:** {recommended['layers']}")
    L.append(f"- **Board thickness:** {recommended['board_thickness_mm']} mm")
    L.append(f"- **Copper weight:** {recommended['copper_weight']}")
    L.append("")
    L.append("| Constraint | Recommend (mm) | Project now |")
    L.append("|---|---|---|")
    for key in ("min_track_width", "min_clearance",
                "min_through_hole_diameter", "min_via_diameter"):
        L.append(f"| {key} | {recommended[key]} | {current.get(key, '—')} |")
    L.append("")
    L.append("After setting these, regenerate custom rules with `kb netclass-coverage` "
             "and the project's `gen_design_rules.py`, then copy the `.kicad_dru` in.")
    return res, "\n".join(L) + "\n"


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    res, guide = build(cfg)
    print(res.render())
    if args.emit:
        out_dir = cfg.root / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / "board_setup_guide.md"
        dest.write_text(guide)
        print(style.dim(f"  wrote {dest}"))
    return 0


def add_parser(sub):
    p = sub.add_parser("stackup-sync", help="reconcile fab stackup/constraints with the project (emit-and-apply)")
    p.add_argument("--emit", action="store_true", help="write output/board_setup_guide.md")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
