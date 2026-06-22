"""dru-guard — catch the silent ".kicad_dru not loaded / out of date" trap.

KiCad applies custom design rules ONLY from a file named exactly `<board>.kicad_dru`
sitting next to the .kicad_pcb. Copy it under any other name, or let it drift from the
config it was generated from, and DRC runs *without* the impedance / per-domain /
cross-clearance rules and says nothing. `dru-lint` checks a DRU's syntax; this checks the
DRU that's actually INSTALLED is present, current, and consistent:

  1. the board-sibling `<board>.kicad_dru` exists                     (else: rules silently off)
  2. it matches a fresh `gen_design_rules.py` run on the current config (else: stale rules)
  3. no dead `<board>_design_rules.kicad_dru` sibling that doesn't load (cleanup)
  4. the `.kicad_pro` net classes still cover every config domain/diff-pair (sync drift)
  5. the stackup id is consistent between the toml and the config note  (doc drift)
  6. the installed DRU passes `dru-lint` (syntax — folded in)

Read-only. Exits 0 clean, 1 on a real problem (missing/stale/lint-failing rules), 2 on setup error.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, render_and_exit
from . import dru_lint, netclass_coverage as ncc

_STACKUP_ID = re.compile(r"JLC\d+H-\d+")   # full impedance-stackup id, e.g. JLC04161H-7628


def _normalize(text: str) -> list[str]:
    """Rule body with comments/blank lines stripped — for content comparison across
    the two comment styles ('#' vs ';') the generators have used."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        out.append(re.sub(r"\s+", " ", s))
    return out


def _regen(cfg: cfgmod.Config) -> str | None:
    """Run the project's gen_design_rules.py on the current config into a temp dir and
    return the generated .kicad_dru text (None if the generator can't be run)."""
    gen = cfg.root / "scripts" / "gen_design_rules.py"
    if not gen.exists() or not cfg.fab_config:
        return None
    fab = json.loads(cfg.fab_config.read_text())
    name = fab.get("circuit_name", "")
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, str(gen), "--config", str(cfg.fab_config),
                            "--out", td], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        cand = Path(td) / f"{name}_design_rules.kicad_dru"
        return cand.read_text() if cand.exists() else None


def analyze(cfg: cfgmod.Config) -> Result:
    res = Result("DRU install guard")
    if not cfg.pcb:
        res.error("project.pcb not set in config")
        return res

    sibling = cfg.pcb.with_suffix(".kicad_dru")           # the one KiCad loads
    if not sibling.exists():
        res.error(f"no board-sibling {sibling.name} — DRC runs WITHOUT custom rules",
                  detail="copy output/<circuit>_design_rules.kicad_dru to this exact name")
    else:
        res.ok(f"board-sibling rules present: {sibling.name}")
        # currency vs a fresh generation
        fresh = _regen(cfg)
        if fresh is None:
            res.info("could not regenerate rules to compare (gen_design_rules.py absent or failed)")
        elif _normalize(fresh) != _normalize(sibling.read_text()):
            inst, gen = set(_normalize(sibling.read_text())), set(_normalize(fresh))
            res.error(f"{sibling.name} is STALE vs the current config",
                      detail=f"+{len(gen - inst)} rule line(s) in fresh gen, "
                             f"-{len(inst - gen)} only in installed — rerun gen_design_rules.py")
        else:
            res.ok("installed rules are current with design_rules_config.json")
        # fold in syntax lint of the installed file
        lint = dru_lint.lint(sibling)
        for f in lint.findings:
            if f.severity in ("error", "warn"):
                res.add(f.severity, f"dru-lint: {f.message}", f.where, f.detail)

    # dead non-loading sibling copy
    dead = cfg.pcb.with_name(cfg.pcb.stem + "_design_rules.kicad_dru")
    if dead.exists():
        res.warn(f"dead rules file {dead.name} sits next to the board but never loads",
                 detail="KiCad only loads <board>.kicad_dru; delete this to avoid confusion")

    # net-class sync: project classes must cover config domains + diff pairs
    fab = json.loads(cfg.fab_config.read_text()) if cfg.fab_config and cfg.fab_config.exists() else {}
    settings = ncc.read_net_settings(cfg)
    proj_classes = {c.get("name") for c in settings.get("classes", [])}
    want = {d for d, nets in fab.get("nets", {}).items() if nets} | set(fab.get("diff_pairs", {}))
    missing = sorted(want - proj_classes)
    if missing:
        res.warn(f"net classes missing from the .kicad_pro: {', '.join(missing)}",
                 detail="run `kb netclass-sync --write` to add them")
    elif want:
        res.ok(f"project defines all {len(want)} config domain/diff-pair classes")

    # stackup-id drift between the toml and the config's own note
    ids = set()
    if cfg.stackup:
        ids.add(cfg.stackup)
    if cfg.fab_config and cfg.fab_config.exists():
        for key in ("_diff_pair_note", "_comment"):
            ids |= set(_STACKUP_ID.findall(str(fab.get(key, ""))))
    if len(ids) > 1:
        res.warn(f"stackup id drift: {', '.join(sorted(ids))} referenced in different places",
                 detail="reconcile toml fab.stackup with the design_rules_config.json note")

    res.summary = f"{res.n_errors} error(s)"
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    return render_and_exit(analyze(cfg))


def add_parser(sub):
    p = sub.add_parser("dru-guard",
                       help="verify the board's .kicad_dru is installed, current & consistent")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
