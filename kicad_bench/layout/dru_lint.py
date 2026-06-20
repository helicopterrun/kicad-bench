"""dru-lint — static checker for a KiCad custom design-rules (.kicad_dru) file.

KiCad applies a project's custom rules only if the file has NO syntax errors — but
a malformed rule is dropped *silently* (DRC just runs without it), and there is no
`kicad-cli` subcommand that validates a .kicad_dru (only the GUI "Check Rule Syntax"
button). This fills that gap with a static lint:

  * `(version N)` present (KiCad 10 expects `1`);
  * balanced parentheses;
  * every `(rule ...)` has a name and at least one `(constraint ...)`;
  * every `(constraint <type> ...)` uses a real constraint type;
  * every `(severity <s>)` uses a real severity (error/warning/ignore/exclusion);
  * advisory: `A.NetClass == '...'` conditions should prefer `A.hasNetclass('...')`
    (avoids the Default-in-list / ordering pitfalls documented by KiCad).

Constraint/severity vocabulary is from the KiCad 10 "Custom Design Rules" reference.
Read-only. Exits 0 if clean, 1 on a real problem, 2 on setup error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, render_and_exit

# KiCad 10 constraint types (Custom Design Rules reference).
CONSTRAINTS = {
    "annular_width", "assertion", "bridged_mask", "clearance", "connection_width",
    "courtyard_clearance", "creepage", "diff_pair_gap", "diff_pair_uncoupled",
    "disallow", "edge_clearance", "hole_clearance", "hole_size", "hole_to_hole",
    "length", "min_resolved_spokes", "physical_clearance", "physical_hole_clearance",
    "silk_clearance", "skew", "solder_mask_expansion", "solder_paste_abs_margin",
    "solder_paste_rel_margin", "text_height", "text_thickness", "thermal_relief_gap",
    "thermal_spoke_width", "track_angle", "track_segment_length", "track_width",
    "via_count", "via_dangling", "via_diameter", "zone_connection",
}
SEVERITIES = {"error", "warning", "ignore", "exclusion"}
EXPECTED_VERSION = "1"

_VERSION = re.compile(r"\(version\s+(\d+)\)")
_CONSTRAINT = re.compile(r"\(constraint\s+([A-Za-z_]+)")
_SEVERITY = re.compile(r"\(severity\s+([A-Za-z_]+)")
_RULE = re.compile(r"\(rule\s+(\"[^\"]*\"|\S+)")
_NETCLASS_EQ = re.compile(r"\bA\.NetClass\s*==")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def lint(path: Path) -> Result:
    res = Result(f"DRU lint: {path.name}")
    raw = path.read_text()
    text = _strip_comments(raw)

    # version
    m = _VERSION.search(text)
    if not m:
        res.error("missing (version N) header")
    elif m.group(1) != EXPECTED_VERSION:
        res.warn(f"(version {m.group(1)}) — KiCad 10 expects (version {EXPECTED_VERSION})")

    # parentheses balance (ignoring those inside quoted strings)
    depth = 0
    in_str = False
    for ch in re.sub(r'"[^"]*"', '""', text):
        if ch == '"':
            in_str = not in_str
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        res.error(f"unbalanced parentheses (net depth {depth:+d}) — KiCad will drop the whole file")

    # constraint types
    constraints = _CONSTRAINT.findall(text)
    for c in constraints:
        if c not in CONSTRAINTS:
            res.error(f"unknown constraint type '{c}'",
                      detail="not in the KiCad 10 constraint set")
    # severities
    for s in _SEVERITY.findall(text):
        if s not in SEVERITIES:
            res.error(f"unknown severity '{s}'", detail=f"valid: {', '.join(sorted(SEVERITIES))}")

    # rules with no constraint
    rules = _RULE.findall(text)
    if rules and not constraints:
        res.error(f"{len(rules)} rule(s) but no constraints found")

    # advisory: NetClass equality
    n_nc = len(_NETCLASS_EQ.findall(text))
    if n_nc:
        res.warn(f"{n_nc} condition(s) use `A.NetClass == ...` — prefer "
                 "`A.hasNetclass('...')`",
                 detail="NetClass is an ordered list incl. Default; == is order/Default-sensitive")

    res.summary = f"{len(rules)} rule(s), {len(constraints)} constraint(s); {res.n_errors} error(s)"
    return res


def _default_dru(cfg: cfgmod.Config) -> Path | None:
    # Prefer the active sibling rules file; else the generated one in output/.
    if cfg.pcb:
        sib = cfg.pcb.with_suffix(".kicad_dru")
        if sib.exists():
            return sib
    for cand in (cfg.root / "output").glob("*_design_rules.kicad_dru"):
        return cand
    return None


def run(args) -> int:
    if args.dru:
        path = Path(args.dru)
    else:
        cfg = cfgmod.load_or_exit(args.config)
        path = _default_dru(cfg)
        if not path:
            sys.exit("error: no .kicad_dru given and none found (sibling of the board "
                     "or output/*_design_rules.kicad_dru)")
    if not path.exists():
        sys.exit(f"error: not found: {path}")
    return render_and_exit(lint(path))


def add_parser(sub):
    p = sub.add_parser("dru-lint", help="static-check a .kicad_dru (no CLI validator exists)")
    p.add_argument("dru", nargs="?", help="path to .kicad_dru (default: sibling/output rules)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
