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

Every command takes `--config PATH`; without it, the tool searches upward from the
cwd for a `kicad-bench.toml`. **Exit codes** are uniform: `0` pass, `1` fail (a real
problem), `2` setup error (bad config, missing file, kicad-cli failure) — so the
tools compose and drop straight into CI or a git hook.

---

# The tools

Eight commands in two groups. Each section below says *what problem it solves*, *what
it actually does*, and *how to read the output*. Examples are real runs against the
Example Board project.

## Priority 1 — quality gates (read-only)

### `kb erc-triage [sheet]`

**Problem.** A raw ERC report mixes genuine errors with two kinds of benign noise:
partial-build artifacts (a leaf sheet captured before integration has "unconnected"
hierarchical pins) and a handful of known KiCad false-positives (e.g. the `power:GND`
symbol's pin sits ~0.47 mm off its anchor, so a correctly-connected ground pin reads
as `power_pin_not_driven`). Triaging these by eye on every run is slow and erodes
trust in the gate.

**What it does.** Runs `kicad-cli sch erc`, then classifies each violation:
- `ALLOWED` — a *partial-build* artifact (a `pin_not_connected` on a hierarchical
  label with no parent yet, or a `(power_)pin_not_driven` whose net is a hierarchical
  **input** on this sheet, i.e. driven by the not-yet-wired parent); or a documented
  false-positive matching a `[[erc.allow]]` rule in the config.
- `ERROR` — everything else.

Only `ERROR` lines fail the command. The partial-build logic mirrors the LVDS
project's `validate_sheet.py` exactly, so behaviour is identical; the `[[erc.allow]]`
layer is the new config-driven part.

```
$ kb erc-triage "Example Board/power_bias.kicad_sch" --config CFG
== ERC triage: power_bias.kicad_sch ==
  ~ [power_pin_not_driven] Input Power pin not driven ...  [U5.7]
      partial-build: net GND is a hier input (driven by parent)
  ~ [pin_not_connected] Hierarchical label 'VGH' ... non-existent parent  [342.90 mm, 48.26 mm]
      partial-build: hier pin, parent not wired yet
  -> PASS  ERC clean — 7 allowed partial-build/known exception(s)
```

**Reading it.** `~` = allowed (with the reason on the indented line), `✗` = real
error. If a violation you believe is benign shows as `✗`, the fix is to add a
*justified* `[[erc.allow]]` entry — the loader refuses any allow-rule without a
`reason`, so suppressions can never be silent.

### `kb netlist-audit [sheet]`

**Problem.** Some wiring bugs are invisible to ERC. The classic one here: an L-shaped
label stub can get silently absorbed so a net like `VCOM` ends up merged into `GND`.
The pins still connect to *something*, so ERC is happy — but the net is wrong.

**What it does.** Exports the netlist and compares every named net's node count
against an expected baseline (`contracts.expected_nets`, a `{net: count}` JSON map).
A mismatch, or an expected net missing from the netlist entirely, is an error. With
no baseline configured it just prints the node-count table — which is how you *build*
the baseline the first time. Generalizes `validate_sheet.py --expect`.

```
$ kb netlist-audit --config CFG          # no baseline -> dump
  · AVDD: 9 nodes   C33.1, C38.1, C39.1, C40.1, D6.1, J2.27, R13.1, R19.1, U5.9
  · GND: 146 nodes  ...
```
With a baseline, each line becomes `✓ GND: 146 nodes` or
`✗ VCOM: 0 nodes, expected 4` / `✗ VCOM: MISSING from netlist`.

### `kb block-review <sheet>`

**Problem.** When several people (or agents) build blocks in parallel, the silent
killers are (a) a net named *almost* right — `VIN_33V` vs `VIN_3V3` — which fails to
join at integration; (b) a reference designator claimed outside the block's reserved
range; (c) a footprint string that doesn't resolve. None of these is an ERC error.

**What it does.** A cheap independent review of one leaf sheet:
1. **Net names** — flags labels that are *close to but not exactly* a contract net
   (difflib ≥ 0.85). Exact matches and clearly-internal nets pass; near-misses are
   the typo signal.
2. **Designators** — every real reference must fall in this block's reserved range
   (or the shared `general` range). The block is inferred from the sheet filename;
   override with `--block`.
