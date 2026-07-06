---
name: kicad-product-workflow
description: >
  End-to-end hardware product lifecycle for kicad-bench product monorepos —
  scaffold a new product, gate every board, and freeze a release. Use when the
  user mentions creating a new product/board, scaffolding a KiCad project, the
  product-monorepo layout (hardware/, firmware/, mechanical/, releases/), approved-
  parts / MPN compliance, freezing or tagging a hardware release, or preparing
  fabrication files. Drives the `kb` CLI; the write commands only ever create a new
  product tree or the frozen releases/ dir — never an existing .kicad_sch/.kicad_pcb.
---

# kicad-product-workflow

Thin wrapper over the `kb` CLI (the kicad-bench package) for the **product lifecycle**:
one git repo per product, multiple boards per product. It complements the read-only
quality/layout skills — here the commands scaffold, gate, and freeze a whole product.

## Prerequisites
- `kb` on PATH (`pip install -e .`) and `kicad-cli` (KiCad 10) for the gate/freeze steps.
- A product created by `kb scaffold` carries its own `kicad-bench.toml`, so most commands
  need no `--config` (upward search finds it). Run `kb doctor` to check.

## The three workflows

| Situation | Command |
|---|---|
| "New product / new board / scaffold X" | `kb scaffold <product> [board1 board2 …]` |
| "Is board X ready? gate it / check MPNs" | `kb audit --board X` · `kb approved-parts --board X` |
| "Freeze / tag / ship release v1.0" | `kb release-freeze v1.0` |

### 1. Scaffold — `kb scaffold`
Creates the monorepo tree (`hardware/<board>/`, `firmware/`, `mechanical/`, `releases/`,
`docs/`, `tools/`), compliance-aware doc stubs, a KiCad-aware `.gitignore`, a wired-in
`kicad-bench.toml` + seed `approved_parts.csv`, and a `hardware-ci.yml`; then `git init`
+ initial commit. It does **not** create `.kicad_pro/.kicad_sch/.kicad_pcb` — direct the
user to open KiCad and save each board at `hardware/<board>/<board>.kicad_pro`.

### 2. Gate — `kb audit` / `kb approved-parts`
Once boards are saved, gate each one. `kb audit` runs every read-only check (ERC/DRC/
netlist/DFM/geometry); `kb approved-parts` enforces the `approved_parts.csv` MPN
allow-list. Both take `--board <name>` on multi-board products (default: first board).
Report findings grouped: blockers (ERC/DRC errors, unapproved parts) → should-fix →
nice-to-have. CI (`.github/workflows/hardware-ci.yml`) runs both per board automatically.

### 3. Freeze — `kb release-freeze`
`kb release-freeze v1.0` validates the version + a clean tree, runs the readiness gate on
**every** board, exports the full fab package into `releases/v1.0/<board>/`, then commits
and annotated-tags. It does not push — push the tag yourself to trigger the CI release job
(fab package → GitHub Release). `--dry-run` gates without writing; `--no-git` exports only.

## Guardrails
- The invariant: `hardware/*/output/` is regeneratable; `releases/v*/` is the frozen
  record of what went to the fab. Never hand-edit `releases/`.
- Do not edit a `.kicad_sch`/`.kicad_pcb` to make a gate pass — fix the issue, or document
  an ERC exception with a reason in `[[erc.allow]]`.
- Do not recommend re-ordering or mass part changes without the user's explicit go-ahead;
  their judgment on their own design outweighs the tool's suggestions.

## When *not* to use this skill
- Pure firmware questions with no hardware involvement → normal coding flow.
- KiCad UI how-tos ("where is the net highlighter?") → answer directly.
- Component value math (divider, filter cutoff) → answer directly.
- Quick schematic reads ("what does this net do?") → open the file, don't run the pipeline.
