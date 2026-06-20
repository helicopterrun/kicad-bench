"""block-review — independent review of one leaf sheet against project contracts.

Cheap, automated second pair of eyes for parallel-agent work. Checks:
  1. Net names — labels on the sheet that are CLOSE to a contract net name but not
     exact are flagged as probable typos (the silent `VIN_3V3` vs `VIN_33V` class
     of wiring bug). Exact matches and clearly-internal nets are fine.
  2. Designators — every reference on the sheet must fall in this block's reserved
     range (or the shared general-purpose range). The block is inferred from the
     sheet filename; override with --block.
  3. Footprints — every footprint string on the sheet resolves to a real
     .kicad_mod (reuses the project's resolver).

Read-only. Exits 0 if no errors, 1 otherwise. Net-name and footprint findings are
errors; out-of-range designators are errors too (hard reservation per the project
conventions).
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

from ..core import config as cfgmod, footprints, schparse
from ..core.report import Result, render_and_exit

# label kinds that name a net (local, global, hierarchical)
import re
_LABELS = re.compile(r'\((?:label|global_label|hierarchical_label)\s+"([^"]+)"')


def _sheet_labels(sch: Path) -> set[str]:
    return {m.lstrip("/") for m in _LABELS.findall(sch.read_text())}


def review(sch: Path, cfg: cfgmod.Config, block: str | None) -> Result:
    res = Result(f"Block review: {sch.name}")

    # 1. net-name typo check ------------------------------------------------
    contract = cfg.nets
    if contract:
        for label in sorted(_sheet_labels(sch)):
            if label in contract:
                continue
            close = difflib.get_close_matches(label, contract, n=1, cutoff=0.85)
            if close:
                res.error(f"net '{label}' looks like a typo of contract net '{close[0]}'",
                          label, detail="exact-match required — nets join by name at integration")
        # report nothing for clearly-internal labels (no close contract match)
    else:
        res.info("no net contract configured — skipping net-name check")

    # 2. designator range check --------------------------------------------
    block = block or sch.stem
    reserved = cfg.designators.get(block)
    general = cfg.designators.get("general", set())
    if reserved is None and not cfg.designators:
        res.info("no designator contract configured — skipping range check")
    elif reserved is None:
        res.warn(f"no designator range defined for block '{block}' — skipping range check",
                 detail=f"known blocks: {', '.join(sorted(cfg.designators))}")
    else:
        allowed = reserved | general
        for ref in sorted(set(schparse.references(sch))):
            if ref in allowed:
                continue
            owner = cfg.block_for_designator(ref)
            if owner and owner != block:
                res.error(f"designator {ref} is reserved for block '{owner}', not '{block}'", ref)
            elif owner is None:
                res.error(f"designator {ref} is outside every reserved range", ref,
                          detail=f"block '{block}' reserves: {_compact(reserved)}")

    # 3. footprint resolution ----------------------------------------------
    if cfg.fp_lib_table and cfg.fp_lib_table.exists():
        kiprjmod = cfg.fp_lib_table.parent
        fp_libs = schparse.parse_fp_lib_table(cfg.fp_lib_table, kiprjmod)
        system = footprints.system_footprint_dir()
        for fp, sheets in sorted(schparse.footprint_refs([sch]).items()):
            ok, msg = footprints.resolve(fp, fp_libs, system)
            if not ok:
                res.error(f"footprint '{fp}' — {msg}")
    else:
        res.info("no fp-lib-table configured — skipping footprint check")

    res.summary = f"{res.n_errors} issue(s) in {block}"
    return res


def _compact(refs: set[str]) -> str:
    return ", ".join(sorted(refs)[:8]) + (" ..." if len(refs) > 8 else "")


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = Path(args.schematic)
    if not sch.exists():
        sys.exit(f"error: schematic not found: {sch}")
    res = review(sch, cfg, args.block)
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("block-review", help="review one sheet: net names, designators, footprints")
    p.add_argument("schematic", help="the leaf sheet to review")
    p.add_argument("--block", help="block name (default: sheet filename stem)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
