# Open-source concept-mining plan

**Status:** research complete, execution not started.
**Audience:** an implementing agent (Fable) picking up work items one at a time.
**Scope:** techniques worth mining from the public KiCad/EDA open-source landscape into
this bench. Every item is filtered against what we *already* ship, so nothing here is a
rebuild — each is a specific, transferable concept plus a concrete landing site in our
module layout.

We have already mined **Pinscope** (datasheet-grounded pin checks) and **Trace** (the
schematic-IR compile step, now `ksir`). This document is the next round.

---

## How to use this document

Each work item is a self-contained ticket: **source to read → the concept → concrete
implementation → compliance check → acceptance criteria → effort.** Do them in tier
order (Tier 1 first); within a tier, lower item numbers set up later ones. Do **not**
batch-implement — land one gate, run its tests + `kb audit` on a real board, then take
the next. The reference table at the bottom has every candidate repo with stars, license,
and URL.

### Ground rules any implementation must respect (from `CLAUDE.md`)

These are hard constraints, not preferences. An item that would violate one is flagged.

- **Read-first is the product.** Quality/datasheet gates never write a design file.
  Layout tools write only into regenerable `output/` (documented exceptions:
  `netclass-sync --write`, `commit-gate --install-hook`). **Never** add a tool that edits
  `.kicad_sch` / `.kicad_pcb` — emit-and-apply instead.
- **Leaf package.** `kicad_bench` must never import `kicad_parts`. Cross-repo data flows by
  path/config, never by import.
- **Dependency-light.** Core = stdlib + `sexpdata` only. Anything heavier is an optional
  extra behind a guarded import with an actionable error (pattern: `[xlsx]`, `[llm]`).
- **Uniform gate anatomy.** A gate = pure logic function taking already-extracted data and
  returning a `core.report.Result` + `run(args)` (loads config, resolves inputs,
  `render_and_exit`) + `add_parser(sub)`. Register in `main.py:_TOOLS`. Model:
  `quality/approved_parts.py`.
- **Exit codes:** 0 pass, 1 fail, 2 setup error. Only `error`-severity findings fail;
  warnings never do. Warn-biased checks **skip, never guess** on missing data (aggregate
  skips into one info line).
- **Share the expensive ops.** New schematic gates accept `counts`/`pin_net`/graph as
  arguments; they do not re-export the netlist. `audit.run_all` does one netlist export +
  one DRC run and injects results into gates.
- **Tests are pure.** Gate logic is tested by calling the pure function with synthetic
  dicts/graphs; kicad-cli is monkeypatched; LLM clients are faked. No network, no KiCad.
  `python3 -m pytest tests/ -q` stays green.
- **The design graph** (`core/graph.py`) treats unknown rail voltage as `None`, never `0`;
  consumers must too.

### The already-owned baseline (do not rebuild)

Schematic gates (erc-triage, netlist-audit, block-review, approved-parts, symbol-style,
sch-readability); the datasheet-grounded layer (cap-derating, led-current, pin-mux,
constraint extraction, page-cited LLM review with downgrade-only severity); the design
graph; layout/DFM prep (netclass coverage/sync, dfm-preflight, stackup-sync, dru-lint);
routed-geometry audits (diffpair-audit, track-conformance, route-coverage, dru-guard); the
datasheet subsystem (fetch/ingest/search/figures/pins → committed index); kicad-parts
(catalog kanban, symbol scaffolding, BOM builder); ksir (YAML→`.kicad_sch`); the kipy IPC
read path; the Cockpit dashboard.

### Read these two peers in full before starting

Both are permissively licensed, so they are **code** mines, not just concept mines. Most
Tier 1/2 items below can lift their implementations rather than reimplement from scratch.

- **`aklofas/kicad-happy`** — MIT, Python, ~750★. AI-agent skills for KiCad: SPICE
  auto-testbenching, EMC pre-compliance, thermal, power-integrity, datasheet extract,
  sourcing. Our nearest sibling. https://github.com/aklofas/kicad-happy
