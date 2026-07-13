# Working *with* KiCad — which native tool to reach for

KiCad 10 exposes several automation surfaces. Using the wrong one is how the last
sprint turned into a fight with the file format. This is the decision guide: pick the
tool that matches the job, and prefer KiCad's own interfaces over hand-parsing files.

## TL;DR decision table

| You want to… | Use | Why |
|---|---|---|
| Run ERC / DRC / netlist / export gerbers / **render a sheet or board to SVG/PDF/PNG** | **`kicad-cli`** | Headless, both editors, always current. The **only** export/plot path (IPC can't export in 9/10). |
| Read or **edit the live PCB** without reopening it | **IPC API (kipy)** via `kicad-ipc-plugin/` | Talks to the running pcbnew; edits process like user input → appear live. **PCB only.** |
| Build / **edit** a **schematic** sheet headlessly | **ksir** (`ksir compile/adopt`, `/Users/christopher/products/ksir/`) | The sheet's `.ksir` YAML is the edit surface; compiles deterministically to `.kicad_sch` (kicad-sch-api is an internal detail). Netlist-diff gate on every compile; `kb ksir-sync` catches drift. |
| A **deterministic pass/fail gate** (ERC triage, netlist, DRC, diff-pairs, release) | **`kb`** (kicad-bench) | Config-driven, exit-coded, CI/hook-friendly; the live sidecar dashboard. |
| A **judgement-style review** (EMC, DfM, datasheet sanity, SPICE) | **`kicad-happy`** plugin | LLM analysis + narrative report — not a binary gate. |
| Capture / place a **reusable subcircuit** | **Native design blocks** | `.kicad_sch` + hier-label ports in a registered `.kicad_dblib_tbl`. |
| A throwaway one-off poke at the board interactively | Scripting console (SWIG `pcbnew`) | Scratch only — **deprecated, removed in KiCad 11.** Never in production. |

## The surfaces in detail

### 1. `kicad-cli` — the headless backbone
Both editors, no running instance needed. ERC, DRC (`--severity-error
--exit-code-violations` for gates), netlist export, gerber/drill/centroid/STEP export,
**sheet→PDF/SVG and board→SVG (`pcb export svg`) / PNG (`pcb render`)**, `sym`/`fp`
upgrade. It is the render/export engine the sidecar's **Preview tab** uses, and the
one path IPC does **not** cover (no plot/export in KiCad 9/10 — added in 11). All
kicad-cli use in this workbench funnels through `kicad_bench/core/cli.py`.

### 2. IPC API (kipy) — live PCB, the native lever
Protocol Buffers over NNG on a UNIX socket (`/tmp/kicad/api.sock`); official client
`kicad-python` (`import kipy`). Talks to the **running** KiCad; requests are processed
"like any other form of user input," so **board edits appear live, no reopen.**
**PCB editor only in KiCad 9/10** — there is no schematic IPC (planned later). This is
the durable replacement for the SWIG `pcbnew` module (removed in KiCad 11). Our add-on
is `kicad-ipc-plugin/` (a pcbnew toolbar plugin + a standalone `kb-ipc` CLI). Connect
notes: a standalone client must discover/point at the socket and allow an ~8 s first
timeout (see `kicad-ipc-plugin/kb_ipc/client.py`).

*IPC / protobuf contract discipline:* consume kipy's generated bindings — never
hand-roll KiCad's wire format. For any RPC surface we define around it: stable field
tags (never reuse a number), enum tag-0 = `*_UNSPECIFIED`, fields optional, binary
(not text) interchange, and never depend on serialization stability across builds.

### 3. ksir — schematic codegen & round-trip editing (file-based)

Because there is **no eeschema IPC**, schematic automation is file-based — and the
file the assistant edits is the sheet's **`.ksir`** (YAML: symbols by refdes, nets
by name), never raw s-expressions. `ksir compile` regenerates the `.kicad_sch`
(all KiCad-10 gotchas handled internally, incl. the ksa version markers and the
rot-90 pin bug) and prints a canonical-netlist diff that must match the intended
edit exactly. `ksir adopt` round-trips an existing/hand-edited sheet into a .ksir
with geometry, field styling, symbol/file uuids and `(instances)` paths preserved
(refuses unless netlist-identical). Drift gate: `kb ksir-sync`. kicad-sch-api
remains the underlying writer, wrapped by ksir — the notes below still apply to
any direct use of it:

### 3b. kicad-sch-api — the underlying writer (direct use discouraged)
Because there is **no eeschema IPC**, schematic automation means writing `.kicad_sch`
files (kicad-sch-api / the `build_*_sheet.py` generators on `sheetgen.py`). Two standing
hazards: it emits KiCad-9 format that KiCad 10 silently wipes → must post-patch to
`20260306 / "10.0"` (`kicad_bench/core/kicad10.py`); and KiCad won't show the change
until the document is reloaded → use **File ▸ Revert** (or watch the sidecar Preview),
don't restart the whole app.

### 4. `kb` (kicad-bench) — the gates and the assistant
The read-first quality/layout workbench: `kb audit` (everything at once), `erc-triage`,
`netlist-audit`, `dfm-preflight`, the diff-pair/track/route geometry audits,
`release-prep`, plus `kb sidecar` (live dashboard), `kb stage` (lock-aware queue), and
`kb init` (one-command board bring-up). Exit codes: `0` pass / `1` fail / `2` setup.
It **never edits design files** (writes only into `output/` and one git hook).

### 5. `kicad-happy` — narrative analysis
An installed Claude Code plugin (v1.3.0) for LLM-driven EMC pre-compliance, DfM,
datasheet extraction, SPICE, PCB-review reports. Use it for judgement; use `kb` for the
binary gate. They complement — don't duplicate one in the other.

### 6. Native design blocks — reuse
A design block is just a `.kicad_sch` whose external connections are **hierarchical
labels**, living in a directory registered via a `.kicad_dblib_tbl` (global or
`${KIPRJMOD}`-relative). KiCad's GUI has "Save Selection as Design Block" / "Place
Design Block"; `design-block-generator` writes them headlessly. See
`design-block-generator/kicad-10-design-blocks.md`.

## The machine split (where each runs)
- **Mac (KiCad GUI):** the IPC add-on (socket is local to KiCad), hand-editing, viewing.
- **NUC (headless):** Claude + `kicad-cli` + `kb` + kicad-sch-api codegen + git.
- **Handoff:** git branches (branch = lock). View Claude's work via the sidecar in a
  browser; reload KiCad (Revert) only to hand-edit.
