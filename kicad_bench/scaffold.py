"""scaffold.py — `kb scaffold <product> [boards...]`: create a new product monorepo.

Priority-0 lifecycle command. Scaffolds the product-monorepo layout that the merged
kicad-product-workflow defines — `hardware/<board>/`, `firmware/`, `mechanical/`,
`releases/`, `docs/`, `tools/` — seeds compliance-aware doc stubs, a KiCad-aware
`.gitignore`, and (the upgrade over the old bash scaffolder) **wires kicad-bench in**:
a product-aware `kicad-bench.toml` and a seed `approved_parts.csv`, so `kb audit` /
`kb approved-parts` work the moment the boards are saved.

Read-first boundary: this creates a brand-new tree of NEW files and refuses to touch an
existing directory. Like the old workflow, it deliberately does **not** generate
`.kicad_pro` / `.kicad_sch` / `.kicad_pcb` — KiCad's file format is version-dependent, so
one manual save from the KiCad UI creates those. No existing design file is ever written.

Exit 0 on success, 2 on a bad name / existing target.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Lowercase kebab-case, same rule as the original new-kicad-product.sh.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Packaged templates (CI workflow, …) copied into scaffolded products.
_TEMPLATES = Path(__file__).parent / "templates"


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


# ── seed-file bodies ─────────────────────────────────────────────────────────
def _readme(product: str, boards: list[str]) -> str:
    board_list = "\n".join(f"- `{b}`" for b in boards)
    return f"""# {product}

Brief description of the product.

## Structure

- `hardware/` — KiCad projects, one per board
- `firmware/` — embedded firmware
- `mechanical/` — enclosure CAD, STEP exports
- `docs/` — architecture, requirements, datasheets
- `releases/` — frozen release snapshots (gerbers, BOM, assembly) — committed
- `tools/` — project-specific scripts

## Boards

{board_list}

## Workflow

1. Open `hardware/<board>/<board>.kicad_pro` in KiCad and save the schematic/PCB.
2. Gate before every commit: `kb audit` (and `kb approved-parts`).
3. Freeze a release with `kb release-freeze <version>` (once available).
"""


_CHANGELOG = """# Changelog

All notable changes to this product. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Initial project scaffold
"""


def _architecture(product: str, boards: list[str]) -> str:
    sections = "\n".join(
        f"\n### {b}\n\nPurpose: TODO" for b in boards)
    return f"""# {product} — Architecture

## System Overview

Block diagram and high-level description of how the boards and firmware interact.

## Boards
{sections}

## Interfaces

Inter-board connectors, protocols, and pinouts.
"""


_REQUIREMENTS = """# Requirements

## Functional
- TODO

## Electrical
- Input voltage:
- Power budget:
- Operating temperature:

## Mechanical
- Enclosure constraints:
- Mounting:

## Compliance
- FCC Part 15:
- IEC 61010 (if applicable):
- Safety / ESD:
"""


def _board_readme(board: str) -> str:
    return f"""# {board}

## Revision
Current: A (unreleased)

## Stackup
TODO — 2-layer / 4-layer controlled impedance / etc.

## Fab
- House:
- Min trace/space:
- Min via / drill:
- Surface finish:
- Panelization:

## Errata
None yet.
"""


_GITIGNORE = """# --- KiCad: regeneratable artifacts ---
hardware/*/output/
hardware/*/*-backups/
hardware/*/fp-info-cache
hardware/*/*.kicad_prl

# --- KiCad: lock / autosave ---
**/~*.lck
**/_autosave-*
**/*.bak

# --- firmware build artifacts ---
firmware/**/build/
firmware/**/.pio/
firmware/**/.pioenvs/
firmware/**/cmake-build-*/

# --- macOS ---
.DS_Store
._*

# --- editors ---
.vscode/
.idea/
*.swp
*~