- **`salitronic/eda-agent`** — Apache-2.0, Python, ~90★. 300+ EDA tools, Altium-primary
  with a KiCad IPC backend. Its `design_lint_report`, net-crossref ECO-drift, and
  IPC-standard calculators are directly liftable. https://github.com/salitronic/eda-agent

---

## Tier 1 — Adopt now: high-leverage, fits read-first + datasheet-grounded core

### T1.1 — Topology-computed rail voltages in the design graph

- **Source:** kicad-happy (power-integrity: Vout from feedback divider, ~65 Vref
  families); eda-agent (`design_add_part` divider math). Both permissive.
- **Concept.** Today `core/graph.py` infers rail voltage from **net names** and leaves the
  unknown as `None`. That is why cap-derating / led-current / review skip so much. These
  projects compute the **actual rail voltage from the feedback divider and regulator
  topology** (`Vout = Vref * (1 + Rtop/Rbot)`, with a Vref table keyed by regulator
  family). Rail voltage becomes *solved from the circuit* instead of *guessed from a name*.
- **Why it's #1.** It improves the substrate that four gates + the reviewer already
  consume. One change, system-wide accuracy lift.
- **Implementation.**
  - Extend `core/graph.py`: given a regulator component (identified via its extracted
    constraints / MPN family) and its feedback net, resolve the `Rtop`/`Rbot` divider from
    the graph and compute Vout. Add a `voltage_source: "computed"|"netname"|None`
    provenance field alongside the value (mirror the extraction `extracted_by` pattern).
  - Vref lookup: prefer the extracted `constraints.json` (a `vref` typed spec) over a
    small built-in family table; unknown Vref ⇒ stays `None` (skip, don't guess).
  - Consumers (`cap_derating`, `led_current`, `review`) already treat `None` as unknown —
    they automatically benefit with no change, but confirm each still skips gracefully.
- **Compliance.** Read-only; stdlib. No new dep.
- **Acceptance.** New `tests/test_graph.py` cases: a synthetic AMS1117/AP63203-style
  divider graph resolves the documented Vout; a divider with an unknown Vref resolves to
  `None`; a rail with no divider is unchanged. `cap-derating` on a real board shows more
  `checked` and fewer `skipped (no net voltage)`.
- **Effort:** M. **Depends on:** benefits from T2.1 (motif detection) but does not require
  it — a direct "regulator FB pin → two-resistor divider" walk is enough to start.

### T1.2 — Schematic ↔ PCB net cross-reference (ECO-drift gate)

- **Source:** eda-agent `obj_crossref_net` / `kicad_full_review` (Apache-2.0).
- **Concept.** Compare the **schematic net → pin set** against the **PCB net → pad set**
  and flag per-direction divergence with an `in_sync` verdict. `netlist-audit` checks node
  counts against a *baseline*; nothing checks that the **board still matches the
  schematic** after a post-layout ECO or a hand edit. This is a whole uncovered bug class.
- **Implementation.**
  - New gate `quality/eco_drift.py` (or `layout/` — it needs both sides; pick by where
    `audit.run_all` already has both the netlist export and `core/pcbgeom` parse).
  - Pure function `crossref(sch_netlist, pcb_pads) -> Result`: for each net, diff the pin
    set from the schematic netlist export against the pad set parsed from `.kicad_pcb`.
    Report nets present in one but not the other, and nets whose membership differs.
    Error on divergence; skip cleanly if the board isn't laid out yet (no pads) → one info
    line.
  - Accept both inputs as arguments so `audit.run_all` injects them (no re-export).
    Register in `main.py:_TOOLS`; add to the audit's "Layout & DFM" section.
- **Compliance.** Read-only; parses artifacts we already parse. Stdlib.
- **Acceptance.** Synthetic netlist + synthetic pad map: identical → PASS; a moved pad / a
  renamed net → ERROR naming the net and direction; empty pad map → info-line skip.
- **Effort:** M.

### T1.3 — Physics-grounded deterministic checks (IPC-2221 / IPC-2141 / field solver)

- **Source:** eda-agent calculators (pure-Python, EDA-agnostic: IPC-2221 trace-width,
  IPC-2141 microstrip/stripline + differential impedance, termination, length-match skew,
  thermal-via R=L/kA); `chschnell/RF2DFieldSolver` (2D-Laplace impedance solver for
  arbitrary cross-sections — the exact fallback). eda-agent is Apache-2.0.
- **Concept.** `track-conformance` checks width against a **config floor**. These check
  against **physics**: does this trace actually carry its net's current (IPC-2221)? Does
  this diff pair hit its target impedance on the stackup we already sync (IPC-2141)? This
  is the "grounded in the physics" sibling to our "grounded in the datasheet" layer.
- **Implementation.**
  - Add `core/ipc.py`: pure closed-form functions (current capacity ↔ width for a copper
    weight/temp-rise; microstrip/stripline single-ended + differential impedance from
    stackup geometry; thermal-via parallel resistance). Cite the IPC equation + section in
    each docstring (provenance discipline).
  - Wire into existing gates rather than adding many new commands:
    - `track-conformance`: add an optional physics mode — flag tracks narrower than the
      IPC-2221 width required for their net's current (current comes from config or, later,
      from the graph). Keep the existing config-floor mode as default.
    - `diffpair-audit` / `stackup-sync`: report computed impedance vs the target in
      `fab.config`; warn on deviation outside tolerance.
  - RF2DFieldSolver is a **later, optional** heavier path for odd cross-sections — behind
    an extra if adopted at all; the closed-form IPC equations cover the common cases with
    zero deps.
