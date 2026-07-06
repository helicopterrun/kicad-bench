# Merging `kicad-product-workflow` into kicad-bench — design & concept map

**Status:** design / mapping only — no implementation yet. This document is the
blueprint for folding the older `kicad-product-workflow` skill's concepts into
kicad-bench. It maps every concept to a concrete `kb` command or config section and
records the architectural decisions, so the code can land incrementally in later
passes without re-litigating the design.

## Why

Two KiCad repos exist under `helicopterrun`:

- **kicad-bench** (this repo) — the modern system. A stdlib-only, config-driven `kb`
  CLI with a hard **read-first boundary** (it never edits `.kicad_sch`/`.kicad_pcb`), a
  uniform `Result`/exit-code contract (`0` pass / `1` fail / `2` setup), `add_parser`/
  `run` plug-in modules, a live `sidecar` dashboard, a datasheet subsystem, and
  `init`/`stage` orchestration. Its gates cover schematic quality (Priority 1),
  DFM/layout prep (Priority 2), and routed-geometry audits (Priority 3). It is
  **single-board**: one `kicad-bench.toml` resolves one `root_sch` / `pcb` / `bom`.

- **[kicad-product-workflow](https://github.com/helicopterrun/kicad-product-workflow)**
  — older, but with real merit. A Claude **skill** that encodes an end-to-end
  **product lifecycle** — scaffold → review → freeze release — over a **multi-board
  product monorepo** (`hardware/<board>/`, `firmware/`, `mechanical/`, `releases/`),
  with **approved-parts governance**, a **regeneratable-vs-frozen** invariant, and
  **multi-board CI** with maturity-gated variants.

The two are complementary, not competing. kicad-bench is a superb *design-quality
engine* but has **no product lifecycle**: no scaffolding, no multi-board model, no
release freeze, no parts governance, no CI templates. kicad-product-workflow has
exactly that lifecycle, but its implementation is partly stubbed (missing
`references/*`, its scaffolder doesn't copy CI, `check_approved_parts.py` is a v0.1
stub). The plan is to keep kicad-bench as the surviving system and **port the
workflow repo's proven concepts into it**, adapted to kicad-bench's architecture.

Two decisions frame everything below:

- **Product-aware config** — extend `kicad-bench.toml` with optional `[product]` /
  `[[boards]]` sections. Single-board stays the default; existing configs keep working.
- **kicad-cli-native fab export now, KiBot as an optional backend hook later.**

## 1. Principles preserved (non-negotiable)

The merge must not weaken any of kicad-bench's invariants. Every ported concept is
designed around them:

- **Read-first boundary stays intact.** The lifecycle commands *do* write, but only to
  **new/scaffold files** (a product that doesn't exist yet) and the **frozen
  `releases/` dir** (plus git tags) — never to an existing `.kicad_sch`/`.kicad_pcb`.
  They form a new **Priority-0 / lifecycle** tier whose writes are as tightly bounded
  as the regenerable `output/` dir is today. See §5 for the per-command audit.
- **Uniform `Result`/exit-code contract.** Every new gate returns a
  `core.report.Result` and maps to `0`/`1`/`2` exactly like the existing tools, so the
  new commands compose into `kb audit`, `kb commit-gate`, CI, and git hooks unchanged.
- **`add_parser`/`run` plug-in convention.** Each new command is a module exposing
  `add_parser(sub)` + `run(args) -> int`, registered in the `_TOOLS` list in
  `kicad_bench/main.py`. Adding a command is adding a module and one list entry.
- **Stdlib-only core.** No new hard dependencies. External engines are shelled out
  through a single choke-point (`core/cli.py` for kicad-cli today; a `FabBackend` seam
  for an optional KiBot later — see §4).
- **Justified suppression / plan-vs-reality.** New gates follow the same discipline the
  ERC allow-rules already model: suppressions require a written reason, and tools
  report what KiCad/the parts list *actually* say versus what the plan wants.

## 2. Concept → command mapping

This is the heart of the design. Each row is a concept from the workflow repo mapped to
its kicad-bench home, with the existing code it reuses.

| Workflow concept (old repo) | Merged into kicad-bench as | Reuses / notes |
|---|---|---|
| Multi-board product monorepo (`hardware/<board>/`, `firmware/`, `mechanical/`, `releases/`) | New `[product]` + `[[boards]]` config sections; `Config` gains an optional `boards: list[BoardConfig]` | Extend `core/config.py`. Single-board configs auto-wrap into one implicit board, so nothing breaks (§3). |
| `scripts/new-kicad-product.sh` scaffolder | `kb scaffold <product> [boards...]` — a new **Priority-0** command | Port bash → Python (stdlib). Keep kebab-case name validation, compliance-aware doc stubs (FCC Part 15 / IEC 61010 / ESD), the KiCad-aware `.gitignore`, and the per-board fab-notes README. **Also** write a `kicad-bench.toml` + `.kicad-bench-config` pointer + CI workflow (fixes the old scaffolder's documented gap). Creates new files only → read-first safe. |
| `scripts/freeze-release.sh` | `kb release-freeze <version>` — extends the existing `release-prep` | Reuse `release_prep.readiness()` (ERC + DRC + BOM gating). Add: clean-tree check, per-board loop, full fab package written to `releases/<version>/<board>/` (frozen, committed), a `.rpt`-per-board record, an annotated git tag. Today `release-prep --export` writes the gitignored `output/release/`; freeze writes the committed `releases/`. |
| KiBot fab package (gerber / drill / **IPC-D-356 / STEP / ibom / pick-and-place / BOM-xlsx**) | kicad-cli-native export now (gerbers, drill, position, **+ STEP, + schematic/board PDFs**); **KiBot as an optional backend later** | Extend `release_prep._export()` behind a `FabBackend` seam (§4). kicad-cli cannot emit interactive-HTML BOM, IPC-D-356, or a sourced BOM-xlsx — those are documented as **KiBot-phase** artifacts, added when the optional backend lands. |
| `scripts/check_approved_parts.py` + `approved_parts.csv` | `kb approved-parts` — a new **Priority-1** gate — plus an `[approved_parts]` config section | Reuse `core/schparse.py` for MPN/property extraction (it already reads symbol `Reference`/`Value`/`Datasheet` properties). The old script's `sexpdata`→regex fallback collapses to a single stdlib regex path. Emits `Result`/`Finding`s: missing MPN = error (configurable), unapproved MPN = warn→error (configurable). Skips `#PWR`/`#FLG`/`TP`/`FID`/`H` refs, exactly as the original. |
| Maturity-gated KiBot variants (`DRAFT`/`PRELIMINARY`/`CHECKED`/`RELEASED`, `dev`→`main`→`tag`) | A documented **maturity ladder** onto existing kb gates: DRAFT = no gate, CHECKED = `kb audit` + `kb approved-parts`, RELEASED = `kb release-freeze` | Pure mapping — no new variant engine. The git ref selects which kb gate CI runs; the "variant" is the gate stringency, not a separate output config. |
| Multi-board CI (`hardware-ci.yml`: discover matrix → erc/drc → PR review → GitHub Release → commit-back) | New CI template(s) under a `templates/` dir that run `kb audit` + `kb approved-parts` per discovered board; the release job runs `kb release-freeze` on a `v*` tag | Adapt the old YAML: keep the `discover` board-matrix, GitHub Release creation, and `releases/<tag>/` commit-back; swap raw `kicad-cli` steps for `kb` so local and CI share one gate. |
| Regeneratable-vs-frozen invariant | Formalized: `hardware/*/output/` gitignored & regenerable; `releases/v*/` committed & frozen; CI enforces the split | Already half-present (`output/` is gitignored today). The scaffold `.gitignore` + the CI encode the whole invariant. |
| The product-lifecycle Claude skill (`SKILL.md`) | New `skills/kicad-product-workflow/SKILL.md` | Mirror the three existing thin SKILL.md wrappers (`kicad-quality-gate`, `kicad-layout-prep`, `kicad-symbol-style`). Drive scaffold/review/freeze through `kb` with the same guardrails, and preserve the old skill's precise "when **not** to use this skill" section. |

## 3. Product-aware config schema

The single-board `Config` (`core/config.py`) grows an optional board list. The design
keeps **full backward compatibility**: a config with no `[[boards]]` array behaves
exactly as today.

### Extended `kicad-bench.toml` (multi-board)

```toml
[product]
name       = "wildlife-cam"
firmware   = "firmware"          # optional, relative to product root
mechanical = "mechanical"        # optional
releases   = "releases"          # optional; default "releases"

[[boards]]
name         = "main-board"
root_sch     = "hardware/main-board/main-board.kicad_sch"
pcb          = "hardware/main-board/main-board.kicad_pcb"
fp_lib_table = "hardware/main-board/fp-lib-table"
bom          = "hardware/main-board/output/bom.xlsx"
# per-board overrides (fall back to top-level [contracts]/[fab] if omitted)
[boards.fab]
config  = "hardware/main-board/design_rules.json"
stackup = "JLC04161H-7628"

[[boards]]
name     = "sensor-board"
root_sch = "hardware/sensor-board/sensor-board.kicad_sch"
pcb      = "hardware/sensor-board/sensor-board.kicad_pcb"

[approved_parts]
csv               = "approved_parts.csv"   # relative to product root
allow_missing_mpn = false                   # missing MPN → error when false
unapproved        = "warn"                   # "warn" | "error" for MPN-not-in-list
```

### `core/config.py` changes

- Add a `BoardConfig` dataclass: `name`, `root_sch`, `pcb`, `fp_lib_table`, `bom`, and
  optional per-board `nets` / `designators` / `expected_nets` / `fab_config` / `stackup`
  that fall back to the top-level `[contracts]`/`[fab]` when unset.
- Add `Config.boards: list[BoardConfig]`. In `load()`:
  - If `[[boards]]` is present, build one `BoardConfig` per entry.
  - Otherwise, synthesize **one implicit board** from the existing top-level
    `[project]` keys (`root_sch`/`pcb`/`fp_lib_table`/`bom`). This is the compatibility
    shim: today's single-board configs get a one-element `boards` list, and every
    existing command keeps reading `cfg.root_sch` etc. unchanged.
- Per-board commands (`audit`, `erc-triage`, `release-prep`, `approved-parts`, …) gain a
  `--board NAME` selector; with no `[[boards]]` it's a no-op, with boards it iterates or
  targets one.
- Add an `ApprovedParts` config object (`csv` path resolved against product root,
  `allow_missing_mpn`, `unapproved` policy).

Board discovery for scaffolded products mirrors the old repo's shared contract —
`hardware/*/*.kicad_pro` — so `kb scaffold`, the CI matrix, and `kb release-freeze`
all agree on what a "board" is.

## 4. Fab-backend seam (native now, KiBot later)

`release_prep._export()` today runs three kicad-cli jobs (gerbers, drill, position).
Generalize it behind a tiny protocol so a richer backend can be added without touching
callers:

```python
class FabBackend(Protocol):
    name: str
    def available(self) -> bool: ...                       # e.g. tool on PATH
    def export(self, board: BoardConfig, out_dir: Path) -> Result: ...
```

- **`KicadCliBackend`** (default, ships now) — the existing `_export` logic plus STEP
  (`kicad-cli pcb export step`) and schematic/board PDFs (`kicad-cli sch export pdf`,
  `kicad-cli pcb export pdf`). Zero new dependencies.
- **`KibotBackend`** (documented, not built now) — detected on PATH like kicad-cli; when
  present, produces the artifacts kicad-cli can't: interactive HTML BOM (ibom),
  IPC-D-356 netlist, and a sourced BOM-xlsx. Selected via a future
  `[fab] backend = "kibot"` or `--backend kibot`.

### Artifact coverage

| Artifact | KicadCliBackend (now) | KibotBackend (later) |
|---|---|---|
| Gerber X2 (zip) | ✓ | ✓ |
| Excellon drill (PTH/NPTH) | ✓ | ✓ |
| Position / centroid CSV | ✓ | ✓ |
| STEP 3D model | ✓ | ✓ |
| Schematic + board PDFs | ✓ | ✓ |
| Interactive HTML BOM (ibom) | — | ✓ |
| IPC-D-356 netlist | — | ✓ |
| Sourced BOM XLSX (MPN/stock/price) | — | ✓ |

No KiBot code lands in this pass — only the seam and this coverage table, so the later
addition is a drop-in.

## 5. Read/write boundary audit

Every new command's writes, confirming the read-first guarantee for existing design
files is never broken:

| Command | Writes | Touches existing `.kicad_sch`/`.kicad_pcb`? |
|---|---|---|
| `kb scaffold` | A brand-new product tree (dirs, doc stubs, `.gitignore`, `kicad-bench.toml`, `.kicad-bench-config`, CI). All files are new. | No — the design files don't exist yet; scaffold explicitly does **not** create `.kicad_pro/.kicad_sch/.kicad_pcb` (KiCad's format is version-dependent; one manual save from the UI, per the old repo's rule). |
| `kb approved-parts` | Nothing (read-only gate). | No |
| `kb release-freeze` | `releases/<version>/<board>/` fab artifacts + per-board `.rpt`, a release commit, and an annotated git tag. | No — reads design, writes only the frozen `releases/` record + tag. |
| `kb audit` / gates over multi-board | Nothing (read-only). | No |

This matches the existing tiers: Priority 1–3 are read-only; the one legacy exception
(`commit-gate --install-hook`) writes a single git hook. The new Priority-0 lifecycle
tier adds "new-files-and-`releases/`-only" writers — a bounded, auditable extension of
the same discipline, not a hole in it.

## 6. Migration & relationship to the old repo

Once the ports land, `kicad-product-workflow` is **superseded** by kicad-bench:

- Its `SKILL.md` concepts live in `skills/kicad-product-workflow/SKILL.md`.
- Its scripts become first-class `kb` subcommands (`scaffold`, `release-freeze`,
  `approved-parts`) sharing kicad-bench's core, tests, and guardrails.
- Its CI template is adapted into kicad-bench's `templates/`.

**Intentionally dropped:** the committed `.DS_Store` noise, and the unshipped
`references/*.md` / `.kibot.yaml` stubs (their intent is captured here and in the ported
commands instead).

**Upgraded in the port:**
- The scaffolder now actually wires CI **and** a kicad-bench config into the new product
  (the old one only claimed to).
- Approved-parts becomes a first-class, composable gate emitting `Result`/`Finding`s
  (the old one was a standalone v0.1 script), with a clear path to the old script's
  documented v1.0 TODOs (nearest-approved-alternative suggestion, `lifecycle_status`
  EOL/NRND enforcement, glob patterns in the CSV).
- Fab export gains a native, dependency-free path and a clean KiBot extension point.

After the port, the old repo should get a README note pointing at kicad-bench and be
archived.

## 7. Staged implementation roadmap

Each phase is independently shippable and testable (kicad-bench's `tests/` are
pure-logic pytest, no kicad-cli needed):

1. **Config foundation** — `[product]` / `[[boards]]` + `BoardConfig` + the implicit
   single-board shim + `[approved_parts]`. No behavior change for existing single-board
   configs. Tests: implicit-board synthesis, multi-board parse, per-board fallback.
2. **`kb approved-parts`** — the Priority-1 gate + config wiring. Tests: MPN extraction,
   allow-list matching (incl. `alt_mpn_1/2`), missing/unapproved policy, ref-skip rules.
3. **`kb scaffold`** — the Priority-0 command writing tree + docs + `.gitignore` +
   config + pointer + CI. Tests: name validation, tree shape, generated config loads,
   idempotence/refuse-overwrite.
4. **`kb release-freeze`** — extend `release_prep` (per-board loop, clean-tree check,
   `releases/<ver>/` write, tag) behind the `FabBackend` seam with `KicadCliBackend`.
   Tests: gating reuse, per-board iteration, backend dispatch (mock kicad-cli).
5. **CI templates + `skills/kicad-product-workflow/SKILL.md`** — the maturity-ladder CI
   and the thin skill wrapper.
6. **(Later) `KibotBackend`** — optional, PATH-detected, for ibom/IPC-356/BOM-xlsx.

## Critical files (targets for the later passes)

- `kicad_bench/core/config.py` — `Config`/`AllowRule`; where `[product]`/`[[boards]]`/
  `BoardConfig`/`[approved_parts]` attach.
- `kicad_bench/layout/release_prep.py` — `readiness()` + `_export()`; the freeze base and
  the `FabBackend` seam.
- `kicad_bench/main.py` — the `_TOOLS` registry and `add_parser`/`run` convention for
  the new commands.
- `kicad_bench/core/report.py` — the `Result`/`Finding`/exit-code contract every gate
  reuses.
- `kicad_bench/core/schparse.py` — symbol-property extraction reused by `approved-parts`.
- `skills/kicad-quality-gate/SKILL.md` — the style template for the new skill.
- Source of ported logic (old repo): `scripts/new-kicad-product.sh`,
  `scripts/freeze-release.sh`, `scripts/check_approved_parts.py`,
  `templates/.github/workflows/hardware-ci.yml`, `templates/approved_parts.csv`,
  `SKILL.md`.