# --- release staging is scratchpad; tagged releases are committed ---
releases/staging/
"""


_APPROVED_PARTS_CSV = """mpn,manufacturer,alt_mpn_1,alt_mpn_2,lifecycle_status,preferred_distributor,min_qty_price,package,value,function,notes
# -----------------------------------------------------------------------------
# approved_parts.csv — MPN allow-list for this product.
# `kb approved-parts` checks every schematic MPN against this list. Rows whose mpn
# starts with # are comments; alt_mpn_1/2 are pin-compatible second sources.
# -----------------------------------------------------------------------------
# --- example rows — replace with your qualified parts ---
CL10B104KB8NNNC,Samsung,GRM188R71H104KA93D,,active,digikey,0.005,0603,100nF,decoupling,default MLCC X7R
AP2112K-3.3TRG1,Diodes Inc,AP2112K-3.3,,active,digikey,0.32,SOT-25,3.3V 600mA,ldo,default 3V3 rail
"""


def _kicad_bench_toml(product: str, boards: list[str]) -> str:
    lines = [
        "[product]",
        f'name = "{product}"',
        'firmware = "firmware"',
        'mechanical = "mechanical"',
        "",
    ]
    for b in boards:
        base = f"hardware/{b}/{b}"
        lines += [
            "[[boards]]",
            f'name         = "{b}"',
            f'root_sch     = "{base}.kicad_sch"',
            f'pcb          = "{base}.kicad_pcb"',
            f'fp_lib_table = "hardware/{b}/fp-lib-table"',
            "",
        ]
    lines += [
        "[approved_parts]",
        'csv        = "approved_parts.csv"',
        'unapproved = "warn"   # "error" once the list is comprehensive',
        "",
    ]
    return "\n".join(lines)


# ── tree creation (pure; no git) ─────────────────────────────────────────────
def scaffold_product(parent: Path, product: str, boards: list[str]) -> Path:
    """Create `<parent>/<product>/…` with the full monorepo tree + seed files.

    Returns the product root. Raises FileExistsError if the target already exists.
    Creates NEW files only — never a `.kicad_pro`/`.kicad_sch`/`.kicad_pcb`.
    """
    root = parent / product
    if root.exists():
        raise FileExistsError(root)

    for d in ("docs/datasheets", "mechanical", "firmware", "releases", "tools"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / ".gitkeep").touch()
    for b in boards:
        for sub in ("lib", "output"):
            (root / "hardware" / b / sub).mkdir(parents=True, exist_ok=True)
            (root / "hardware" / b / sub / ".gitkeep").touch()

    (root / "README.md").write_text(_readme(product, boards))
    (root / "CHANGELOG.md").write_text(_CHANGELOG)
    (root / "docs" / "architecture.md").write_text(_architecture(product, boards))
    (root / "docs" / "requirements.md").write_text(_REQUIREMENTS)
    (root / ".gitignore").write_text(_GITIGNORE)
    (root / "kicad-bench.toml").write_text(_kicad_bench_toml(product, boards))
    (root / "approved_parts.csv").write_text(_APPROVED_PARTS_CSV)
    for b in boards:
        (root / "hardware" / b / "README.md").write_text(_board_readme(b))

    # CI: run the kb gates per board on push/PR, freeze on a v* tag.
    ci = _TEMPLATES / "hardware-ci.yml"
    if ci.exists():
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "hardware-ci.yml").write_text(ci.read_text())

    return root


def _git_init(root: Path) -> str:
    """Init a git repo and make the initial commit if a user identity is configured.
    Returns a short status line. Never fatal — scaffolding already succeeded."""
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True,
                       capture_output=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        have_id = (subprocess.run(["git", "config", "user.email"], cwd=root,
                                  capture_output=True).returncode == 0
                   and subprocess.run(["git", "config", "user.name"], cwd=root,
                                     capture_output=True).returncode == 0)
        if have_id:
            subprocess.run(["git", "commit", "-q", "-m", f"Initial scaffold for {root.name}"],
                           cwd=root, check=True, capture_output=True)
            return "✓ git repo initialized and initial commit made"
        return "✓ git repo initialized (staged; no commit — git user.name/email unset)"
    except (OSError, subprocess.CalledProcessError):
        return "⚠ git not available — skipped repo init"


def run(args) -> int:
    if not valid_name(args.product):
        sys.exit(f"error: product name must be lowercase kebab-case: {args.product!r}")
    boards = args.boards or ["main-board"]
    for b in boards:
        if not valid_name(b):
            sys.exit(f"error: board name must be lowercase kebab-case: {b!r}")
    if len(set(boards)) != len(boards):
        sys.exit("error: duplicate board names")

    parent = Path(args.dir).expanduser()
    if not parent.is_dir():
        sys.exit(f"error: parent dir does not exist: {parent}")

    try:
        root = scaffold_product(parent, args.product, boards)
    except FileExistsError as e:
        sys.exit(f"error: {e} already exists")

    git_line = "· git init skipped (--no-git)" if args.no_git else _git_init(root)

    print(f"✓ created {root}")
    print(f"  boards: {', '.join(boards)}")
    print(f"  {git_line}")
    print("\nNext steps:")
    print(f"  cd {root}")
    print("  # In KiCad: File ▸ New Project, save each board as:")
    for b in boards:
        print(f"  #   hardware/{b}/{b}.kicad_pro")
    print("  # then:  kb audit   (and  kb approved-parts)")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "scaffold",
        help="scaffold a new KiCad product monorepo (tree + docs + kicad-bench.toml)")
    p.add_argument("product", help="product name (lowercase kebab-case)")
    p.add_argument("boards", nargs="*",
                   help="board names (default: main-board)")
    p.add_argument("-d", "--dir", default=".",
                   help="parent directory to create the product in (default: cwd)")
    p.add_argument("--no-git", action="store_true", help="skip git init/commit")
    p.set_defaults(func=run)
