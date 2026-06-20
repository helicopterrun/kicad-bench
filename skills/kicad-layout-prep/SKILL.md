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
| "Which nets actually have a class, and what's the gap?" | `kb netclass-coverage --config CFG` |
| "Is the design ready to push into layout?" | `kb dfm-preflight --config CFG` |
| "What board-setup values should I enter?" | `kb stackup-sync --config CFG [--emit]` |
| "Is this .kicad_dru valid?" | `kb dru-lint [path] --config CFG` |
| "Export fab files (if releasable)" | `kb release-prep --config CFG [--export]` |

## How to interpret results
- **netclass-coverage**: reports each net's ACTUAL class as KiCad resolves it from the
  project's `net_settings` (patterns/assignments), not from any planning doc. A net on
  Default inherits the Default width — fine for logic, risky for power/bias. The
  "plan wants X, project leaves on Default" lines are the gap to close before routing.
- **dfm-preflight**: the **hard** block fails on real ERC errors, any symbol with
  more pins than its footprint has pads, or PCB DRC errors. The **informational**
  block also flags whether custom rules are *active*: `kicad-cli pcb drc` only loads
  a `<board>.kicad_dru` sitting next to the board, so rules generated into `output/`
  do nothing until copied there. Validate that file first with `dru-lint`.
- **dru-lint**: static-checks a `.kicad_dru` (no `kicad-cli` validator exists; KiCad
  silently drops a malformed one). Run it before copying generated rules into place.
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
