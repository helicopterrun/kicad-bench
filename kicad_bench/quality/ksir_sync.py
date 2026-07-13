"""ksir-sync — detect drift between adopted .ksir sources and their sheets.

A sheet with a `<name>.ksir` beside it is ksir-managed: the YAML is the edit
surface and the .kicad_sch is compiled output. If someone hand-edits the sheet
in eeschema and forgets `ksir adopt --force`, the two silently diverge and the
next compile would clobber the hand edit. This gate recompiles each .ksir into
a temp dir and canonical-netlist-diffs it against the sheet on disk — any delta
is drift and fails.

Read-only on the design: compilation happens entirely in a temp directory.
Requires the `ksir` package; without it the check reports SKIP (info) so
kicad-bench keeps working on machines that don't have ksir installed.

Exit codes: 0 in sync, 1 drift, 2 setup error.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, render_and_exit


def sync_check(sheet_dir: Path) -> Result:
    res = Result(f"ksir sync: {sheet_dir.name}")
    ksirs = sorted(sheet_dir.glob("*.ksir"))
    if not ksirs:
        res.summary = "no .ksir files (nothing adopted — check skipped)"
        return res
    try:
        from ksir import compile as kcompile
        from ksir import model, netdiff
    except ImportError:
        res.info("ksir package not installed — sync not checked")
        res.summary = f"{len(ksirs)} .ksir file(s) present but ksir unavailable (SKIP)"
        return res

    for kf in ksirs:
        sch = kf.with_suffix(".kicad_sch")
        if not sch.exists():
            res.error(f"{kf.name}: no matching {sch.name}", kf.name)
            continue
        try:
            sh = model.load(kf)
            with tempfile.TemporaryDirectory() as td:
                trial = Path(td) / sch.name
                kcompile.compile_sheet(sh, trial)
                lines = netdiff.diff(netdiff.netlist(sch), netdiff.netlist(trial))
            if lines:
                res.error(f"{sch.name}: drift vs {kf.name} "
                          f"(hand edit not re-adopted, or stale .ksir)",
                          sch.name, detail="\n".join(lines))
            else:
                res.ok(f"{sch.name}: in sync", sch.name)
        except Exception as e:                      # noqa: BLE001 — report, don't crash the audit
            res.error(f"{kf.name}: recompile failed: {e}", kf.name)

    res.summary = (f"{res.n_errors} drifted of {len(ksirs)} adopted sheet(s)"
                   if res.n_errors else f"all {len(ksirs)} adopted sheet(s) in sync")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if cfg.root_sch is None:
        sys.exit("error: no project.root_sch in config")
    res = sync_check(Path(cfg.root_sch).parent)
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("ksir-sync",
                       help="fail if any adopted .ksir drifted from its .kicad_sch")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
