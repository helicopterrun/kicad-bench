"""template.py — instantiate a KiCad-10 project from the kicad-templates repo.

Closes the gap `kb scaffold` deliberately leaves open: scaffold builds the product
tree but not the `.kicad_pro`/`.kicad_sch`/`.kicad_pcb` (KiCad's file format is
version-specific, so historically one manual save from the KiCad UI created them).
This copies a *version-matched* template out of the separately-versioned
`kicad-templates` repo into a board directory, renames the three files to the
board's basename, patches the internal name references, generates an `fp-lib-table`
for the template's bundled footprint libraries, and (best-effort) validates that the
result loads under `kicad-cli`.

Why an external, version-pinned templates repo instead of generating files inline:
KiCad bumps its on-disk format with major releases, so the templates carry a
`generator_version`. We match that against the installed `kicad-cli` and warn on a
mismatch — the same concern scaffold avoided, now handled explicitly. Re-save the
templates in the repo when KiCad majors bump.

The renaming is done with *anchored* edits (`(project "…"`, `(sheetfile "…"`, and the
structural JSON fields), never a blind basename replace — a template named `default`
would otherwise collide with `(type default)` netclass tokens.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .core import cli

#: Default starter template (2-layer OSHpark stackup + house drawing sheet/libs).
DEFAULT_TEMPLATE = "2L_OSHpark_Template"

_KICAD_FILES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")


# ── template discovery ───────────────────────────────────────────────────────
def templates_root(explicit: str | None = None) -> Path:
    """Where the template dirs live: explicit arg > $KICAD_TEMPLATES_DIR > ~/kicad-templates."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("KICAD_TEMPLATES_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / "kicad-templates").resolve()


def list_templates(root: Path | None = None) -> list[str]:
    """Names of instantiable template dirs (each holds exactly one `<name>.kicad_pro`)."""
    r = root or templates_root()
    if not r.is_dir():
        return []
    return [d.name for d in sorted(r.iterdir())
            if d.is_dir() and not d.name.startswith(".") and list(d.glob("*.kicad_pro"))]


def resolve_template(name: str, root: Path | None = None) -> Path:
    """Return the template dir for `name`, or raise ValueError with the available list."""
    r = root or templates_root()
    d = r / name
    if not d.is_dir() or not list(d.glob("*.kicad_pro")):
        avail = ", ".join(list_templates(r)) or "(none found)"
        raise ValueError(f"no such template {name!r} in {r} — available: {avail}")
    return d


def _template_basename(template_dir: Path) -> str:
    pros = list(template_dir.glob("*.kicad_pro"))
    if len(pros) != 1:
        raise ValueError(
            f"template {template_dir.name!r} must contain exactly one .kicad_pro "
            f"(found {len(pros)})")
    return pros[0].stem


# ── name-reference patching ──────────────────────────────────────────────────
def _patch_pro(text: str, old: str, board: str) -> str:
    """Rewrite the project-name references in a `.kicad_pro` (JSON), structurally."""
    d = json.loads(text)
    if isinstance(d.get("meta"), dict):
        d["meta"]["filename"] = f"{board}.kicad_pro"
    sch = d.get("schematic")
    if isinstance(sch, dict):
        for s in sch.get("top_level_sheets") or []:
            if isinstance(s, dict):
                if str(s.get("filename", "")).endswith(".kicad_sch"):
                    s["filename"] = f"{board}.kicad_sch"
                if s.get("name") == old:
                    s["name"] = board
    # `sheets` is [[uuid, display-name], …]; the root sheet name may even be a stale
    # copy-paste artifact from the source template — normalise it to the board.
    for entry in d.get("sheets") or []:
        if isinstance(entry, list) and len(entry) == 2:
            entry[1] = board
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def _patch_sch(text: str, old: str, board: str) -> str:
    return re.sub(r'\(project "' + re.escape(old) + r'"',
                  f'(project "{board}"', text)


def _patch_pcb(text: str, old: str, board: str) -> str:
    return re.sub(r'\(sheetfile "' + re.escape(old) + r'\.kicad_sch"\)',
                  f'(sheetfile "{board}.kicad_sch")', text)


