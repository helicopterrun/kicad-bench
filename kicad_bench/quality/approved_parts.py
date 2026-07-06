"""approved-parts — MPN allow-list governance gate (read-only).

Checks every component's manufacturer part number in the schematic against an
`approved_parts.csv` allow-list. This is the startup-specific enforcement layer that
distinguishes a templated workflow from an ad-hoc one — ported from the
kicad-product-workflow `check_approved_parts.py` into a first-class kicad-bench gate
that emits a `Result` and composes into `commit-gate`/`audit`/CI.

Policy (from the `[approved_parts]` config section):
  * a component with **no MPN** -> error, unless `allow_missing_mpn = true` (then warn);
  * an MPN **not on the list** -> warn by default, or error if `unapproved = "error"`.

Reference designators that never carry an MPN — power/flag symbols and the
testpoint/fiducial/mounting-hole/mark families — are skipped. Approved MPNs include the
`alt_mpn_1`/`alt_mpn_2` second-source columns; `#`-comment rows in the CSV are ignored.

Read-only. Exit 0 if every checked part is compliant, 1 otherwise, 2 on setup error.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from ..core import config as cfgmod, schparse
from ..core.report import Result, render_and_exit

# Non-BOM designators that legitimately have no MPN.
SKIP_PREFIXES = ("#PWR", "#FLG", "TP", "FID", "H", "MK")


def load_approved(csv_path: Path) -> set[str]:
    """Approved MPNs (incl. `alt_mpn_1`/`alt_mpn_2`) from an approved_parts.csv.

    Blank rows and `#`-comment rows (mpn cell starting with `#`) are skipped, so the CSV
    can carry human-readable section dividers.
    """
    approved: set[str] = set()
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            mpn_cell = (row.get("mpn") or "").strip()
            if not mpn_cell or mpn_cell.startswith("#"):
                continue
            for key in ("mpn", "alt_mpn_1", "alt_mpn_2"):
                val = (row.get(key) or "").strip()
                if val and not val.startswith("#"):
                    approved.add(val)
    return approved


def check(components: list[dict], approved: set[str],
          allow_missing: bool, unapproved: str) -> Result:
    """Compare extracted components against the approved set. Pure logic — no I/O."""
    res = Result("Approved parts")
    checked = missing = not_listed = 0
    for c in components:
        ref = c["reference"]
        if ref.startswith(SKIP_PREFIXES):
            continue
        checked += 1
        mpn = c["mpn"]
        val = c["value"]
        if not mpn:
            missing += 1
            msg = f"{ref} ({val}) — no MPN"
            (res.warn if allow_missing else res.error)(msg, ref)
        elif mpn not in approved:
            not_listed += 1
            msg = f"{ref} ({val}) — MPN {mpn!r} not in approved list"
            (res.error if unapproved == "error" else res.warn)(msg, ref)

    if not res.findings:
        res.ok(f"all {checked} component(s) use approved MPNs")
    res.summary = (f"{checked} checked against {len(approved)} approved MPN(s); "
                   f"{missing} missing, {not_listed} unapproved")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    ap = cfg.approved_parts
    if ap is None:
        sys.exit('error: no [approved_parts] in config — add e.g.\n'
                 '  [approved_parts]\n  csv = "approved_parts.csv"')
    if not ap.csv.exists():
        sys.exit(f"error: approved-parts CSV not found: {ap.csv}")

    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch:
        sys.exit("error: no schematic given and no project.root_sch in config")
    if not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")

    comps = schparse.components(Path(sch))
    approved = load_approved(ap.csv)
    res = check(comps, approved, ap.allow_missing_mpn, ap.unapproved)
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("approved-parts",
                       help="MPN allow-list governance gate (schematic vs approved_parts.csv)")
    p.add_argument("schematic", nargs="?",
                   help="sheet to check (default: project.root_sch / active board)")
    p.add_argument("--board", help="which board to check (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
