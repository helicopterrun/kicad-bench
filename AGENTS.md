# kicad-bench

## Project Shape

`kicad-bench` is a generic, config-driven KiCad-10 workflow and PCB-design
workbench. It provides read-first quality gates, datasheet-grounded review, and
layout/DFM preparation through one CLI, `kb`, with per-board configuration in
`kicad-bench.toml`.

Main layers:

- `kicad_bench/core/`: shared CLI wrapper, config, report/result types, parsers,
  design graph, part/constraint specs, and review prompt support.
- `kicad_bench/quality/` and `kicad_bench/layout/`: individual gates and checks.
- `audit.py`, `review.py`, `review_mcp.py`, `constraints.py`, `datasheet.py`, and
  `board_state.py`: composed surfaces and shared engines.
- `scaffold.py`, `init.py`, `release_freeze.py`, and `fab.py`: lifecycle and fab
  export flow.
- `main.py`: CLI registration. Each tool module exposes `add_parser(sub)` and
  `run(args) -> int`.

## Architecture Rules

- Read-first is the product. Quality gates and datasheet checks report; they do
  not edit `.kicad_sch` or `.kicad_pcb`.
- Layout tools write only documented outputs: regenerable `output/` artifacts, or
  `netclass-sync --write` into `.kicad_pro` after creating a `.bak`.
- Lifecycle tools write their own artifacts: new monorepo scaffolds,
  `kicad-bench.toml` and daemon units from `init`, and `releases/<version>/`
  records from `release-freeze`.
- `commit-gate --install-hook` is the one tool that writes a git hook.
- Keep `kicad_bench` a leaf package. It must not import `kicad_parts`; cross-repo
  data flows by path and config.
- Keep core dependencies light: stdlib plus `sexpdata`. Heavier dependencies belong
  in optional extras with guarded imports and actionable errors.
- Use structured parsers and shared engines instead of fragile string matching.
- Missing or uncertain design data should skip or warn rather than guess.

## Gate Conventions

- A gate module has pure logic that returns `core.report.Result`, a `run(args)` that
  loads config and renders/exits, and `add_parser(sub)` for CLI registration.
- Exit codes are uniform: `0` pass, `1` fail, `2` setup error.
- Only `error` findings fail. Warnings do not fail.
- Share expensive operations in `audit.run_all`; new schematic gates should accept
  already-exported netlist/count/graph data rather than re-exporting.
- The design graph is the connectivity substrate. Unknown inferred values stay
  unknown; do not silently coerce them to zero.
- Constraint data lives in committed `.datasheet-index/<slug>/constraints.json`
  with schema versioning, PDF hash linkage, extraction provenance, and source page
  citations.
- Review severity is downgrade-only: uncited or unverified errors become warnings;
  code must not promote findings beyond what the evidence supports.

## Development

- Install with `pip install -e . --break-system-packages`.
- Optional extras: `.[xlsx]`, `.[imgdiff]`, and `.[llm]`.
- Run `python3 -m pytest tests/ -q` from the repo root after Python changes.
- Tests should be pure where possible. Mock `kicad-cli` calls and LLM clients; use
  minimal files in `tmp_path` only when a file is genuinely needed.
- Verify against a real board when behavior touches `kicad-cli` or live KiCad data.
- The Cockpit sidecar imports this package at startup. Restart
  `kicad-bench-sidecar.service` after changing code it consumes, such as
  `board_state.py`, `audit.py`, or `review*.py`.

## Documentation

- Module docstrings should explain the problem, behavior, and boundaries.
- CLI `help=` strings are the README index; keep them short and accurate.
- KiCad-specific footguns should be documented where they are enforced.