# ── fp-lib-table ─────────────────────────────────────────────────────────────
def _write_fp_lib_table(dest_dir: Path) -> list[str]:
    """Register every `lib/**/*.pretty` under the board dir in a project `fp-lib-table`.

    Returns the registered library names. Uses `${KIPRJMOD}`-relative URIs so the
    project is relocatable. Skips writing if there are no footprint libs.
    """
    lib_root = dest_dir / "lib"
    pretties = sorted(p for p in lib_root.rglob("*.pretty") if p.is_dir()) if lib_root.is_dir() else []
    if not pretties:
        return []
    lines = ["(fp_lib_table", "\t(version 7)"]
    used: set[str] = set()
    names: list[str] = []
    for p in pretties:
        base = p.name[:-len(".pretty")]
        name, n = base, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        names.append(name)
        rel = p.relative_to(dest_dir).as_posix()
        lines.append(
            f'\t(lib (name "{name}")(type "KiCad")(uri "${{KIPRJMOD}}/{rel}")'
            f'(options "")(descr "Project {name}"))')
    lines.append(")\n")
    (dest_dir / "fp-lib-table").write_text("\n".join(lines))
    return names


# ── version guard + validation (best-effort) ─────────────────────────────────
def _major(v: str) -> str | None:
    m = re.search(r"(\d+)\.\d+", v)
    return m.group(1) if m else None


def _version_guard(template_dir: Path) -> str | None:
    """Warn if the template's KiCad generator major differs from the installed kicad-cli."""
    pcb = template_dir / f"{_template_basename(template_dir)}.kicad_pcb"
    try:
        head = pcb.read_text()[:2000]
    except OSError:
        return None
    m = re.search(r'\(generator_version "([^"]+)"', head)
    tmpl_v = m.group(1) if m else None
    if not (tmpl_v and cli.have_kicad_cli()):
        return None
    cli_v = cli.version()
    if _major(tmpl_v) and _major(cli_v) and _major(tmpl_v) != _major(cli_v):
        return (f"template built for KiCad {tmpl_v} but kicad-cli is {cli_v} — "
                f"re-save the templates repo for this KiCad major")
    return None


def _validate(dest_dir: Path, board: str, warnings: list[str]) -> bool | None:
    """Load the instantiated sch+pcb through kicad-cli to prove they parse. Non-fatal."""
    if not cli.have_kicad_cli():
        return None
    ok = True
    try:
        cli.netlist_xml(dest_dir / f"{board}.kicad_sch")   # raises on a sch that won't load
    except Exception as e:                                  # noqa: BLE001 — report, don't crash
        ok = False
        warnings.append(f"schematic did not validate: {e}")
    try:
        cli.drc_json(dest_dir / f"{board}.kicad_pcb")       # raises if the pcb won't load
    except Exception as e:                                  # noqa: BLE001
        ok = False
        warnings.append(f"pcb did not validate: {e}")
    return ok


# ── public entry point ───────────────────────────────────────────────────────
def instantiate_template(template_dir: Path | str, dest_dir: Path | str,
                         board_name: str, *, validate: bool = True) -> dict:
    """Instantiate a template into `dest_dir` as `<board_name>.kicad_{pro,sch,pcb}`.

    Copies + renames the three design files, patches their internal name references,
    copies the template's `lib/` footprint libraries, writes an `fp-lib-table`, checks
    the KiCad version, and (best-effort) validates the result loads. Returns a summary
    dict. Raises FileExistsError if a target design file already exists, ValueError on
    a malformed template.
    """
    template_dir = Path(template_dir)
    dest_dir = Path(dest_dir)
    old = _template_basename(template_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for ext in _KICAD_FILES:
        tgt = dest_dir / f"{board_name}{ext}"
        if tgt.exists():
            raise FileExistsError(tgt)

    result: dict = {"board": board_name, "template": template_dir.name,
                    "files": [], "footprint_libs": [], "warnings": [], "validated": None}

    patchers = {".kicad_pro": _patch_pro, ".kicad_sch": _patch_sch, ".kicad_pcb": _patch_pcb}
    for ext, patch in patchers.items():
        src = template_dir / f"{old}{ext}"
        (dest_dir / f"{board_name}{ext}").write_text(patch(src.read_text(), old, board_name))
        result["files"].append(f"{board_name}{ext}")

    src_lib = template_dir / "lib"
    if src_lib.is_dir():
        shutil.copytree(src_lib, dest_dir / "lib", dirs_exist_ok=True)
    result["footprint_libs"] = _write_fp_lib_table(dest_dir)

    warn = _version_guard(template_dir)
    if warn:
        result["warnings"].append(warn)
    if validate:
        result["validated"] = _validate(dest_dir, board_name, result["warnings"])
    return result
