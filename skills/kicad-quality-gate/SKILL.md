---
name: kicad-quality-gate
description: >
  Run the kicad-bench Priority-1 quality gates on a KiCad project — ERC triage
  (real vs. known-allowed), per-net node-count audit (catches nets silently merged
  into GND), independent block review (net names / designator ranges / footprint
  strings), and the composite commit-gate. Use before committing a schematic
  change, when reviewing a parallel-agent block, or when an ERC report is noisy and
  you need real-vs-false-positive triage. Read-only; never edits design files.
---

# kicad-quality-gate

Thin wrapper over the `kb` CLI (the kicad-bench package). All commands are
read-only and exit non-zero on a real problem, so they compose and gate commits.

## Prerequisites
- `kb` on PATH (`pip install -e /root/kicad-bench`) and `kicad-cli` (KiCad 10).
- A `kicad-bench.toml` for the project. For Example Board use
  `--config /root/kicad-bench/configs/example-board.toml`. Run `kb doctor` to check.

## When to use which command

| Situation | Command |
|---|---|
| "Is the ERC report real or known noise?" | `kb erc-triage [sheet] --config CFG` |
| "Did a net get silently merged into GND?" | `kb netlist-audit [sheet] --config CFG` |
| "Review this block before merge" | `kb block-review <sheet> --config CFG` |
| "Gate this commit" | `kb commit-gate --config CFG` |
| "Enforce the gate automatically" | `kb commit-gate --install-hook --config CFG` |

## How to interpret results
- **erc-triage**: `ALLOWED` = partial-build artifact (hier pin / input driven by a
  not-yet-wired parent) or a documented false-positive from `[[erc.allow]]`. Only
  `ERROR` lines fail. If a violation you believe is benign shows as `ERROR`, the fix
  is to add a justified `[[erc.allow]]` entry — never to silence it without a reason.
- **netlist-audit**: needs `contracts.expected_nets` (a `{net: count}` baseline) to
  assert; with none it just dumps node counts. Build the baseline from a known-good
  state with `kb netlist-audit --emit-baseline <dir-or-path>`, then point
  `contracts.expected_nets` at it (resolved relative to the config file).
- **block-review**: net-name findings flag labels that are *close to but not exactly*
  a contract net (the silent-rewire bug); designator findings flag refs outside the
  block's reserved range.
- **commit-gate**: runs footprints → erc-triage → netlist-audit and prints
  `COMMIT ALLOWED` / `COMMIT BLOCKED`.

## Guardrails
- Do not edit `.kicad_sch` to make a gate pass — fix the underlying issue or document
  the exception with a reason.
- These tools never modify design files; if `git status` is dirty after running them,
  something else changed it.
