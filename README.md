# kicad-bench

A generic, config-driven **KiCad-10 workflow & PCB-design workbench**: read-first
quality gates and layout/DFM prep that wrap the noisy, manual, error-prone steps of
schematic→BOM→layout with guardrails. It complements design-*generation* tooling
(sheet codegen, BOM pickers) — it does **not** edit schematic design content.

Built originally for the Example Board project but kept project-agnostic: every
project specific lives in one `kicad-bench.toml`, so the same tools work on the next
board.

## The read/write boundary

- **Priority-1 (quality)** tools are strictly read-and-report. Nothing edits a
  design file.
- **Priority-2 (layout)** tools report by default. The only writes they ever make
  are into the regenerable `output/` dir (`stackup-sync --emit`, `release-prep
  --export`). They **never** write `.kicad_sch` or `.kicad_pcb`. Stackup/board-setup
  changes are *emitted for you to apply* in KiCad.
- The one exception is `commit-gate --install-hook`, which writes a single git
  pre-commit hook into the project's `.git/hooks`.

## Install

```sh
pip install -e /root/kicad-bench --break-system-packages   # exposes the `kb` CLI
kb doctor --config /root/kicad-bench/configs/example-board.toml
```

Requires KiCad 10 `kicad-cli` on PATH. Core is stdlib-only; `release-prep`'s BOM
check uses `openpyxl` if present.

## Commands

| Command | Phase | What it does |
|---|---|---|
| `kb erc-triage [sheet]` | quality | Run ERC, classify each violation REAL vs. ALLOWED (partial-build artifacts + documented `[[erc.allow]]` false-positives). |
| `kb netlist-audit [sheet]` | quality | Per-net node-count audit vs. an expected baseline — catches nets silently merged into GND (the L-stub bug). |
| `kb block-review <sheet>` | quality | Independent block check: net-name typos vs. contract, designator-range violations, footprint-string resolution. |
| `kb commit-gate` | quality | Composite gate (footprints → erc-triage → netlist-audit); `--install-hook` wires it as a git pre-commit hook. |
| `kb netclass-coverage` | layout | Report nets with no explicit net class (they fall to Default). |
| `kb dfm-preflight` | layout | Pre-layout/pre-fab gate: ERC + pins-vs-pads + 3D models + PCB DRC. |
| `kb stackup-sync` | layout | Reconcile the chosen fab stackup/constraints with the project; `--emit` writes a board-setup guide. Emit-and-apply. |
| `kb release-prep` | layout | Gated fab export (ERC=0 / DRC=0 / BOM fully sourced); `--export` writes gerbers/drill/pos. |
| `kb doctor` | — | Check kicad-cli + config resolution. |

All commands take `--config PATH`; without it, they search upward from the cwd for
`kicad-bench.toml`. Exit codes: `0` pass, `1` fail, `2` setup error.

## Configuration (`kicad-bench.toml`)

One file holds all project specifics. A config kept in this repo can point at a
separate design repo via `project.root` (see `configs/example-board.toml`).

```toml
[project]
root         = "/path/to/design-repo"      # optional; paths below resolve under it
root_sch     = "Proj/Proj.kicad_sch"
pcb          = "Proj/Proj.kicad_pcb"
fp_lib_table = "Proj/fp-lib-table"
bom          = "BOM.xlsx"

[contracts]
nets = "from:scripts/design_rules_config.json"   # inline list, or from a JSON/markdown source
# expected_nets = "expected_nets.json"           # {net: node_count} baseline for netlist-audit

[contracts.designators]                          # ranges expand inclusively
buck = ["U4", "R22-R29", "L3"]
general = ["R1-R12", "C1-C32"]                    # shared, allowed on any sheet

[[erc.allow]]                                     # known false-positives — reason REQUIRED
type = "power_pin_not_driven"
ref = "U5"
pin = "7"
reason = "power:GND pin ~0.47mm off anchor; visually connected."

[fab]
config  = "scripts/design_rules_config.json"
stackup = "JLC04161H-7628"
```

## Relationship to the design repo's `scripts/`

kicad-bench *generalizes and consolidates* the per-project validators
(`validate_sheet.py`, `validate_footprints.py`, `preflight_layout.py`,
`gen_design_rules.py`) into reusable, config-driven tools sharing one core
(`kicad_bench/core/`). The originals remain the source-of-truth references; the
classification and resolution logic here is ported to match them.

## Skills

`skills/kicad-quality-gate` and `skills/kicad-layout-prep` are thin SKILL.md
wrappers so Claude can drive the `kb` tools with the same guardrails.

## Layout

```
kicad_bench/core/      cli, schparse, config, kicad10, footprints, report
kicad_bench/quality/   erc_triage, netlist_audit, block_review, commit_gate
kicad_bench/layout/    netclass_coverage, dfm_preflight, stackup_sync, release_prep
configs/               per-project kicad-bench.toml files
skills/                SKILL.md wrappers
tests/                 pytest (pure logic, no kicad-cli needed)
```