3. **Footprints** — every footprint string resolves to a real `.kicad_mod` (reuses
   the project's resolver, with difflib suggestions on a miss).

```
$ kb block-review "Example Board/video.kicad_sch" --config CFG
== Block review: video.kicad_sch ==
  ✗ designator ESD3 is outside every reserved range  [ESD3]
      block 'video' reserves: C60, C61, C62, C63, ...
  -> FAIL  1 issue(s) in video
```
(That is a *real* finding on LVDS: the video sheet uses `ESD3`, but the conventions
reserve only `ESD1` for video — exactly the drift this tool exists to catch.)

### `kb commit-gate`

**Problem.** "Run ERC + netlist + footprint checks before every commit" is a
remembered ritual; rituals get skipped under pressure.

**What it does.** Runs three gates in sequence and aggregates them into one verdict:
footprint resolution across all sheets → `erc-triage` on the root → `netlist-audit`
(if a baseline is configured). Because documented false-positives live in
`[[erc.allow]]`, any REAL error here is by definition an *undocumented* regression.

```
$ kb commit-gate --config CFG
== Footprints ==
  -> PASS  0 unresolved of 33 unique across 7 sheet(s)
== ERC triage: Example Board.kicad_sch ==
  -> PASS  ERC clean ...
== commit-gate: COMMIT ALLOWED ==
```

Wire it in permanently with `kb commit-gate --install-hook --config CFG`, which
writes a `pre-commit` hook calling this command (the only thing that ever writes
outside `output/`).

---

## Priority 2 — layout / DFM prep

### `kb netclass-coverage`

**Problem.** A net with no explicit net class silently inherits KiCad's Default
width — harmless for logic, dangerous for a 12 V power or panel-bias rail.

**What it does.** Reads the actual netlist and the fab/design-rules JSON (the same
`design_rules_config.json` the project's `gen_design_rules.py` consumes) and reports
every real net not covered by a domain or diff-pair member list.

```
$ kb netclass-coverage --config CFG
  ! VSYNC: no explicit class (falls to Default)  [VSYNC]
  · 44/107 nets explicitly classed
  -> PASS  63 unclassed net(s) — assign a class or set the Default width before routing
```
Reporting only by default; `--strict --min-coverage F` makes it fail below fraction
`F`. Generalizes `preflight_layout.check_netclass`.

### `kb dfm-preflight`

**Problem.** Before pushing a schematic into layout you want one gate that catches
the show-stoppers, separating hard blockers from things merely worth knowing.

**What it does.** A whole-project gate with a **hard** section and an
**informational** section:
- *Hard (fails the command):* real ERC errors; any symbol with **more pins than its
  footprint has pads** (a "update PCB from schematic" blocker); PCB DRC errors (only
  if the board actually has placed footprints).
- *Informational:* missing 3D models; net-class coverage summary.

```
$ kb dfm-preflight --config CFG
== DFM preflight ==
  ✓ ERC clean (real errors)
  ✓ no symbol exceeds its footprint pads
  ✓ PCB DRC clean
  -> PASS  no problems
== DFM preflight (informational) ==
  · all project-footprint 3D models present
  · net-class: 44/107 classed, 63 fall to Default
== DFM preflight: PASS ==
```
Generalizes `preflight_layout.py` and adds the PCB DRC pass.

### `kb stackup-sync`

**Problem.** The chosen fab stackup and min trace/space/drill/via constraints are
often filled into config but never entered into the project's Board Setup.

**What it does.** Reads the fab config (layers, thickness, copper, `fab_min`) plus
the chosen stackup id, then prints the recommended Board Setup values and **diffs**
them against what the project currently declares. It is *emit-and-apply*: by default
it only reports; `--emit` writes `output/board_setup_guide.md`. It **never** writes
the `.kicad_pcb` — you enter the values in KiCad.

```
$ kb stackup-sync --config CFG
  · target stackup: JLC04161H-7628 — 4-layer, 1.6mm, 1oz
  ! min_track_width: project has 0.127mm, recommend 0.09mm  [min_track_width]
  ✓ min_through_hole_diameter: 0.2mm matches  [min_through_hole_diameter]
  -> PASS  apply recommended values in KiCad → Board Setup (this tool does not write the PCB)
```

### `kb release-prep`

**Problem.** It's easy to generate fab outputs from a design that isn't actually
done — ERC errors left, DRC unrun, BOM rows still unsourced.

**What it does.** Gates fabrication export on three conditions: ERC triage clean, PCB
DRC clean (if laid out), and the BOM has no `NEEDS-PN` placeholder. With `--export`
(and only if all gates pass and the board is laid out) it writes gerbers + drill +
position file into `output/release/` via kicad-cli.

```
$ kb release-prep --config CFG
== Release readiness ==
  ✓ ERC clean
  ✓ PCB DRC clean
  ✗ BOM: 7 row(s) still NEEDS-PN
      220nF | C103 | C_0402_1005Metric | Backlight; ...
  -> FAIL  NOT ready
```

### `kb doctor`

Sanity check: confirms `kicad-cli` is on PATH and reports which config will be used.

---

## Configuration (`kicad-bench.toml`)

One file holds all project specifics, so the tools stay generic. A config kept in
*this* repo can point at a separate design repo via `project.root` (that's how
`configs/example-board.toml` keeps the LVDS design repo untouched).

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

[contracts.designators]                          # ranges expand inclusively (R22-R29 -> R22..R29)
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

**Where contract data comes from.** `contracts.nets` can be an inline list or
`from:<file>` — a design-rules JSON (whose `nets`/`diff_pairs` enumerate every named
net) or a markdown doc scanned for `UPPER_SNAKE` net tokens. This is deliberate: the
LVDS config sources its net list straight from the existing
`design_rules_config.json` so it never duplicates (and drifts from) the
source-of-truth.

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
