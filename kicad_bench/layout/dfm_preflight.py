"""dfm-preflight — whole-project pre-layout / pre-fab DFM gate.

Generalizes the project's preflight_layout.py and adds a PCB DRC pass:
  1. ERC triage on the root sheet — REAL errors are a hard fail (reuses the
     config allow-rules so known false-positives don't block).
  2. symbol-pin count vs. footprint-pad count per placed part — a symbol with MORE
     pins than its footprint has pads is a hard "update PCB from schematic" blocker.
  3. 3D models referenced by project footprints exist (informational).
  4. net-class coverage summary (informational).
  5. if the .kicad_pcb has board content: PCB DRC (severity-error) is a hard fail.

Read-only. Exits non-zero on any hard fail (ERC errors, pins>pads, DRC errors).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..core import cli, config as cfgmod, footprints, schparse
from ..core.report import Result, style
from ..quality import erc_triage
from . import netclass_coverage


def _pins_pads(root, cfg: cfgmod.Config, res: Result) -> None:
    pins = {(lp.get("lib"), lp.get("part")): len(lp.find("pins").findall("pin"))
            for lp in root.iter("libpart") if lp.find("pins") is not None}
    fp_libs = {}
    if cfg.fp_lib_table and cfg.fp_lib_table.exists():
        fp_libs = schparse.parse_fp_lib_table(cfg.fp_lib_table, cfg.fp_lib_table.parent)
    system = footprints.system_footprint_dir()
    blockers = 0
    for c in root.iter("comp"):
        ref = c.get("ref")
        if not ref or ref.startswith("#") or ref.startswith(("FID", "H")):
            continue
        ls = c.find("libsource")
        fp = c.findtext("footprint") or ""
        sp = pins.get((ls.get("lib"), ls.get("part"))) if ls is not None else None
        mod = footprints.resolve_path(fp, fp_libs, system) if fp else None
        np = footprints.pad_count(mod) if mod else None
        if sp is not None and np is not None and sp > np:
            res.error(f"{ref}: {sp} pins > {np} pads ({fp})", ref)
            blockers += 1
    if not blockers:
        res.ok("no symbol exceeds its footprint pads")


def _check_3d(cfg: cfgmod.Config, res: Result) -> None:
    proj_dir = cfg.fp_lib_table.parent if cfg.fp_lib_table else cfg.root
    lib_root = proj_dir.parent  # ${KIPRJMOD}/.. base used by the project
    missing = set()
    for f in proj_dir.parent.glob("Library/*.pretty/*.kicad_mod"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'\(model\s+"([^"]+)"', text):
            rp = m.group(1).replace("${KIPRJMOD}/../", "").replace("\\", "/")
            if not (lib_root / rp).exists():
                missing.add(Path(m.group(1)).name)
    if missing:
        for m in sorted(missing):
            res.warn(f"3D model missing: {m}")
    else:
        res.info("all project-footprint 3D models present")


def _pcb_drc(cfg: cfgmod.Config, res: Result, info: Result) -> None:
    if not cfg.pcb or not cfg.pcb.exists():
        res.info("no .kicad_pcb configured — DRC skipped")
        return
    # An un-laid-out board has no footprints placed; only run DRC if it has any.
    text = cfg.pcb.read_text(errors="ignore")
    if "(footprint " not in text:
        res.info("PCB has no placed footprints yet — DRC skipped (pre-layout)")
        return
    errs = cli.drc_violations(cfg.pcb, ("error",))
    warns = cli.drc_violations(cfg.pcb, ("warning",))
    if errs:
        for v in errs[:10]:
            res.error(f"DRC [{v.get('type')}] {v.get('description')}")
        if len(errs) > 10:
            res.error(f"... and {len(errs) - 10} more DRC error(s)")
    else:
        res.ok(f"PCB DRC clean — 0 errors ({len(warns)} warning(s))")
    # Custom rules are loaded only from <board>.kicad_dru next to the board.
    dru = cfg.pcb.with_suffix(".kicad_dru")
    if dru.exists():
        info.info(f"custom rules active: {dru.name} found next to the board")
    else:
        info.warn(f"custom rules NOT active: no {dru.name} next to the board — "
                  "rules generated into output/ aren't applied until copied here")


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = cfg.root_sch
    if not sch or not sch.exists():
        sys.exit("error: project.root_sch missing or not found")

    hard = Result("DFM preflight")
    info = Result("DFM preflight (informational)")

    try:
        _, pin_net = cli.netlist_nets(sch)
        root = cli.netlist_xml(sch)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")

    # 1. ERC
    in_nets = schparse.input_hier_nets(sch)
    erc = erc_triage.triage(sch, pin_net, in_nets, cfg.allow_rules)
    if erc.n_errors:
        hard.error(f"ERC: {erc.n_errors} real error(s) — run `kb erc-triage` for detail")
    else:
        hard.ok("ERC clean (real errors)")

    # 2. pins vs pads
    _pins_pads(root, cfg, hard)

    # 5. PCB DRC
    _pcb_drc(cfg, hard, info)

    # 3. 3D models (info)
    _check_3d(cfg, info)

    # 4. net-class coverage (info) — ACTUAL project assignment
    fab_cfg = None
    if cfg.fab_config and cfg.fab_config.exists():
        fab_cfg = json.loads(cfg.fab_config.read_text())
    _, ncstats = netclass_coverage.analyze(sch, cfg, fab_cfg)
    msg = (f"net-class: {ncstats['n_assigned']}/{ncstats['n_total']} on a non-Default "
           f"class, {ncstats['n_default']} fall to Default")
    if fab_cfg and ncstats["plan_gap"]:
        msg += f"; {ncstats['plan_gap']} planned net(s) still unclassed"
    info.info(msg)

    print(hard.render())
    print()
    print(info.render())
    print()
    verdict = style.green("PASS") if hard.passed else style.red("FAIL")
    print(style.bold(f"== DFM preflight: {verdict} =="))
    return 0 if hard.passed else 1


def add_parser(sub):
    p = sub.add_parser("dfm-preflight", help="pre-layout/pre-fab gate (ERC, pins/pads, 3D, DRC)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
