# kicad-bench

[![tests](https://github.com/helicopterrun/kicad-bench/actions/workflows/test.yml/badge.svg)](https://github.com/helicopterrun/kicad-bench/actions/workflows/test.yml)

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
pip install -e . --break-system-packages   # exposes the `kb` CLI
kb doctor --config configs/example-board.toml
```

Requires KiCad 10 `kicad-cli` on PATH. Core is stdlib-only; `release-prep`'s BOM
check uses `openpyxl` if present.

Every command takes `--config PATH`; without it, the tool searches upward from the
cwd for a `kicad-bench.toml`. **Exit codes** are uniform: `0` pass, `1` fail (a real
problem), `2` setup error (bad config, missing file, kicad-cli failure) — so the
tools compose and drop straight into CI or a git hook.

---

# The tools

Sixteen commands. **`kb audit`** runs all the read-only checks below at once (sharing one
netlist export and one DRC run) and prints a consolidated whole-repo report — start there;
the individual commands are for targeted checks. **`kb sidecar`** serves the same audit as a
live web dashboard to keep open beside KiCad (it re-audits when you save the board). Each
section below says *what problem it solves*, *what
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

Only `ERROR` lines fail the command. The partial-build logic mirrors the original
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

**Fail-safe.** The only violation *types* this tool can down-classify are
`pin_not_connected` / `pin_not_driven` / `power_pin_not_driven` (plus any type named
in an allow-rule). Every other type is treated as REAL and tagged "unmapped ERC type"
— the tool never assumes silence for a violation class it doesn't understand.

### `kb netlist-audit [sheet]`

**Problem.** Some wiring bugs are invisible to ERC. The classic one here: an L-shaped
label stub can get silently absorbed so a net like `VCOM` ends up merged into `GND`.
The pins still connect to *something*, so ERC is happy — but the net is wrong.

**What it does.** Exports the netlist and compares every named net's node count
against an expected baseline (`contracts.expected_nets`, a `{net: count}` JSON map,
resolved relative to the config file). A mismatch, or an expected net missing
entirely, is an error. Generalizes `validate_sheet.py --expect`.

Build the baseline once from a known-good state, then it becomes a regression gate:
```
$ kb netlist-audit --emit-baseline configs/ --config CFG    # writes <config>.expected_nets.json
  ✓ wrote baseline of 109 nets

$ kb netlist-audit --config CFG                              # now ASSERTS
  ✓ GND: 146 nodes
  ✗ VCOM: 0 nodes, expected 4        # or: ✗ VCOM: MISSING from netlist
  -> PASS  0 mismatch(es) of 109 checked
```
With no baseline configured it just prints the node-count table. commit-gate runs
this automatically once a baseline is set.

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
(That is a *real* finding on this board: the video sheet uses `ESD3`, but the conventions
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

**Problem.** A net with no class silently inherits KiCad's Default width — harmless
for logic, dangerous for a 12 V power or panel-bias rail. Crucially, KiCad does *not*
class nets from any planning document: it resolves each net's class from the
project's `net_settings` (explicit `netclass_assignments`, else matching
`netclass_patterns`, else Default). So "what the plan wants" and "what the board
actually does" are two different things.

**What it does.** Reports the **actual** per-net class as KiCad resolves it (a net is
assigned to *every* matching pattern's class — KiCad applies all matches, not the
first/last), then cross-references the planning config (`fab.config` domains) to
surface the gap: nets the plan wants on a power/bias/diff-pair class but the project
leaves on Default.

```
$ kb netclass-coverage --config CFG
  · classes defined in project: Default, I2C, Power, SPI, USB
  ! NO netclass patterns or assignments in the project — all 109 nets fall to Default
  ! AVDD: plan wants 'BIAS' class, project leaves it on Default  [AVDD]
  ! LVDS_CLK_P: plan wants 'diff-pair' class, project leaves it on Default  [LVDS_CLK_P]
  ...
  -> PASS  0/109 nets on a non-Default class; 109 on Default; 43 planned net(s) still unclassed
```
That is the real picture for this project today: classes are *defined* but **no
patterns assign any net**, so everything is Default. `--strict --min-coverage F` fails
when planned-net coverage drops below `F`.

> Pattern matching note: KiCad treats a pattern as **both** glob (`*`/`?`) **and**
> regex simultaneously — so e.g. `X*` matches every net (regex zero-or-more). This
> tool reproduces that behaviour faithfully (it's a documented KiCad footgun).

### `kb netclass-sync`

**Problem.** `netclass-coverage` shows the gap; this closes it. The planning config
knows which nets belong to which power/bias/diff-pair domain, but the `.kicad_pro`
has no classes assigned, so everything is Default.

**What it does.** Generates a `net_settings` block from the planning config — one
class per domain/diff-pair (clearance, track width, via size/drill, diff-pair
gap/width straight from the config) plus an **exact** `netclass_assignments` map
(net → [class], no wildcard ambiguity), only for nets that actually exist in the
netlist (planned-but-absent nets are reported and skipped). By default it writes the
fragment to `output/netclass_settings.json` and leaves the project untouched.
`--write` **merges** it into the `.kicad_pro` (keeping existing classes, adding new
ones, setting assignments) after writing a `.bak` — it never touches `.kicad_sch`/
`.kicad_pcb`.

```
$ kb netclass-sync --config CFG               # emit (safe)
  · 9 class(es) from plan; 42 of 45 planned nets present and assignable
  ! planned net not in netlist (skipped): VIN_3V3   # surfaces plan vs. netlist drift
  -> wrote output/netclass_settings.json

$ kb netclass-sync --config CFG --write       # merge into .kicad_pro (+ .bak)
  ✓ added 8 class(es), set 42 net assignment(s)
```
After `--write`, also copy the generated `.kicad_dru` next to the board so custom
rules apply (see the DRC note under `dfm-preflight`), and validate it with `dru-lint`.

### `kb dfm-preflight`

**Problem.** Before pushing a schematic into layout you want one gate that catches
the show-stoppers, separating hard blockers from things merely worth knowing.

**What it does.** A whole-project gate with a **hard** section and an
**informational** section:
- *Hard (fails the command):* real ERC errors; any symbol with **more pins than its
  footprint has pads** (a "update PCB from schematic" blocker); PCB DRC errors (only
  if the board has placed footprints).
- *Informational:* missing 3D models; net-class coverage summary; whether the
  project's custom rules are actually active (see the DRC note below).

```
$ kb dfm-preflight --config CFG
== DFM preflight ==
  ✓ ERC clean (real errors)
  ✓ no symbol exceeds its footprint pads
  ✓ PCB DRC clean — 0 errors (14 warning(s))
  -> PASS  no problems
== DFM preflight (informational) ==
  ! custom rules NOT active: no Example Board.kicad_dru next to the board —
    rules generated into output/ aren't applied until copied here
  · all project-footprint 3D models present
  · net-class: 0/109 on a non-Default class, 109 fall to Default; 43 planned net(s) still unclassed
== DFM preflight: PASS ==
```
Generalizes `preflight_layout.py` and adds the PCB DRC pass (parsed from `--format
json`, counting error-severity violations).

> **DRC + custom rules (verified on KiCad 10.0.3).** `kicad-cli pcb drc` *does* apply
> a project's custom design rules — but only from a file named `<board>.kicad_dru`
> sitting next to the board (matching base name), and only if it's syntactically
> valid (a malformed DRU is silently dropped). This project generates its rules into
> `output/` (gitignored), so they are **not active** until copied to
> `Example Board/Example Board.kicad_dru`. dfm-preflight flags this explicitly.

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

### `kb dru-lint`

**Problem.** KiCad applies custom rules only if the `.kicad_dru` is syntactically
valid — but it drops a malformed file *silently*, and there is no `kicad-cli` command
to validate one (only a GUI "Check Rule Syntax" button). So a typo in a generated DRU
means your rules just quietly don't apply.

**What it does.** Static-checks a `.kicad_dru`: `(version 1)` header, balanced parens,
every `(constraint …)` against the KiCad 10 constraint set, every `(severity …)`
against `error/warning/ignore/exclusion`, rules-without-constraints, and an advisory
to prefer `A.hasNetclass('X')` over `A.NetClass == 'X'`. Defaults to the project's
sibling `<board>.kicad_dru` or the generated `output/*_design_rules.kicad_dru`.

```
$ kb dru-lint --config CFG
== DRU lint: Example Board_design_rules.kicad_dru ==
  -> PASS  41 rule(s), 44 constraint(s); 0 error(s)
```

## Priority 3 — routed-geometry audits (read-only)

These read the actual track/via/zone geometry from the `.kicad_pcb` (via
`core/pcbgeom`, the one place that parses board copper) and check what `kicad-cli pcb
drc` cannot: diff-pair length/skew, as-routed width vs per-domain floors, and routing
completeness. All are read-only; the matching *writer* (`apply_track_widths.py`) lives
in the design repo so this package stays read-first.

### `kb diffpair-audit`

**Problem.** DRC enforces a pair's gap/width *floor* but never reports the things that
decide whether a 100 Ω / 90 Ω pair works: per-net routed length, intra-pair skew, the
as-routed width, or whether the pair dropped to an inner/reference layer.

**What it does.** For every pair in `fab.config` `diff_pairs`, reports len(P)/len(N),
intra-pair skew, inter-pair bus spread, width vs target, layer (flags a pair not on a
single outer layer), and via count. `--max-skew` / `--width-tol` / `--max-bus-spread`
set the budgets; `--strict` exits 1 on any flagged pair.

### `kb track-conformance`

**Problem.** DRC has one global minimum track width; it can't say "this 12 V rail was
routed below its 0.5 mm domain floor."

**What it does.** Maps every routed net to its domain (from `fab.config`) and reports
tracks/vias under floor, as a per-domain rollup. Respects copper *pours*: a poured power
net with no thin discrete tracks is conformant, not "thin." `--strict` exits 1 on any
under-floor copper.

### `kb route-coverage`

**Problem.** "How much is left to route?" normally means opening pcbnew.

**What it does.** Counts KiCad's ratsnest (`unconnected_items` from DRC) per net and
rolls it up by hierarchical block and by net class, and lists still-open diff pairs — a
headless routing dashboard. `--strict` exits 1 if any routable net is still open.

### `kb dru-guard`

**Problem.** KiCad loads custom rules only from a `<board>.kicad_dru` sibling; copy it
under the wrong name or let it drift from the config and DRC silently runs without the
impedance/clearance rules.

**What it does.** Verifies the board-sibling DRU exists, is current vs a fresh
`gen_design_rules.py` run, passes `dru-lint`, that the `.kicad_pro` classes still cover
the config's domains/diff-pairs, and that the stackup id is consistent. Flags a dead
non-loading `<board>_design_rules.kicad_dru` sibling.

### `kb sidecar`

**Problem.** The audit is a terminal command; while routing you want it *glanceable* and
*live*, not something you re-run by hand.

**What it does.** Starts a tiny stdlib web server (no Flask) serving one self-contained
dark dashboard: the verdict, a "fix next" list of every error, and every check with
expandable findings. It watches the board's mtime and re-audits automatically when you save
in KiCad, caching by mtime so extra tabs/polls don't trigger redundant DRC runs. Read-only.

```
$ kb sidecar --config CFG            # → http://127.0.0.1:8765
$ kb sidecar --host 0.0.0.0 --port 9000
```
Runs wherever the board file and `kicad-cli` are: on the Mac it tracks live saves; on a
headless server it reflects each `git pull`. The design repo's `scripts/sidecar.py` launches
it no-arg.

### `kb doctor`

Sanity check: confirms `kicad-cli` is on PATH and reports which config will be used.

---

## Configuration (`kicad-bench.toml`)

One file holds all project specifics, so the tools stay generic. A config kept in
*this* repo can point at a separate design repo via `project.root` (that's how
`configs/example-board.toml` keeps the example design repo untouched).

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
example config sources its net list straight from the existing
`design_rules_config.json` so it never duplicates (and drifts from) the
source-of-truth.

## Relationship to the design repo's `scripts/`

kicad-bench *generalizes and consolidates* the per-project validators
(`validate_sheet.py`, `validate_footprints.py`, `preflight_layout.py`,
`gen_design_rules.py`) into reusable, config-driven tools sharing one core
(`kicad_bench/core/`). The originals remain the source-of-truth references; the
classification and resolution logic here is ported to match them.

The Priority-3 geometry parser (`core/pcbgeom`) is shared the other direction too: the
design repo's `scripts/audit.py` (a no-arg wrapper over `kb audit` for the whole repo) and
`scripts/apply_track_widths.py` (the mutating writer that widens under-floor tracks) both
import it. Read-only audits live here; the one writer lives there, so this package stays
read-first.

## Skills

`skills/kicad-quality-gate` and `skills/kicad-layout-prep` are thin SKILL.md
wrappers so Claude can drive the `kb` tools with the same guardrails.

## Layout

```
kicad_bench/audit.py   consolidated whole-repo audit (composes every check below)
kicad_bench/sidecar.py live web dashboard of the audit (stdlib server + one HTML page)
kicad_bench/core/      cli, schparse, config, kicad10, footprints, report, pcbgeom
kicad_bench/quality/   erc_triage, netlist_audit, block_review, commit_gate
kicad_bench/layout/    netclass_coverage, netclass_sync, dfm_preflight, stackup_sync,
                       release_prep, dru_lint,
                       diffpair_audit, track_conformance, route_coverage, dru_guard
configs/               per-project kicad-bench.toml files
skills/                SKILL.md wrappers
tests/                 pytest (pure logic, no kicad-cli needed)
```
