# kicad-bench

[![tests](https://github.com/helicopterrun/kicad-bench/actions/workflows/test.yml/badge.svg)](https://github.com/helicopterrun/kicad-bench/actions/workflows/test.yml)

A generic, config-driven **KiCad-10 workflow & PCB-design workbench**: read-first
quality gates, datasheet-grounded design review, and layout/DFM prep that wrap the
noisy, manual, error-prone steps of schematic→BOM→layout→release with guardrails. It
complements design-*generation* tooling (sheet codegen, parts pickers) — it does
**not** edit schematic design content.

Every project specific lives in one `kicad-bench.toml`, so the same tools work on the
next board. One CLI (`kb`), five tiers:

| Tier | Commands | What it covers |
|------|----------|----------------|
| **Priority 0 — lifecycle** | `scaffold`, `init`, `release-freeze` | product monorepo bring-up → frozen, tagged releases |
| **Priority 1 — quality gates** | `erc-triage`, `netlist-audit`, `block-review`, `approved-parts`, `eco-drift`, `commit-gate`, `ksir-sync`, `symbol-style`, `sch-readability` | read-only schematic/library gates |
| **Datasheet-grounded checks** | `cap-derating`, `led-current`, `pin-mux`, `constraints`, `review` | the semantic layer above ERC — deterministic checks + an LLM reviewer, both grounded in the manufacturer datasheets |
| **Priority 2 — layout / DFM prep** | `netclass-coverage`, `netclass-sync`, `dfm-preflight`, `stackup-sync`, `release-prep`, `dru-lint` | pre-layout and pre-fab gates |
| **Priority 3 — routed geometry** | `diffpair-audit`, `track-conformance`, `route-coverage`, `dru-guard`, `board-diff` | as-routed copper audits DRC can't do |

Plus the working surfaces: **`kb audit`** (everything at once), **`kb datasheet`**
(harvest/ingest/read PDFs cheaply), **`kb lib-search`**, **`kb sch-live`**, **`kb
stage`**, **`kb sidecar`**, and **`kb doctor`**.

## The read/write boundary

- **Quality gates and datasheet checks** are strictly read-and-report. Nothing edits
  a design file.
- **Layout tools** report by default. The only writes they ever make are into the
  regenerable `output/` dir (`stackup-sync --emit`, `release-prep --export`) or, for
  `netclass-sync --write`, a `.bak`-backed merge into the `.kicad_pro`. They **never**
  write `.kicad_sch` or `.kicad_pcb` — stackup/board-setup changes are *emitted for
  you to apply* in KiCad.
- **Lifecycle tools** write only their own artifacts: `scaffold` a new monorepo,
  `init` a `kicad-bench.toml` + daemon units, `release-freeze` the `releases/<v>/`
  record.
- **Data stores** the tools own: the committed `.datasheet-index/` (ingested
  datasheet text/metadata + extracted `constraints.json`), the committed
  `.cockpit/review.json` (review findings), and gitignored caches
  (`.audit-cache/`, `.datasheet-cache/`).
- The one hook: `commit-gate --install-hook` writes a single git pre-commit hook into
  the project's `.git/hooks`.

## Install

```sh
pip install -e . --break-system-packages   # exposes the `kb` CLI
kb doctor                                  # checks kicad-cli + config resolution
```

Requires KiCad 10 `kicad-cli` on PATH. Core is stdlib-only (plus the tiny `sexpdata`
s-expression reader). Optional extras:

```sh
pip install -e '.[xlsx]'   # openpyxl — .xlsx BOM support
pip install -e '.[llm]'    # anthropic — only for `--backend api`; the default
                           # claude-code backend needs no extra and no API key
```

Every command takes `--config PATH`; without it, the tool searches upward from the
cwd for a `kicad-bench.toml` (or honors `$KICAD_BENCH_CONFIG` / a
`.kicad-bench-config` pointer). Multi-board configs take `--board`. **Exit codes**
are uniform: `0` pass, `1` fail (a real problem), `2` setup error (bad config,
missing file, kicad-cli failure) — so the tools compose and drop straight into CI or
a git hook.

---

# `kb audit` — start here

Runs every read-only check at once (sharing one netlist export and one DRC run) and
prints a consolidated whole-repo report, grouped into **Schematic**, **Datasheet
checks**, **Rules & config**, and **Layout & DFM** sections, with a "fix next" error
digest. The individual commands below are for targeted checks; `--full` prints every
finding.

The same audit powers the live dashboards: the built-in `kb sidecar`, and the
[Cockpit](../kicad-parts) (`kp cockpit`) — the multi-board FastAPI/Vue dashboard that
superseded the sidecar on this bench and adds the Review tab, previews, parts/BOM
views, and notes. `kicad_bench/board_state.py` is the shared engine both consume.

---

# Priority 0 — product lifecycle

### `kb scaffold <product> [boards...]`

Creates a new product monorepo — `hardware/<board>/`, `firmware/`, `mechanical/`,
docs, per-board `kicad-bench.toml` — so every board starts life already wired into
the gates. (The merged successor of the old `kicad-product-workflow` skill; design
notes in `docs/product-workflow-merge.md`.)

### `kb init <design-repo>`

One-command adoption of an *existing* KiCad repo: auto-discovers the root schematic /
PCB / fp-lib-table / BOM and writes a `kicad-bench.toml` (+ optional daemon units).
This is also what the Cockpit's "+ Board" button calls.

### `kb release-freeze <version>`

Extends `release-prep`'s readiness gate to the whole product, then turns a clean
state into a frozen record: fab exports into `releases/<version>/<board>/`, a commit,
and a tag. Fab export itself lives behind one seam (`kicad_bench/fab.py`) shared with
`release-prep`.

---

# Priority 1 — quality gates (read-only)

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

Only `ERROR` lines fail the command.

```
$ kb erc-triage "Example Board/power_bias.kicad_sch"
== ERC triage: power_bias.kicad_sch ==
  ~ [power_pin_not_driven] Input Power pin not driven ...  [U5.7]
      partial-build: net GND is a hier input (driven by parent)
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

**Problem.** Some wiring bugs are invisible to ERC. The classic one: an L-shaped
label stub can get silently absorbed so a net like `VCOM` ends up merged into `GND`.
The pins still connect to *something*, so ERC is happy — but the net is wrong.

**What it does.** Exports the netlist and compares every named net's node count
against an expected baseline (`contracts.expected_nets`). A mismatch, or an expected
net missing entirely, is an error.

```
$ kb netlist-audit --emit-baseline configs/     # write baseline from a known-good state
$ kb netlist-audit                              # now ASSERTS
  ✗ VCOM: 0 nodes, expected 4
```

### `kb block-review <sheet>`

**Problem.** When several people (or agents) build blocks in parallel, the silent
killers are (a) a net named *almost* right — `VIN_33V` vs `VIN_3V3` — which fails to
join at integration; (b) a reference designator claimed outside the block's reserved
range; (c) a footprint string that doesn't resolve. None of these is an ERC error.

**What it does.** A cheap independent review of one leaf sheet: contract-net
near-miss detection (difflib ≥ 0.85), designator-range enforcement (from
`[contracts.designators]`), and footprint resolution with suggestions.

### `kb approved-parts [sheet]`

**Problem.** Ad-hoc MPN choices bypass sourcing/qualification.

**What it does.** Checks every component's MPN against the project's
`approved_parts.csv` allow-list (incl. `alt_mpn_*` second sources). Policy in
`[approved_parts]`: missing MPN and unapproved MPN can each be error or warn. This is
the `kb` side of the contract that [`kicad-parts`](../kicad-parts)' catalog kanban
syncs into (`kp` promotes catalog rows → `approved_parts.csv`).

### `kb board-diff <old-rev> [<new-rev>]`

**Problem.** `kb changes` lists commits; nothing said what they did to the *board*.
Reading a `.kicad_pcb` diff by eye is hopeless — nudging one footprint rewrites
coordinates all over the file, and a re-pour rewrites megabytes of zone polygon.

**What it does.** Answers the review question instead: which components appeared,
vanished, changed footprint, moved or flipped (with offsets); which nets appeared,
vanished or changed membership; how the copper totals moved. Pure set/dict math over
the parsers that already exist. When the file differs but nothing structural does, it
says exactly that rather than a flat "no change".

**Never `git checkout`.** History is read with `git show <rev>:<path>`, which writes
nothing — a checkout would clobber uncommitted work in the tree being reviewed. There
is a regression test asserting the working tree is byte-identical afterwards.

**`--image`.** Writes a colour-coded overlay of the two revisions (old red, new blue).
Both renders come from `--page-size-mode 2 --exclude-drawing-sheet`, so two revisions
of the same outline are pixel-aligned for free.
* `--image out.svg` composes the two SVG exports — stdlib only, works anywhere
  `kicad-cli pcb export svg` does.
* `--image out.png` does a true pixel diff and reports what fraction of the board
  changed. Needs the `[imgdiff]` extra (Pillow) plus `pdftoppm`, and goes board → PDF →
  PNG because kicad-cli has no 2D PNG export.

**KiCad footgun it encodes.** The PCB **PDF** plotter silently emits *no file* when
given `--page-size-mode 2` or `--exclude-drawing-sheet` — exit code 0, empty output.
Either flag alone breaks it; the SVG plotter accepts both, and `sch export pdf` is
unaffected. The `Can't get document portal` line on stderr is a red herring printed on
every kicad-cli invocation and is **not** the cause. So `export_pcb_pdf` renders at full
page size with the drawing sheet left in, and `imgdiff.overlay` crops to the union of
both revisions' ink instead — otherwise the changed-pixel percentage would be measured
against the paper rather than the board.

**Render polarity is not fixed either.** The SVG export uses the dark editor theme
(light ink on dark); the PDF plotter draws dark ink on white. A fixed ink threshold made
one of them read as ~100% ink and every diff came out empty, so the mask is chosen per
image from its mean luminance.

### Physics-grounded impedance (`core/ipc.py`, wired into `kb diffpair-audit`)

`track-conformance` checks copper against a *config floor* — a number someone typed.
`core/ipc.py` checks against the published models instead: closed-form IPC-2141
microstrip/stripline and differential impedance, plus IPC-2221 current capacity.

The geometry comes from the **board's own** `(setup (stackup ...))`, now parsed into
`pcbgeom.Board.stackup` — dielectric height and epsilon_r per layer. The board file is
the authoritative source, so it can't drift the way a parallel fab-config copy would,
and no new config keys are needed. `diffpair-audit` already read
`target_impedance_ohm` and only printed it; it now computes the pair's actual
differential impedance and warns outside ±10%.

Everything here is **mm-native** (the reference implementations in the wild are
mils-native; mixing the two is a permanent bug source) and **warn-biased** — the IPC
closed forms are curve fits good to roughly ±10%, so a computed value is evidence for
a warning, never for an error.

It skips rather than guessing when the model doesn't describe the geometry: no stackup,
`w/h` outside the fit's 0.1–3.0 validity band, or a coplanar ground pour *closer* than
the reference plane (then the pour sets the impedance, not the plane). That last rule
is load-bearing — on a 2-layer board with a 1.51 mm dielectric and a 0.2 mm pour
clearance the plain microstrip model reads 144 Ω against a 90 Ω target, which is a
model mismatch, not a design error. On a thin-prepreg inner-ground stackup where the
plane is nearer, the model holds and is used.

### `kb eco-drift [sheet]`

**Problem.** Nothing checked that the board still matches the *schematic*. A footprint
added straight onto the PCB, a net renamed on one side only, or a pad dragged onto the
wrong net all pass ERC, DRC and `route-coverage` — `netlist-audit` compares node counts
against a committed baseline, not against the board.

**What it does.** Reduces both domains to `net -> {"REF.PAD", ...}` and diffs them per
direction: membership disagreements, schematic-only nets, board-only nets. Errors on
real divergence; *warns* when the only difference is a component that simply isn't
placed yet, so it stays usable mid-layout. A board with no netted pads reports one info
line instead of screaming.

**KiCad footgun it encodes.** Net names differ between the two exports in two ways.
The netlist strips the one leading `/` on sheet-scoped names while the board keeps it —
so the comparison strips exactly that, and *not* the leaf, since collapsing
`/video/SW` to `SW` would alias distinct same-named nets on different sheets and hide
the very drift this gate looks for. And KiCad escapes an interior `/` as `{slash}` in
`unconnected-(...)` pseudo-net names (`unconnected-(J201-JTDO/SWO-Pad8)` on one side vs
`...JTDO{slash}SWO-Pad8` on the other), which would otherwise diverge on every board;
those pseudo-nets are filtered out entirely.

### `kb commit-gate`

Runs footprint resolution → `erc-triage` → `netlist-audit` (if a baseline exists) and
aggregates one COMMIT ALLOWED / BLOCKED verdict. `--install-hook` wires it in as a
git pre-commit hook. Because documented false-positives live in `[[erc.allow]]`, any
REAL error here is by definition an *undocumented* regression.

### `kb ksir-sync`

**Problem.** A sheet with a `<name>.ksir` beside it is ksir-managed — the YAML is the
edit surface, the `.kicad_sch` compiled output. Hand-edit the sheet and forget `ksir
adopt --force`, and the next compile silently clobbers the hand edit.

**What it does.** Recompiles each adopted `.ksir` into a temp dir and fails on drift
against the checked-in sheet. Runs inside `kb audit` whenever `.ksir` files exist.

### `kb symbol-style [libs...]`

Audits `.kicad_sym` libraries against the house symbol style guide
(`skills/kicad-symbol-style/SKILL.md`): 50 mil grid on fields+pins, Reference 50 mil
regular, Value 40 mil italic, visible Description/Datasheet fields, no `unspecified`
pin types. Advisory by default (the guide is v0.1); `--strict` makes deviations fail.
`kp scaffold`-generated symbols pass this by construction.

### `kb sch-readability [sheet]`

An **objective** sheet-layout rule gate — grid alignment, sheet bounds, body
overlaps, power-symbol conventions. Deliberately *not* an aesthetic score:
calibration against a human-perfected vs. raw-generator golden pair showed geometric
metrics can't separate "clean" from "messy", so this checks only the rules that are
mechanically decidable and leaves aesthetics to humans.

---

# Datasheet-grounded checks — the layer above ERC

**Problem.** ERC passes boards that don't work. Nothing in a rule checker knows that
a 5 V rail is feeding a 3.6 V-abs-max input, that an LED's series resistor lets
through twice its rated current, that a net named `UART5_TX` landed on a pin whose
mux only offers `UART5_RX` — or that a write-control pin tied to ground leaves an
EEPROM writable by anything on the bus. All of that is in the datasheets, and nobody
re-reads 400 pages per part on every revision.

**The design.** A queryable **design graph** (components ↔ nets, with rail voltages
inferred from net names) is built from the shared netlist export; per-datasheet
**extracted constraints** (`constraints.json` inside the committed
`.datasheet-index/<slug>/`) supply pin tables, abs-max/recommended ratings, and typed
specs; three deterministic gates and one LLM reviewer consume both. Deterministic
checks are warn-biased — they *skip*, never guess, on missing data — and the reviewer
is bound by a code-enforced severity discipline. (The idea owes a debt to
[Pinscope](https://github.com/Faradworks/Pinscope); the implementation is original.)

### `kb cap-derating`

For every capacitor whose rated voltage is known (extracted constraints first, else
parsed from the value string — `"100nF 16V X7R"`), compares the worst-case operating
voltage across its nets against the rating derated per dielectric class
(`[derating]`; defaults ceramic 0.8, tantalum 0.5, electrolytic 0.8). Over the
absolute rating → error; inside the rating but outside policy → warn; unknown data →
counted in one info line.

```
$ kb cap-derating
== Capacitor derating ==
  · 50 capacitor(s) skipped — 48 without a known voltage rating, 2 without a known net voltage
  ✓ all 14 rated capacitor(s) within derating policy
  -> PASS  14 checked, 0 flagged, 50 skipped (no rating/voltage data)
```

### `kb led-current`

Resolves each LED's simple rail—R—LED—rail/gnd loop from the graph and checks
worst-case `I = (V − Vf) / R` against the rated forward current (error over rating,
warn under 10 % margin, error on a no-series-resistance loop). Anything ambiguous — a
driver IC on the net, an unknown drive rail, missing Vf/If — is skipped, not guessed.

### `kb pin-mux`

When a net *name* asserts a peripheral signal (`UART5_TX`, `/I2C0.SCL`) and it lands
on a pin whose alternate-function table exposes that peripheral but **not** that
signal, the assignment is physically unrealizable — a hard, context-free error.
Strictly a *feasibility* check, never a *direction* check, and nets where a peer
component exposes the same peripheral are skipped (inter-device links are ambiguous
by name). Needs extracted pin tables; with none ingested it reports one info line.

### `kb constraints` — the extraction store

```
$ kb constraints status            # coverage: BOM MPNs vs stored constraints
$ kb constraints seed              # no-LLM heuristic pin tables (from ingested text)
$ kb constraints extract [MPN]     # LLM extraction — pin tables from pinout-page
                                   # images, ratings from section text, page-cited
$ kb constraints show MPN
```

One `constraints.json` per ingested datasheet, next to its `index.json` — schema-
versioned, sha-keyed to the source PDF, MPN-family matched (`SN75LVDS83BDGG` finds
`sn75lvds83b`), and shareable across boards via `[constraints] extra_index_dirs`.
`extract` uses structured output with a strict schema and cites `source_pages` for
every field; re-runs skip anything current. A real run:

```
$ kb constraints extract M24C02
  extracted datasheetsm24c02: 8 pin(s), 5 abs-max row(s) [$0.368]
```

### `kb review` — the datasheet-grounded reviewer

Per-IC agentic review: the model gets the design graph, the extracted constraints,
and **budgeted** access to the ingested datasheet pages (text, or the rendered page
image when tables/figures matter — global + per-neighbor page budgets), and files
findings cited to pages. The severity rules are strict and enforced twice — in the
prompt, and again in code by a **downgrade-only** normalization pass:

- **error requires a concrete harm pathway** — the specific condition in *this*
  circuit, the datasheet limit with numbers, the resulting malfunction, and the page
  citation. Anything unverified is demoted to a warning prefixed `Unverified:`.
- an error citing a page the reviewer never actually read is demoted;
- direction is never flagged (only feasibility); deliberate design choices are
  info, not defects; duplicate/same-premise findings collapse.

Findings persist to the committed **`.cockpit/review.json`** (they're review
artifacts that travel with the design); per-run token/cost accounting goes to the
gitignored `.audit-cache/review-usage.jsonl`. The Cockpit's **Review tab** renders
the findings with page-cite chips that deep-link straight to the cited datasheet
page. A real finding from this bench's LVDS driver board (one IC, $0.64, 59 s):

```
! U3's WC (pin 7) is hard-tied to GND, leaving the EDID EEPROM permanently
  write-enabled with no protection against writes from the HDMI source.  [U3]
    Unverified: (3) — the malfunction is conditional, not guaranteed. (1) pin 7 (WC)
    is on GND. (2) Datasheet §2.4 (p7): writes are disabled only when WC is high.
    (4) Page 7. ...  [datasheetsm24c02 p7]
```

**Backends.** The default is **`claude-code`**: each IC review drives the local
`claude` CLI headlessly (`claude -p`) on your Claude subscription — no API key. A
per-IC stdio MCP server (`kicad_bench/review_mcp.py`) exposes the same tools with the
same budgets, `--strict-mcp-config` plus a tool allowlist keeps the run inside them,
and the parent process normalizes the findings afterward exactly as the API path
would. `--backend api` (or `[llm] backend = "api"`) uses the `anthropic` SDK instead
(`pip install -e '.[llm]'`, `ANTHROPIC_API_KEY`). `kb constraints extract` follows
the same dispatch (`claude -p --json-schema` structured output). Models are always
resolved explicitly — review `claude-opus-4-8`, extraction `claude-sonnet-5`,
overridable in `[llm]`.

```
$ kb review --ref U3                 # one IC
$ kb review                          # every U* IC with >= [review].min_pins pins
```

---

# Priority 2 — layout / DFM prep

### `kb netclass-coverage`

**Problem.** A net with no class silently inherits KiCad's Default width — harmless
for logic, dangerous for a 12 V power or panel-bias rail. KiCad resolves each net's
class from the project's `net_settings` only; "what the plan wants" and "what the
board actually does" are two different things.

**What it does.** Reports the **actual** per-net class as KiCad resolves it (a net is
assigned to *every* matching pattern's class), then cross-references the planning
config (`fab.config` domains) to surface the gap: nets the plan wants on a
power/bias/diff-pair class but the project leaves on Default. `--strict
--min-coverage F` fails below a coverage floor.

> Pattern matching note: KiCad treats a pattern as **both** glob (`*`/`?`) **and**
> regex simultaneously — so e.g. `X*` matches every net (regex zero-or-more). This
> tool reproduces that behaviour faithfully (it's a documented KiCad footgun).

### `kb netclass-sync`

Closes the gap `netclass-coverage` reports: generates a `net_settings` block from the
planning config — one class per domain/diff-pair with clearance/width/via/gap values,
plus an **exact** `netclass_assignments` map (no wildcard ambiguity) for nets that
actually exist. Default emits `output/netclass_settings.json`; `--write` merges into
the `.kicad_pro` after a `.bak` — never touching `.kicad_sch`/`.kicad_pcb`.

### `kb dfm-preflight`

One pre-layout gate separating hard blockers from context: *hard* — real ERC errors,
any symbol with more pins than its footprint has pads, PCB DRC errors; *info* —
missing 3D models, net-class coverage, whether the project's custom rules are
actually active.

> **DRC + custom rules (verified on KiCad 10.0.3).** `kicad-cli pcb drc` *does* apply
> custom design rules — but only from a `<board>.kicad_dru` sitting next to the board
> with a matching base name, and only if syntactically valid (a malformed DRU is
> silently dropped). Rules generated into `output/` are **not active** until copied
> there; `dfm-preflight` and `dru-guard` flag this explicitly.

### `kb stackup-sync`

Reads the fab config (layers, thickness, copper, `fab_min`) and diffs the recommended
Board Setup values against what the project declares. Emit-and-apply: `--emit` writes
`output/board_setup_guide.md`; it never writes the `.kicad_pcb`.

### `kb release-prep`

Gates fabrication export on: ERC triage clean, PCB DRC clean (if laid out), and no
`NEEDS-PN` placeholder left in the BOM. `--export` writes gerbers + drill + position
into `output/release/`.

### `kb dru-lint`

Static-checks a `.kicad_dru` (there is no `kicad-cli` validator, and KiCad drops a
malformed file *silently*): version header, balanced parens, constraint/severity
vocabulary, rules-without-constraints, and `hasNetclass()` advisories.

---

# Priority 3 — routed-geometry audits (read-only)

These read the actual track/via/zone geometry from the `.kicad_pcb` (via
`core/pcbgeom`, the one place that parses board copper) and check what `kicad-cli pcb
drc` cannot. All are read-only.

### `kb diffpair-audit`

For every pair in `fab.config` `diff_pairs`: len(P)/len(N), intra-pair skew,
inter-pair bus spread, as-routed width vs target, layer discipline, via count.
`--max-skew` / `--width-tol` / `--max-bus-spread` set budgets; `--strict` fails on
any flagged pair.

### `kb track-conformance`

Maps every routed net to its domain (from `fab.config`) and reports tracks/vias under
the domain's width floor, as a per-domain rollup. Respects copper *pours* — a poured
power net with no thin discrete tracks is conformant, not "thin."

### `kb route-coverage`

Counts KiCad's ratsnest (`unconnected_items` from DRC) per net, rolled up by
hierarchical block and net class, plus still-open diff pairs — a headless routing
dashboard. `--strict` fails while any routable net is open.

### `kb dru-guard`

Verifies the board-sibling DRU exists, is current vs a fresh generator run, passes
`dru-lint`, that the `.kicad_pro` classes still cover the config's domains/diff-pairs,
and that the stackup id is consistent.

---

# Datasheets — harvest, ingest, read cheaply

**Problem.** Datasheets are big PDFs. Searching them or pulling out a pinout by
re-parsing the PDF (or having an agent open it) is slow and burns tokens, and nothing
fetches them in the first place.

**What it does.** A PDF is opened (via poppler) **exactly once**, at `ingest`, which
extracts each kind of data into its own cheap file under `.datasheet-index/<slug>/`:

| File | Holds | Committed? |
|------|-------|-----------|
| `text/full.txt`, `text/pNNN.txt` | full + per-page text (for `search`) | yes |
| `index.json` | metadata, parsed TOC, section→page map, figure list | yes |
| `constraints.json` | extracted electrical constraints (see `kb constraints`) | yes |
| `figures/*.png` | pinout / typical-app / layout diagrams, pre-rendered | no |
| `registry.json` | part → datasheet provenance | yes |

Afterwards the read commands touch only those artifacts — never the PDF — so the
text/JSON path needs **no poppler** (works on a fresh clone / in CI).

```
$ kb datasheet fetch tps65150          # find + download + ingest one part's datasheet
$ kb datasheet fetch --all             # every part with a known URL
$ kb datasheet ingest --all            # (re)build the index for PDFs already on disk
$ kb datasheet search "soft-start"     # grep ingested TEXT — no PDF, no poppler
$ kb datasheet figures tps65150 --section pinout --package LQFP48
$ kb datasheet toc / locate / view     # navigate by the doc's own TOC
$ kb datasheet parts                   # BOM/work-list ⋈ datasheets — coverage + provenance
$ kb datasheet pins ap2112k --package SOT25 [--json]   # DRAFT scaffold pin table
```

**Fetch** resolves a URL in priority order — a `[datasheets]` override, a KiCad
symbol `Datasheet` field, a BOM `Datasheet` column, then `--url` — downloads via
stdlib `urllib`, validates the `%PDF` header to reject HTML interstitials, and
auto-ingests. Parts with no resolvable URL are *reported* (not failed) so an agent
can web-search them and re-run with `--url`. **Pins** is a best-effort extraction of
a pin table into scaffold-ready `[{number,name,etype}]` — a DRAFT to verify against
the rendered pinout figure; [`kicad-parts`](../kicad-parts)' `kp scaffold` consumes
it, and `kb constraints seed` builds its heuristic pin tables from the same
extractor.

---

# Live surfaces & utilities

### `kb sidecar`

The built-in, stdlib-only live dashboard: one self-contained dark page with the audit
verdict, a "fix next" list, and schematic/PCB/datasheet/parts tabs; re-audits when
the board file changes. On this bench it's been **superseded by the Cockpit**
(`kp cockpit` from [`kicad-parts`](../kicad-parts)) — a multi-board FastAPI/Vue
dashboard with the same audit plus the Review tab, SSE live updates, BOM/sourcing
views, and per-board notes. Both consume the same `board_state.py` engine, so the
sidecar remains the zero-dependency fallback.

### `kb stage`

A lock-aware job queue: queue edits while KiCad has the files open; they apply the
moment KiCad closes them. The safe write-path for automation around a live editor.

### `kb sch-live`

Live *schematic* summary from a running KiCad over the IPC API (shells out to the
separate `kb-ipc` add-on so `kb` stays stdlib-only). KiCad master (→ v11) serves the
eeschema IPC API; KiCad 9/10 expose pcbnew only.

### `kb lib-search`

Full-text search over every installed symbol/footprint library — "do I already have a
symbol for this?" without opening KiCad. Indexes `.kicad_sym` names, descriptions,
and keywords plus footprint libs into a queryable FTS index.

### `kb doctor`

Confirms `kicad-cli` is on PATH and reports which config will be used.

---

# Configuration (`kicad-bench.toml`)

One file holds all project specifics, so the tools stay generic. The config normally
lives in the design repo (`kb init` writes it); it can also live elsewhere and point
at the design repo via `project.root`.

```toml
[project]
root         = "/path/to/design-repo"      # optional; paths below resolve under it
root_sch     = "Proj/Proj.kicad_sch"
pcb          = "Proj/Proj.kicad_pcb"
fp_lib_table = "Proj/fp-lib-table"
bom          = "BOM.xlsx"

[contracts]
nets = "from:scripts/design_rules_config.json"   # inline list, or from a JSON/markdown source
# expected_nets = "expected_nets.json"           # {net: count} baseline for netlist-audit

[contracts.designators]                          # ranges expand inclusively
buck = ["U4", "R22-R29", "L3"]
general = ["R1-R12", "C1-C32"]                   # shared, allowed on any sheet

[[erc.allow]]                                    # known false-positives — reason REQUIRED
type = "power_pin_not_driven"
ref = "U5"
pin = "7"
reason = "power:GND pin ~0.47mm off anchor; visually connected."

[approved_parts]
csv = "approved_parts.csv"
allow_missing_mpn = false
unapproved = "warn"                              # or "error"

[derating]                                       # cap-derating factors (defaults shown)
ceramic = 0.8
tantalum = 0.5
electrolytic = 0.8

[constraints]                                    # extra constraint stores (cross-board)
extra_index_dirs = ["/path/to/shared/Datasheets/.datasheet-index"]

[llm]                                            # review + extraction backends
# backend = "claude-code"                        # default; "api" needs ANTHROPIC_API_KEY
# review_model = "claude-opus-4-8"               # defaults shown
# extract_model = "claude-sonnet-5"

[review]
# min_pins = 8                                   # ICs below this are skipped (use --ref)
# page_budget = 24                               # datasheet pages per IC review
# neighbor_budget = 8                            # of those, per *neighbor* datasheet

[fab]
config  = "scripts/design_rules_config.json"
stackup = "JLC04161H-7628"

[datasheets]                                     # optional URL overrides
TPS65150 = "https://www.ti.com/lit/ds/symlink/tps65150.pdf"
```

**Where contract data comes from.** `contracts.nets` can be an inline list or
`from:<file>` — a design-rules JSON (whose `nets`/`diff_pairs` enumerate every named
net) or a markdown doc scanned for `UPPER_SNAKE` net tokens — so the config never
duplicates (and drifts from) the source-of-truth.

# The ecosystem

kicad-bench is the **leaf package** of a small stack:

- [`kicad-parts`](../kicad-parts) (`kp`) — parts-library manager (catalog, symbol
  scaffolding, House library) **and the Cockpit** dashboard; depends on
  `kicad_bench` for the s-expr parser, kicad-cli wrapper, datasheet subsystem, and
  `board_state.py`. `kicad_bench` never imports `kicad_parts`.
- `kb-ipc` — the KiCad IPC add-on `kb sch-live` shells out to.
- `skills/` — SKILL.md wrappers (`kicad-quality-gate`, `kicad-layout-prep`,
  `kicad-product-workflow`, `kicad-symbol-style`) so Claude drives the `kb` tools
  with the same guardrails.
- `NATIVE_TOOLS.md` — the decision guide for KiCad's own automation surfaces
  (kicad-cli vs IPC vs file parsing).

# Layout

```
kicad_bench/main.py        `kb` CLI dispatch (every tool registers add_parser/run)
kicad_bench/audit.py       consolidated whole-repo audit (shares netlist + DRC runs)
kicad_bench/board_state.py per-board live-state engine (audit/preview/review caches)
kicad_bench/sidecar.py     stdlib live dashboard (superseded by the Cockpit)
kicad_bench/datasheet.py   fetch/ingest/search/figures/pins + the .datasheet-index
kicad_bench/constraints.py kb constraints CLI (status/seed/extract/show)
kicad_bench/review.py      kb review engine (both backends, normalize, persistence)
kicad_bench/review_mcp.py  per-IC stdio MCP server for the claude-code backend
kicad_bench/libsearch.py   FTS over installed libraries
kicad_bench/stage.py       lock-aware job queue
kicad_bench/sch_live.py    live schematic summary over IPC (via kb-ipc)
kicad_bench/scaffold.py    product-monorepo scaffolding      } Priority 0
kicad_bench/init.py        one-command board adoption        }
kicad_bench/release_freeze.py  frozen, tagged releases       }
kicad_bench/fab.py         fab-export seam (release-prep + release-freeze)
kicad_bench/bom_assembly.py    sch ⋈ pcb ⋈ BOM join (Cockpit BOM tab)
kicad_bench/core/          cli, config, report, sexp, schparse, pcbgeom, kicad10,
                           footprints, graph, partspec, conspec, review_prompt
kicad_bench/quality/       erc_triage, netlist_audit, block_review, approved_parts,
                           commit_gate, ksir_sync, symbol_style, sch_readability,
                           cap_derating, led_current, pin_mux
kicad_bench/layout/        netclass_coverage, netclass_sync, dfm_preflight,
                           stackup_sync, release_prep, dru_lint, diffpair_audit,
                           track_conformance, route_coverage, dru_guard
skills/                    SKILL.md wrappers
windows-onboarding/        coworker Windows setup kit
tests/                     pytest — pure logic with synthetic inputs, no kicad-cli
```
