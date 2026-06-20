"""commit-gate — the composite pre-commit discipline gate.

Runs, in order, and aggregates into one verdict:
  1. footprint resolution across all project sheets
  2. ERC triage on the root schematic (REAL errors only — documented
     false-positives live in `[ [erc.allow] ]`, so any REAL error here is, by
     definition, an *undocumented* regression)
  3. netlist node-count audit (if expected_nets is configured)

Designed to be wired as a git pre-commit hook so "run ERC + netlist before commit"
stops being a remembered ritual. `--install-hook` writes a hook into the project's
.git/hooks that calls this command.

Read-only with respect to design files. `--install-hook` writes one file into the
project's .git/hooks. Exits 0 (allow commit) / 1 (block commit) / 2 (setup error).
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

from ..core import cli, config as cfgmod, footprints, schparse
from ..core.report import Result, render_and_exit, style
from . import erc_triage, netlist_audit


def _footprint_result(cfg: cfgmod.Config) -> Result:
    res = Result("Footprints")
    if not (cfg.fp_lib_table and cfg.fp_lib_table.exists()):
        res.info("no fp-lib-table configured — skipped")
        return res
    proj_dir = cfg.fp_lib_table.parent
    sheets = sorted(proj_dir.glob("*.kicad_sch"))
    fp_libs = schparse.parse_fp_lib_table(cfg.fp_lib_table, proj_dir)
    system = footprints.system_footprint_dir()
    refs = schparse.footprint_refs(sheets)
    for fp, where in sorted(refs.items()):
        ok, msg = footprints.resolve(fp, fp_libs, system)
        if not ok:
            res.error(f"'{fp}' — {msg}", detail="used in: " + ", ".join(sorted(where)))
    res.summary = (f"{res.n_errors} unresolved of {len(refs)} unique "
                   f"across {len(sheets)} sheet(s)")
    return res


def gate(cfg: cfgmod.Config) -> tuple[bool, list[Result]]:
    if not cfg.root_sch or not cfg.root_sch.exists():
        sys.exit("error: project.root_sch missing or not found — required for commit-gate")
    sch = cfg.root_sch
    results: list[Result] = []

    results.append(_footprint_result(cfg))

    _, pin_net = cli.netlist_nets(sch)
    in_nets = schparse.input_hier_nets(sch)
    results.append(erc_triage.triage(sch, pin_net, in_nets, cfg.allow_rules))

    if cfg.expected_nets:
        results.append(netlist_audit.audit(sch, cfg.expected_nets))

    ok = all(r.passed for r in results)
    return ok, results


def install_hook(cfg: cfgmod.Config) -> int:
    git_dir = cfg.root / ".git"
    if not git_dir.is_dir():
        sys.exit(f"error: {cfg.root} is not a git repository (no .git dir)")
    hook = git_dir / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/bin/sh\n"
        "# Installed by `kb commit-gate --install-hook`.\n"
        f'exec kb commit-gate --config "{cfg.path}"\n'
    )
    hook.write_text(body)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"✓ installed pre-commit hook -> {hook}")
    return 0


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if args.install_hook:
        return install_hook(cfg)
    try:
        ok, results = gate(cfg)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    for r in results:
        print(r.render())
        print()
    verdict = style.green("COMMIT ALLOWED") if ok else style.red("COMMIT BLOCKED")
    print(style.bold(f"== commit-gate: {verdict} =="))
    if not ok:
        print(style.dim("  fix the errors above, or document a known false-positive "
                        "in [[erc.allow]] with a reason."))
    return 0 if ok else 1


def add_parser(sub):
    p = sub.add_parser("commit-gate", help="footprints + ERC triage + netlist audit, as a commit gate")
    p.add_argument("--install-hook", action="store_true",
                   help="write a git pre-commit hook into the project and exit")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