- **Compliance.** Read-only; the IPC equations are stdlib math (no dep). Field solver, if
  ever added, goes behind an extra.
- **Acceptance.** `tests/test_ipc.py`: known IPC-2221/2141 worked examples match within
  tolerance; a 0.2 mm trace on a 3 A net flags; a correctly-sized trace passes; impedance
  of a documented JLC04161H stackup matches the fab's stated target.
- **Effort:** M (equations) / L (field solver, optional).

### T1.4 — Visual git-diff review surface for the Cockpit

- **Source:** `leoheck/kiri` (~690★), `Gasman2014/KiCad-Diff` (~290★), plotgitsch. Mine
  the pipeline concept; check each project's license before lifting code (kiri is a shell
  orchestrator around `kicad-cli`/plotgitsch).
- **Concept.** Render **schematic + layout diffs between two git revisions** as images in
  a web view. We are read-first, git-centric, and already run the Cockpit + `kicad-cli`
  plotting. A "what changed since last commit / since the frozen release" visual tab is
  high review value and reuses machinery we own.
- **Implementation.**
  - Add a `board_state.py` helper that, given two git refs, checks out each board file to a
    temp dir, plots both via `kicad-cli`, and produces an image diff (overlay or
    side-by-side). Regenerable output only (gitignored cache, like figures).
  - Surface as a Cockpit Review sub-tab (kicad-parts consumes `board_state.py`; expose the
    diff there — data flows by the shared engine, not by import). The stdlib `kb sidecar`
    can render a minimal version too.
- **Compliance.** Read-only (checks out into temp dirs; never mutates working tree). Plot
  cache is gitignored/regenerable.
- **Acceptance.** Given a repo with two commits touching a board, the helper emits a diff
  image and a changed-nets/changed-footprints summary; identical revisions → "no change."
- **Effort:** M–L (the Cockpit UI is the long pole).

---

## Tier 2 — Strong: a new module family or one optional dependency

### T2.1 — Canonical-motif detection (subgraph isomorphism)

- **Source:** eda-agent (VF2 detects bypass cap, voltage divider, feedback divider, LC
  filter, crystal). Apache-2.0.
