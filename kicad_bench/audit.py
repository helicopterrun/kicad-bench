"""audit — the consolidated, whole-repo audit.

One command that runs every read-only check kicad-bench has — schematic, netlist,
per-sheet review, net-class, design-rules, and the routed-geometry/DFM audits — sharing
the two expensive operations (one netlist export, one DRC run) instead of each tool
re-doing them. It prints a per-check roster, then a consolidated finding summary grouped
by source so you see the whole repo's conformance state at a glance.

Read-only. Exit 0 if there are no error-severity findings, 1 if any, 2 on setup error.
`--full` prints every finding (default: section roster + the error-severity detail).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .core import cli, config as cfgmod, pcbgeom, schparse
from .core.report import Result, style
from .core import graph as graphmod
from .quality import (block_review, cap_derating, commit_gate, erc_triage,
                      ksir_sync, led_current, netlist_audit, pin_mux)
from .layout import (diffpair_audit, dru_guard, dru_lint, netclass_coverage,
                     route_coverage, stackup_sync, track_conformance)


def _drc_summary(drc: dict) -> Result:
    """Collapse the (often hundreds of) DRC items into one line per violation type."""
    res = Result("DFM / DRC")
    by_type: dict[str, list[int]] = {}      # type -> [errors, warnings]
    for v in drc.get("violations", []):
        e = by_type.setdefault(v.get("type", "?"), [0, 0])
        e[0 if v.get("severity") == "error" else 1] += 1
    n_err = sum(e for e, _ in by_type.values())
    n_warn = sum(w for _, w in by_type.values())
    for t in sorted(by_type, key=lambda k: (-by_type[k][0], -by_type[k][1])):
        e, w = by_type[t]
        if e:
            res.error(f"{t}: {e} error(s)" + (f" + {w} warning(s)" if w else ""))
        else:
            res.warn(f"{t}: {w} warning(s)")
    unconn = len(drc.get("unconnected_items", []))
    if unconn:
        res.info(f"{unconn} unconnected (ratsnest) item(s) — unrouted connections")
    res.summary = f"{n_err} error(s), {n_warn} warning(s), {unconn} unrouted"
    return res


def run_all(cfg: cfgmod.Config) -> list[tuple[str, Result]]:
    """Run every check once; return [(section, Result), ...] in report order."""
    out: list[tuple[str, Result]] = []
    sch = cfg.root_sch
    if not sch or not sch.exists():
        sys.exit("error: project.root_sch missing or not found")
    fab = json.loads(cfg.fab_config.read_text()) if cfg.fab_config and cfg.fab_config.exists() else {}

    # ---- Schematic (shares one netlist export for ERC pin map) ----
    counts, pin_net = cli.netlist_nets(sch)
    in_nets = schparse.input_hier_nets(sch)
    out.append(("Schematic", commit_gate._footprint_result(cfg)))
    out.append(("Schematic", erc_triage.triage(sch, pin_net, in_nets, cfg.allow_rules)))
    if cfg.expected_nets:
        r = netlist_audit.audit(sch, cfg.expected_nets)
        r.title += "  (vs recorded baseline — mismatches may be branch drift)"
        out.append(("Schematic", r))
    proj_dir = sch.parent
    for sheet in sorted(proj_dir.glob("*.kicad_sch")):
        if sheet == sch:
            continue
        out.append(("Schematic", block_review.review(sheet, cfg, None)))
    if any(proj_dir.glob("*.ksir")):
        out.append(("Schematic", ksir_sync.sync_check(proj_dir)))

    # ---- Datasheet checks (share the same netlist export via the design graph) ----
    comps: list[dict] = []
    seen: set[str] = set()
    for sheet in sorted(proj_dir.glob("*.kicad_sch")):
        for c in schparse.components(sheet):
            if c["reference"] not in seen:
                seen.add(c["reference"])
                comps.append(c)
    g = graphmod.build_from(counts, pin_net, comps)
    from .core import conspec
    lookup = conspec.lookup(cfg)
    graphmod.solve_rail_voltages(g, lookup)
    out.append(("Datasheet checks",
                cap_derating.check(g, cap_derating.factors_from_cfg(cfg), lookup)))
    out.append(("Datasheet checks", led_current.check(g, lookup=lookup)))
    out.append(("Datasheet checks", pin_mux.check(g, pin_mux.tables_from_cfg(cfg, g))))

    # ---- Rules & config ----
    out.append(("Rules & config", netclass_coverage.analyze(sch, cfg, fab or None)[0]))
    if cfg.pcb:
        sib = cfg.pcb.with_suffix(".kicad_dru")
        if sib.exists():
            out.append(("Rules & config", dru_lint.lint(sib)))
    out.append(("Rules & config", dru_guard.analyze(cfg)))
    out.append(("Rules & config", stackup_sync.build(cfg)[0]))

    # ---- Layout & DFM (shares one DRC run) ----
    if cfg.pcb and cfg.pcb.exists():
        board = pcbgeom.parse(cfg.pcb)
        out.append(("Layout & DFM", diffpair_audit.analyze(board, fab, 0.15, 0.02, 5.0)))
        out.append(("Layout & DFM", track_conformance.analyze(board, fab, 0.2)))
        try:
            drc = cli.drc_json(cfg.pcb)
            out.append(("Layout & DFM", _drc_summary(drc)))
            rats = route_coverage.ratsnest_from_drc(drc)
            out.append(("Layout & DFM", route_coverage.analyze(board, cfg, rats)[0]))
        except cli.KicadCliError as e:
            r = Result("DFM / DRC")
            r.info(f"DRC skipped: {e}")
            out.append(("Layout & DFM", r))
    return out


def _verdict_glyph(r: Result) -> str:
    if r.n_errors:
        return style.red("✗")
    if any(f.severity == "warn" for f in r.findings):
        return style.yellow("!")
    return style.green("✓")


def render(sections: list[tuple[str, Result]], full: bool) -> str:
    lines = [style.bold("==== Repo audit ====")]
    cur = None
    for sec, r in sections:
        if sec != cur:
            lines.append(style.bold(f"\n[{sec}]"))
            cur = sec
        tail = r.summary or f"{r.n_errors} error(s)"
        lines.append(f"  {_verdict_glyph(r)} {r.title:46s} {style.dim(tail)}")
        if full:
            for f in r.findings:
                if f.severity in ("error", "warn"):
                    g = "✗" if f.severity == "error" else "!"
                    lines.append(style.dim(f"        {g} {f.message}"))

    # consolidated, error-severity detail grouped by check
    err_checks = [(sec, r) for sec, r in sections if r.n_errors]
    warn_checks = [(sec, r) for sec, r in sections
                   if not r.n_errors and any(f.severity == "warn" for f in r.findings)]
    lines.append(style.bold(f"\n==== Errors ({len(err_checks)} check(s)) ===="))
    if not err_checks:
        lines.append(style.green("  none"))
    for _, r in err_checks:
        errs = [f for f in r.findings if f.severity == "error"]
        lines.append(f"  {style.red(r.title)} — {len(errs)}")
        for f in errs[:6]:
            where = style.dim(f" [{f.where}]") if f.where else ""
            lines.append(f"      {f.message}{where}")
        if len(errs) > 6:
            lines.append(style.dim(f"      … and {len(errs) - 6} more"))

    verdict = style.green("PASS") if not err_checks else style.red("FAIL")
    lines.append(style.bold(
        f"\n==== {verdict}  {len(err_checks)} check(s) with errors, "
        f"{len(warn_checks)} with warnings, {len(sections)} total ===="))
    return "\n".join(lines)


def sections_to_dict(sections: list[tuple[str, Result]], cfg: cfgmod.Config) -> dict:
    """JSON-able view of an audit run — consumed by the web sidecar."""
    by_sec: dict[str, list] = {}
    order: list[str] = []
    for sec, r in sections:
        if sec not in by_sec:
            by_sec[sec] = []
            order.append(sec)
        glyph = ("error" if r.n_errors
                 else "warn" if any(f.severity == "warn" for f in r.findings) else "ok")
        by_sec[sec].append({
            "title": r.title,
            "summary": r.summary or f"{r.n_errors} error(s)",
            "glyph": glyph,
            "n_errors": r.n_errors,
            "findings": [{"severity": f.severity, "message": f.message,
                          "where": f.where, "detail": f.detail}
                         for f in r.findings if f.severity in ("error", "warn", "info")],
        })
    err = sum(1 for _, r in sections if r.n_errors)
    warn = sum(1 for _, r in sections
               if not r.n_errors and any(f.severity == "warn" for f in r.findings))
    pcb = cfg.pcb
    return {
        "board": pcb.name if pcb else "",
        "board_mtime": pcb.stat().st_mtime if pcb and pcb.exists() else 0,
        "verdict": {"err_checks": err, "warn_checks": warn,
                    "clean": len(sections) - err - warn, "total": len(sections)},
        "sections": [{"section": s, "checks": by_sec[s]} for s in order],
    }


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    sections = run_all(cfg)
    print(render(sections, args.full))
    return 1 if any(r.n_errors for _, r in sections) else 0


def add_parser(sub):
    p = sub.add_parser("audit", help="consolidated whole-repo audit (all read-only checks)")
    p.add_argument("--full", action="store_true", help="print every error/warn finding, not just the roster")
    p.add_argument("--board", help="which board to audit (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
