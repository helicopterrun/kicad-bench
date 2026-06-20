---
name: kicad-layout-prep
description: >
  Run the kicad-bench Priority-2 layout/DFM-prep tools on a KiCad project —
  net-class coverage (which nets fall to Default), DFM preflight (ERC + pins-vs-pads
  + 3D + PCB DRC), stackup-sync (reconcile the chosen fab stackup/constraints with
  the project, emit-and-apply), and release-prep (gated gerber/drill/pos export with
  ERC/DRC/BOM guardrails). Use when moving a finished schematic toward layout/fab.
  Reports only by default; --emit/--export write only into the gitignored output/.
---

# kicad-layout-prep

Thin wrapper over the `kb` CLI for the layout/fab phase. Reporting is read-only;
the only writes are into `output/` (regenerable) and never into the `.kicad_pcb`.

## Prerequisites
Same as kicad-quality-gate: `kb` + `kicad-cli` + a `kicad-bench.toml`. For LVDS use
`--config /root/kicad-bench/configs/example-board.toml`.

## When to use which command

| Situation | Command |
|---|---|
| "Which nets still need a net class?" | `kb netclass-coverage --config CFG` |
| "Is the design ready to push into layout?" | `kb dfm-preflight --config CFG` |
| "What board-setup values should I enter?" | `kb stackup-sync --config CFG [--emit]` |
| "Export fab files (if releasable)" | `kb release-prep --config CFG [--export]` |

## How to interpret results
- **netclass-coverage**: every `!` net inherits the Default class width — fine for
  logic, risky for power/bias. Assign a class (see the project's net-class guide) or
  set a safe Default before routing. `--strict --min-coverage F` fails below F.
- **dfm-preflight**: the **hard** block fails on real ERC errors, any symbol with
  more pins than its footprint has pads, or PCB DRC errors. The **informational**
  block (3D models, net-class) never fails but is worth reading.
- **stackup-sync**: emit-and-apply. It prints the recommended Board Setup values and
  diffs them against the project; `--emit` writes `output/board_setup_guide.md`. It
  does **not** write the `.kicad_pcb` — enter the values in KiCad yourself.
- **release-prep**: refuses to export unless ERC is clean, PCB DRC is clean, and the
  BOM has no `NEEDS-PN` placeholder. `--export` writes gerbers/drill/pos to
  `output/release/` only when all gates pass and the board is laid out.

## Guardrails
- Never hand-edit the `.kicad_pcb` to satisfy a gate; resolve the real issue.
- After locking the stackup, also regenerate the `.kicad_dru` with the project's
  `gen_design_rules.py` and copy it in.