- **Concept.** Pattern-match over the design graph to *recognize* sub-circuits. This is
  **enabling tech**: it is what lets T1.1 find the feedback divider, T2.2 auto-build a
  testbench, and the reviewer ground its findings ("U4 is a buck; here is its comp
  network").
- **Implementation.** `core/motifs.py` over `core/graph.py`: a small library of motif
  templates matched by a lightweight subgraph search (full VF2 is overkill for these tiny
  motifs — a hand-rolled matcher on component roles + net degree is enough and keeps it
  stdlib). Return matched motifs with their component/net bindings.
- **Compliance.** Read-only; stdlib.
- **Acceptance.** Synthetic graphs: a two-resistor divider off an FB pin matches
  `feedback_divider` with correct `Rtop`/`Rbot` bindings; an RC pair matches `rc_lowpass`;
  noise (unrelated Rs) does not false-match.
- **Effort:** M. **Unlocks:** T1.1 (cleaner), T2.2, better review grounding.

### T2.2 — SPICE auto-testbench verification (optional extra)

- **Source:** kicad-happy (motif → auto-generated testbench → ngspice; Monte-Carlo N=100
  with sensitivity ranking), MIT. `PySpice` (KiCadTools computes netlists from `.kicad_sch`
  directly).
- **Concept.** Simulation is a genuine capability gap. For a detected motif (T2.1),
  auto-generate a testbench, run ngspice, and report pass/warn/fail. Monte-Carlo tolerance
  analysis pairs naturally with our derating checks.
- **Implementation.** New engine `spice.py` + `[spice]` extra (ngspice/PySpice behind a
  guarded import with an actionable error, per the dependency rule). Start with two motifs
  (RC/LC filter cutoff, divider ratio) to prove the seam before breadth.
- **Compliance.** Read-only (emits testbenches into regenerable output). Heavy dep is an
  extra.
- **Acceptance.** With the extra installed, an RC-lowpass motif produces a testbench whose
  simulated −3 dB point matches the analytic value within tolerance; without the extra, the
  command prints the actionable install hint and exits 2.
- **Effort:** L.

### T2.3 — EMC / power-integrity pre-compliance rule engine

- **Source:** kicad-happy (44 geometric+analytical checks: ground-plane voids, diff-pair
  skew ≤25 ps, decoupling-cap-to-load distance, unfiltered I/O, PDN impedance; risk score
  + probe-point test plan). MIT; formulas cite Ott/Paul/Bogatin.
- **Concept.** A new read-only **geometric** category grounded in published formulas. Every
  check is decidable from geometry we already parse via `core/pcbgeom`.
- **Implementation.** A `layout/emc_*` family (or one `layout/emc_preflight.py` with
  sub-checks), consuming the parsed board geometry + the graph. Warn-biased: skip on
  missing data. Start with the three highest-value checks (decoupling distance, diff-pair
  skew — already partly in diffpair-audit, ground-plane void) before the full 44.
- **Compliance.** Read-only; stdlib math.
- **Acceptance.** Synthetic board geometry: a decoupling cap placed far from its IC pin
  warns; a stitched ground plane passes; a void under a fast net warns.
- **Effort:** L (full engine) / M (first three checks).

### T2.4 — Local JLCPCB/LCSC parts DB + lifecycle, and LCSC→KiCad scaffolding

- **Source:** `Bouni/kicad-jlcpcb-tools` (~2k★ — mirrors the full JLC/LCSC parts DB to
  local SQLite: offline parametric search, basic/extended flag, stock);
  `uPesy/easyeda2kicad.py` (~1.5k★, generates symbol+footprint+3D from any LCSC part
  number); kicad-happy EOL/NRND/obsolescence alerts. **These land in `kicad-parts`, not
  `kicad_bench`** (leaf-package rule).
- **Concept.** (a) A local parametric DB gives `approved-parts` and the catalog real
  sourcing/EOL data offline. (b) easyeda2kicad is a far faster scaffolding path than
  pin-table entry for jellybean parts — pull the symbol/footprint, then `kb symbol-style`
  audits it.
- **Implementation.** In kicad-parts: an importer that mirrors the JLC/LCSC DB to a local
  store (same atomic-write, no-DB convention as the catalog); an easyeda2kicad-backed
  "scaffold from LCSC PN" path feeding the existing scaffold pipeline. In kicad_bench:
  extend the `approved-parts` policy to consume an EOL/lifecycle field if present (still
  config-driven; the gate stays generic).
- **Compliance.** kicad-parts owns the network/DB; kicad_bench only reads a lifecycle
  field via config. Keep the leaf boundary intact.
- **Acceptance.** kicad-parts: an LCSC PN scaffolds a symbol+footprint that passes
  `kb symbol-style`. kicad_bench: `approved-parts` flags an EOL part when policy says so,
  skips when the field is absent.
- **Effort:** L (spans both repos).

### T2.5 — Convention-inference for symbol/footprint lint

- **Source:** eda-agent `lib_audit_footprint_policies` (infers each convention by
  **majority vote across the library**, flags deviations expected-vs-actual). Apache-2.0.
- **Concept.** `symbol-style` checks against a *fixed* v0.1 guide. Learn the house
  convention from the library itself, then flag outliers — self-calibrating, and it kills
  the "whose rule is this" friction on a gate we already flag as provisional.
- **Implementation.** Add an inference mode to `quality/symbol_style.py`: compute the
  majority value per style dimension across the target libs, then report deviations from
  the inferred norm (alongside, or instead of, the fixed guide under a flag). Keep the
  fixed guide as the default so behavior is unchanged unless opted in.
- **Compliance.** Read-only; stdlib.
- **Acceptance.** A synthetic lib where 9/10 symbols use 50-mil Reference text flags the
  10th; a lib with no majority reports "no inferable convention" rather than guessing.
- **Effort:** M.

---

## Tier 3 — Mine a concept, don't adopt wholesale

- **T3.1 Datasheet quality-scoring + regression corpus** (kicad-happy, MIT). A
  five-dimension trust rubric on extracted datasheet data (extends our `extracted_by`
  provenance with a confidence score), and a large open-source-board regression corpus
  methodology — a model for hardening extraction and gates against real boards beyond
  synthetic tests. *Adopt:* a confidence score on extracted constraints; a curated
  handful of real adopted repos as an integration-test corpus.
- **T3.2 Parser patterns for ksir + the sexp layer.** `psychogenic/kicad-skip` (~220★) —
  object-path/dotted addressing into `.kicad_sch` (directly relevant to ksir's "one-line
  semantic change = one-line diff" goal). `mvnmgrx/kiutils` (MIT, ~130★) — dataclass
  round-trip with SCM-friendly stable formatting (relevant to keeping ksir compiles
  diff-clean). *Adopt:* the addressing ergonomics and stable-serialization ideas; do not
  swap out our `core/sexp`.
- **T3.3 KiBot declarative pipeline** (`INTI-CMNB/KiBot`, **GPL-3.0 → concept-only**). The
  de-facto declarative YAML preflight/outputs pipeline for hardware CI. *Adopt:* the
  config-schema *shape* for our fab/release outputs. Do **not** vendor code (license).
- **T3.4 kicanvas** (`theacodes`, MIT, ~1.1k★). Renders `.kicad_sch`/`.kicad_pcb`
  client-side in WebGL. *Adopt:* Cockpit previews without server-side `kicad-cli` plotting.
- **T3.5 InteractiveHtmlBom** (MIT, ~4.5k★). Cross-probe BOM↔board render. *Adopt:* enrich
  the Cockpit BOM tab.
- **T3.6 Schematic-as-code peers** (validate ksir + two ideas): `atopile` (MIT, ~3.5k★) —
  constraint solving + parametric part-picking + `assert` with units/tolerances; the
  assertion-with-physical-units idea is mineable into the review layer. `tscircuit` (MIT,
  ~2.3k★) — `circuit-json`, an open interchange IR (a reference point for ksir's format).
  `SKiDL` (MIT, ~1.6k★) — ERC expressed as Python.
- **T3.7 KiCost** (~620★). Multi-distributor cost rollup for the BOM builder.

---

## Tier 4 — Concept-only / out of scope for a read-first core

- **Autorouting:** `freerouting` (GPL/AGPL, ~1.8k★), KiCadRoutingTools (Rust A*),
  `tscircuit/autorouting` (benchmark set). Heavy and write-heavy; at most a *hand-off*
  target from `route-coverage`, never embedded.
- **Auto-placement / authoring:** eda-agent's motif-composer placement (VF2 + Sugiyama +
  rail consolidation), atopile's `.kicad_pcb` generation. Clever, but they **write design
  files** — violates read-first. Park as a future *emit-and-apply* idea only.
- **Niche rendering/RF:** `PcbDraw`, `pcb2blender`, `RF-tools-KiCAD`. Useful, off the
  critical path.

---

## If only five things get done

1. **T1.1 Topology-computed rail voltages** — biggest multiplier; upgrades four gates +
   the reviewer at once.
2. **T1.2 Sch↔PCB ECO-drift gate** — new bug class, pure logic, perfect anatomy fit.
3. **T1.3 IPC-2221/2141 physics checks** — "grounded in physics" companion to the
   datasheet layer, zero new deps.
4. **T1.4 Visual git-diff Cockpit tab** — high review value, reuses `kicad-cli`.
5. **Read kicad-happy + eda-agent in full** — MIT/Apache peers whose SPICE, EMC, thermal,
   and calculator code can be lifted directly instead of reimplemented.

---

## Licensing guidance

- **Concepts are free to mine** regardless of license (this is how Pinscope and Trace were
  adopted — clean-room reimplementation of the idea).
- **Lifting actual code** is fine only from permissive licenses: **MIT** (kicad-happy,
  easyeda2kicad.py, kiutils, kicanvas, InteractiveHtmlBom, atopile, tscircuit, SKiDL) and
  **Apache-2.0** (eda-agent). Preserve their license/attribution if you copy code.
- **GPL/AGPL** (KiBot, freerouting) are **concept-only** — do not vendor their code into
  this stack. Reimplement the idea clean-room if you want it.
- When in doubt, reimplement clean-room; the concepts, not the code, are what matter here.

---

## Candidate reference table

| Repo | ~Stars | License | Tier | The transferable concept |
|------|-------:|---------|:----:|--------------------------|
| aklofas/kicad-happy | 750 | MIT | 1/2 | Vout-from-divider, SPICE auto-TB, EMC engine, thermal, quality-scoring |
| salitronic/eda-agent | 90 | Apache-2.0 | 1/2 | Sch↔PCB crossref, 31-check lint, IPC calculators, motif detection, convention-inference lint |
| leoheck/kiri | 690 | (check) | 1 | Visual git-diff of schematic + layout across revisions |
| Gasman2014/KiCad-Diff | 290 | (check) | 1 | Image diff between layout revisions |
| chschnell/RF2DFieldSolver | — | (check) | 1 | 2D-Laplace impedance solver for arbitrary cross-sections |
| Bouni/kicad-jlcpcb-tools | 2000 | (check) | 2 | Local JLC/LCSC parts DB mirror (offline parametric + stock + basic/extended) |
| uPesy/easyeda2kicad.py | 1540 | MIT | 2 | Symbol+footprint+3D generation from an LCSC part number |
| PySpice | — | (check) | 2 | ngspice binding; netlist-from-`.kicad_sch` (KiCadTools) |
| psychogenic/kicad-skip | 220 | (check) | 3 | Object-path/dotted addressing into `.kicad_sch` |
| mvnmgrx/kiutils | 130 | MIT | 3 | Dataclass round-trip, SCM-friendly stable serialization |
| INTI-CMNB/KiBot | 2000+ | GPL-3.0 | 3 | Declarative YAML preflight/outputs pipeline (concept-only) |
| theacodes/kicanvas | 1100 | MIT | 3 | Client-side WebGL rendering of `.kicad_sch`/`.kicad_pcb` |
| openscopeproject/InteractiveHtmlBom | 4500 | MIT | 3 | Cross-probe BOM↔board render |
| atopile/atopile | 3500 | MIT | 3 | Constraint solving, parametric picking, `assert` with units/tolerances |
| tscircuit/tscircuit | 2300 | MIT | 3 | `circuit-json` open interchange IR |
| devbisme/skidl | 1600 | MIT | 3 | ERC expressed in the host language (Python) |
| hildogjr/KiCost | 620 | (check) | 3 | Multi-distributor cost rollup |
| freerouting/freerouting | 1800 | GPL/AGPL | 4 | Autorouter (hand-off target only) |
| yaqwsx/KiKit | 1960 | (check) | 4 | Panelization/automation (adjacent) |

*(Stars are approximate as of the July 2026 research pass. Entries marked "(check)" need a
license confirmation before any code is lifted; the concept is mineable regardless.)*
