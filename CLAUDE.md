# kicad-bench — KiCad workflow & PCB-design workbench

Config-driven, read-first quality gates + datasheet-grounded review + layout/DFM prep
for KiCad 10 projects. One CLI (`kb`), one `kicad-bench.toml` per board. See
README.md for the per-tool reference; this file is the architecture contract.

## System overview

| Layer | Location | Purpose |
|-------|----------|---------|
| **Core** | `kicad_bench/core/` | shared substrate: `cli` (kicad-cli wrapper), `config`, `report` (Result/Finding/exit codes), `sexp`/`schparse`/`pcbgeom` (parsers), `graph` (design graph), `partspec`/`conspec` (spec sources), `review_prompt` |
| **Gates** | `kicad_bench/quality/`, `kicad_bench/layout/` | one module per check; pure logic + CLI registration |
| **Engines** | `audit.py`, `review.py` + `review_mcp.py`, `constraints.py`, `datasheet.py`, `board_state.py` | the composed surfaces: whole-repo audit, LLM review, constraint extraction, datasheet index, dashboard state |
| **Lifecycle** | `scaffold.py`, `init.py`, `release_freeze.py`, `fab.py` | Priority-0 monorepo bring-up → frozen releases |
| **CLI** | `main.py` | `_TOOLS` list; every tool module exposes `add_parser(sub)` + `run(args) -> int` |

## Architecture principles

- **Read-first is the product.** Quality gates and datasheet checks never write a
  design file. Layout tools write only into regenerable `output/` (exception:
  `netclass-sync --write` merges into `.kicad_pro` after a `.bak`; `commit-gate
  --install-hook` writes one git hook). Never add a tool that edits `.kicad_sch` /
  `.kicad_pcb` — emit-and-apply instead.
- **Leaf package.** `kicad_bench` must never import `kicad_parts` (the Cockpit in
  kicad-parts depends on this package — `board_state.py` is the shared engine).
  Cross-repo data flows by *path/config*, never by import.
- **Dependency-light.** Core deps: stdlib + `sexpdata` only. Anything heavier is an
  optional extra (`[xlsx]` openpyxl, `[llm]` anthropic) behind a guarded import with
  an actionable error message. The claude-code backend and the MCP server are
  deliberately stdlib-only.
- **Uniform gate anatomy.** A gate module = pure logic function taking
  already-extracted data and returning a `core.report.Result` + `run(args)` (loads
  config, resolves inputs, `render_and_exit`) + `add_parser(sub)`. Register in
  `main.py:_TOOLS`. Model: `quality/approved_parts.py`.
- **Exit codes:** 0 pass, 1 fail, 2 setup error (`sys.exit("error: ...")` from
  `run()`). Only `error`-severity findings fail; warnings never do. Warn-biased
  checks *skip, never guess* on missing data (aggregate skips into one info line).
- **Share the expensive ops.** `audit.run_all` does one netlist export + one DRC run
  and injects the results into gates (see `erc_triage.triage`'s signature). A new
  schematic gate should accept `counts`/`pin_net`/graph as arguments, not re-export.
- **The design graph** (`core/graph.py`) is the connectivity substrate for
  datasheet-grounded checks: components↔nets with rail voltages inferred from net
  names (conservative — unknown stays `None`, and consumers must treat `None` as
  unknown, never 0).
- **Constraint data** lives in `constraints.json` inside the committed
  `.datasheet-index/<slug>/` dirs (schema-versioned, sha-keyed to the PDF,
  MPN-family matched via `core/conspec.py`). Extraction provenance matters:
  `extracted_by: llm|heuristic` + `source_pages` page cites on every field.
- **Review severity is enforced twice.** The prompt (`core/review_prompt.py`, frozen
  for prompt-cache stability) asks for the discipline; `review.normalize()`
  guarantees it in code — **downgrade-only**: errors without an actually-read cited
  page demote to `Unverified:` warnings, nothing is ever promoted. Keep it that way;
  a false error is the biggest trust-killer.
- **LLM backends.** Default `claude-code` drives the local `claude` CLI
  (subscription auth, no key): review via a per-IC stdio MCP server
  (`review_mcp.py`) with `--strict-mcp-config` + tool allowlist so budgets hold;
  extraction via `claude -p --json-schema`. `api` uses the anthropic SDK. Both
  funnel through the same normalize/persist path. Resolve models explicitly — never
  inherit the user's claude CLI session default. Resolve the binary with
  `review.claude_bin()` (daemon PATHs lack `~/.local/bin`).
- **Persistence split:** committed = review artifacts (`.cockpit/review.json`),
  datasheet index + constraints; gitignored = regenerable caches (`.audit-cache/`,
  `.datasheet-cache/`, figures). `review.json`'s `sch_mtime` must be the max across
  *all* sheets (matching `BoardState.sch_mtime()`), or the Cockpit shows fresh
  reviews as stale.
- **KiCad footguns are documented where enforced** — DRU siblings load silently or
  not at all, netclass patterns are glob+regex simultaneously, flatpak kicad-cli
  can't read `/var/lib/flatpak` symlink targets (stage libs into /tmp). When you
  discover a new one, encode it in a gate and note it in the README.

## Development guidelines

- **Tests are pure.** Gate logic is tested by calling the pure function with
  synthetic dicts/graphs; kicad-cli is monkeypatched
  (`monkeypatch.setattr("kicad_bench.core.cli.netlist_nets", ...)`); LLM clients are
  faked (see `tests/test_review.py::FakeClient`). Where a file is genuinely needed,
  write a minimal one into `tmp_path`. `python3 -m pytest tests/ -q` from the repo
  root must stay green — no network, no KiCad required.
- Docstrings carry the *why*: each module opens with problem → behavior → boundaries
  (what it will never do). Match the existing comment density and voice.
- CLI help strings are the README's index — keep `add_parser(help=...)` one-line and
  accurate.
- Verify against a real board when behavior touches kicad-cli or live data
  (`kb audit` on an adopted repo); tests alone don't prove the CLI seam.
- The editable install means code changes are live in `kb` immediately — but the
  **Cockpit systemd service imports this package at startup**: restart it
  (`systemctl --user restart kicad-bench-sidecar.service`) after changing anything
  it consumes (`board_state.py`, `audit.py`, `review*.py`).
