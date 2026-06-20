"""release-prep — gated fabrication export.

End-of-project helper. Refuses to produce fab outputs unless the design is
actually releasable:
  * ERC triage clean (no REAL errors),
  * PCB DRC clean (if the board is laid out),
  * BOM fully sourced — no `NEEDS-PN` placeholder left in the JLCPCB BOM.

With --export (and only if all gates pass and the board is laid out) it writes
gerbers + drill + position file + BOM into output/release/ via kicad-cli. Without
--export it just reports readiness.

Read-only unless --export (which writes only into the gitignored output/release/).
Exit 0 if releasable (or exported), 1 if not ready, 2 on setup error.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ..core import cli, config as cfgmod, schparse
from ..core.report import Result, style
from ..quality import erc_triage


def _bom_unsourced(bom: Path) -> list[str]:
    """Return BOM rows still carrying a NEEDS-PN placeholder."""
    try:
        import openpyxl
    except ImportError:
        return ["(openpyxl not installed — cannot check BOM sourcing)"]
    wb = openpyxl.load_workbook(bom, read_only=True, data_only=True)
    hits = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if any("NEEDS-PN" in c for c in cells):
                hits.append(" | ".join(cells[:4]))
    return hits


def readiness(cfg: cfgmod.Config) -> tuple[bool, Result, bool]:
    res = Result("Release readiness")
    sch = cfg.root_sch
    if not sch or not sch.exists():
        sys.exit("error: project.root_sch missing or not found")

    # ERC
    _, pin_net = cli.netlist_nets(sch)
    in_nets = schparse.input_hier_nets(sch)
    erc = erc_triage.triage(sch, pin_net, in_nets, cfg.allow_rules)
    if erc.n_errors:
        res.error(f"ERC: {erc.n_errors} real error(s)")
    else:
        res.ok("ERC clean")

    # DRC / layout state
    laid_out = bool(cfg.pcb and cfg.pcb.exists()
                    and "(footprint " in cfg.pcb.read_text(errors="ignore"))
    if not laid_out:
        res.warn("PCB not laid out yet — gerber export will be skipped")
    else:
        report = cli.drc_report(cfg.pcb)
        n = len(re.findall(r"^\[", report, re.M))
        if n:
            res.error(f"PCB DRC: {n} violation(s)")
        else:
            res.ok("PCB DRC clean")

    # BOM
    if cfg.bom and cfg.bom.exists():
        unsourced = _bom_unsourced(cfg.bom)
        if unsourced:
            res.error(f"BOM: {len(unsourced)} row(s) still NEEDS-PN",
                      detail="; ".join(unsourced[:5]) + (" ..." if len(unsourced) > 5 else ""))
        else:
            res.ok("BOM fully sourced (no NEEDS-PN)")
    else:
        res.warn("no BOM configured — sourcing not checked")

    ready = res.passed and laid_out
    res.summary = ("releasable" if ready else
                   "NOT ready" + ("" if laid_out else " (no layout)"))
    return ready, res, laid_out


def _export(cfg: cfgmod.Config) -> int:
    out = cfg.root / "output" / "release"
    out.mkdir(parents=True, exist_ok=True)
    pcb = str(cfg.pcb)
    jobs = [
        (["kicad-cli", "pcb", "export", "gerbers", "--output", str(out), pcb], "gerbers"),
        (["kicad-cli", "pcb", "export", "drill", "--output", str(out) + "/", pcb], "drill"),
        (["kicad-cli", "pcb", "export", "pos", "--output", str(out / 'positions.csv'),
          "--format", "csv", pcb], "position file"),
    ]
    for cmd, label in jobs:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(style.red(f"  ✗ {label} export failed: {r.stderr.strip()}"))
            return 1
        print(style.green(f"  ✓ {label}"))
    print(style.dim(f"  release artifacts in {out}"))
    return 0


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    try:
        ready, res, laid_out = readiness(cfg)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    print(res.render())
    if not args.export:
        return 0 if res.passed else 1
    if not ready:
        print(style.red("== not releasable — export refused =="))
        return 1
    print(style.bold("== exporting fab outputs =="))
    return _export(cfg)


def add_parser(sub):
    p = sub.add_parser("release-prep", help="gated fab export (ERC/DRC/BOM guardrails)")
    p.add_argument("--export", action="store_true", help="write gerbers/drill/pos if releasable")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
