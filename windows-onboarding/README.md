# Making PCBs with our KiCad stack — Windows onboarding

Welcome! This guide gets you from a fresh Windows machine to designing PCBs with the
same toolchain we use on Mac/Linux. One PowerShell script installs everything; the rest
of this doc explains what you got and how to actually use it.

> **TL;DR:** open PowerShell, run the bootstrap, open a new terminal, run `kb doctor`,
> make a board from a template, and keep `kb sidecar` open while you route.

---

## What this stack is (the 30-second mental model)

Designing a board here is **four tools doing four jobs**:

| Tool | Job | You touch it… |
|---|---|---|
| **KiCad 10** | Draw the schematic, lay out the PCB. The actual EDA app. | …constantly — it's where the design lives. |
| **`kb` (kicad-bench)** | The **gate**. Read-only quality + DFM checks: ERC triage, netlist regressions, footprint resolution, net-class coverage, diff-pair length/skew, DRC, release readiness. | …before every commit and while routing. |
| **Templates + design blocks** | Reusable starting points: pre-stacked board templates and ready-made subcircuits (LDO, buck, USB-C, …). | …when you start a new board or drop in a known circuit. |
| **Claude Code** | The AI assistant, wired with our "skills" so it can drive `kb` and generate design blocks for you. | …whenever you want a second pair of hands. |

**The golden rule of `kb`: it never edits your design.** Every quality check is
read-and-report. The only files it ever writes are regenerable outputs under `output/`
(and, if you ask, one git pre-commit hook). KiCad is the only thing that changes your
`.kicad_sch` / `.kicad_pcb`. So you can run any `kb` command at any time without fear.

---

## Prerequisites

- **Windows 10 (1709 or newer) or Windows 11**, 64-bit.
- An internet connection and ~4 GB free disk.
- `winget` (the "App Installer", ships with modern Windows). If you don't have it, grab
  **App Installer** from the Microsoft Store first.
- You do **not** need admin rights for most of it — but winget may show a UAC prompt when
  it installs KiCad / Python / Node / Git. Click yes.

---

## Quick start

**Option A — download and run in one go** (paste into PowerShell):

```powershell
irm https://raw.githubusercontent.com/helicopterrun/kicad-bench/main/windows-onboarding/bootstrap.ps1 -OutFile $env:TEMP\kb-bootstrap.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\kb-bootstrap.ps1
```

**Option B — if you already cloned this repo:**

```powershell
cd <path-to>\kicad-bench\windows-onboarding
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

Useful flags:

```powershell
.\bootstrap.ps1 -IncludeProjects     # also clone the example boards
.\bootstrap.ps1 -SkipClaudeCode      # KiCad + kb only, no AI tooling
.\bootstrap.ps1 -Workspace D:\kicad  # put the repos somewhere other than $HOME\kicad-stack
```

When it finishes, **open a brand-new PowerShell window** (so PATH changes take effect) and
run `kb doctor`. The script is **idempotent** — safe to re-run any time to pull repo
updates and re-sync everything.

---

## What the script installs and where

| Installed | How | Notes |
|---|---|---|
| KiCad 10 | winget `KiCad.KiCad` | The installer doesn't add `kicad-cli` to PATH — **the script does that for you.** |
| Python 3.12 | winget `Python.Python.3.12` | Runs `kb` and the design-block generator. |
| Git | winget `Git.Git` | For cloning/updating the repos. |
| Node LTS | winget `OpenJS.NodeJS.LTS` | Runtime for Claude Code. *(skipped with `-SkipClaudeCode`)* |
| Claude Code | `npm i -g @anthropic-ai/claude-code` | The `claude` CLI. |
| `kb` (kicad-bench) | `pipx install --editable` | Isolated venv, on PATH, auto-tracks `git pull`. |
| `kicad-sch-api` + `jsonschema` | `pip install --user` | Used by the design-block generator. |

**Locations:**

- **Repos / your workspace:** `%USERPROFILE%\kicad-stack\` (or whatever `-Workspace` you passed) —
  contains `kicad-bench`, `example-templates`, `example-block-library`, `design-block-generator`.
- **Claude skills:** linked into `%USERPROFILE%\.claude\skills\` — `kicad-quality-gate`,
  `kicad-layout-prep`, `design-block-generator`.
- **Templates:** `KICAD_USER_TEMPLATE_DIR` (a user env var) points KiCad at the templates folder.
- **MCP:** `kicad-sch-api` registered at user scope (`claude mcp list` to verify).

---

## Your first board

1. **Open KiCad.** First time only, point it at the design-block library:
   `Preferences → Manage Design Block Libraries → Add` → choose
   `kicad-stack\example-block-library` → name it (e.g. `org-blocks`). One-time.
2. **New project from a template:** `File → New Project from Template → User Templates`.
   Pick the one that matches your fab/stackup:

   | Template | Layers | Stackup |
   |---|---|---|
   | `2L_JLCPCB_Template` | 2 | 1.6 mm FR4 (JLCPCB) |
   | `4L_JLCPCB_Template` | 4 | JLC04161H-7628 |
   | `2L_OSHpark_Template` / `…After_Dark` | 2 | OSH Park |
   | `4L_OSHpark_Template` | 4 | OSH Park 4-layer |

3. **Design the schematic.** Drop in design blocks via `Place → Design Block` for common
   subcircuits, or ask Claude Code to generate one (see below).
4. **Gate it before you commit.** In a terminal, from the project folder:
   ```powershell
   kb audit          # runs every read-only check at once; start here
   kb commit-gate    # footprints + ERC triage + netlist regression -> one verdict
   ```
5. **Lay out the PCB.** Keep the live dashboard open beside KiCad — it re-runs the audit
   automatically every time you save the board:
   ```powershell
   kb sidecar        # -> http://127.0.0.1:8765
   ```
6. **Ship it.** When ERC/DRC are clean and the BOM has no unsourced rows:
   ```powershell
   kb release-prep            # checks readiness
   kb release-prep --export   # writes gerbers + drill + position files to output\release\
   ```

> Each project carries its own `kicad-bench.toml` at its root, so `kb` finds its config
> automatically — you rarely need `--config`. Just run `kb` commands from inside the
> project folder.

---

## The `kb` command cheat-sheet

`kb audit` runs all the read-only checks at once — **start there.** The rest are for
targeted use. Full docs for every command are in [`../README.md`](../README.md).

**Quality gates (read-only):**

| Command | Answers |
|---|---|
| `kb audit` | "Show me everything, one report." |
| `kb erc-triage [sheet]` | "Is this ERC noise real, or a known false-positive?" |
| `kb netlist-audit [sheet]` | "Did a net get silently merged (e.g. into GND)?" |
| `kb block-review <sheet>` | "Review this block: net-name typos, designator range, footprints." |
| `kb commit-gate [--install-hook]` | "Gate this commit." `--install-hook` makes it automatic. |

**Layout / DFM prep:**

| Command | Answers |
|---|---|
| `kb netclass-coverage` / `kb netclass-sync [--write]` | "Which nets are still on Default?" / generate + apply net classes. |
| `kb dfm-preflight` | "Any show-stoppers before layout?" |
| `kb stackup-sync [--emit]` | "Do my Board Setup constraints match the fab stackup?" |
| `kb dru-lint` / `kb dru-guard` | "Is my custom design-rule file valid and actually loading?" |
| `kb release-prep [--export]` | "Am I ready to fab — and export the files." |

**Routed-geometry audits (read-only):**

| Command | Answers |
|---|---|
| `kb diffpair-audit` | "Pair length / skew / width / layer OK?" |
| `kb track-conformance` | "Any copper routed under its per-domain width floor?" |
| `kb route-coverage` | "How much is left to route?" |

**Always handy:** `kb doctor` (sanity check) · `kb sidecar` (live dashboard).

Exit codes are uniform everywhere: `0` pass, `1` real problem, `2` setup error — so they
drop straight into git hooks and CI.

---

## Using Claude Code

Run `claude` in a project folder. First launch: `/login` with your Anthropic account.

The bootstrap wires three **skills** so Claude works the way it does on our other machines:

- **`kicad-quality-gate`** — drives the read-only `kb` quality checks (ERC triage, netlist
  audit, block review, commit-gate). Ask things like *"run the quality gate on this sheet."*
- **`kicad-layout-prep`** — drives the layout/DFM `kb` tooling (net classes, stackup, DRC,
  diff-pairs, release prep).
- **`design-block-generator`** — turns a screenshot, a description, or an existing
  schematic into a reusable KiCad design block, with approved-parts + ERC gates on both
  ends. Try: *"Make a design block for an AMS1117 3.3 V LDO."*

Verify the AI plumbing any time with `claude mcp list` (you should see `kicad-sch-api`).

---

## Keeping the stack up to date

Re-run the bootstrap whenever — it pulls every repo and re-syncs skills:

```powershell
powershell -ExecutionPolicy Bypass -File $env:TEMP\kb-bootstrap.ps1
```

Or update one repo by hand: `git -C $HOME\kicad-stack\kicad-bench pull`. Because `kb` is an
**editable** install, a `git pull` of `kicad-bench` updates the tool with no reinstall.

---

## Troubleshooting

**`kb` (or `claude`, `kicad-cli`) "is not recognized"** → You're in the terminal that was
open *before* install. Open a **new** PowerShell window. (PATH changes only apply to new
sessions.)

**`kicad-cli` still not found in a new terminal** → KiCad installed somewhere unexpected.
Find it (`Get-ChildItem 'C:\Program Files\KiCad' -Recurse -Filter kicad-cli.exe`) and add
that `bin` folder to your PATH, or just re-run the bootstrap.

**`running scripts is disabled on this system`** → Launch with the bypass flag (as shown):
`powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1`. That bypass is per-invocation
and doesn't change your machine's policy.

**A repo clone failed** → That repo is probably still **private**. Ask the maintainer to
make it public (or to add you as a collaborator — then run `gh auth login` and re-run).

**`python` opens the Microsoft Store** → That's the Store alias stub. The script prefers the
`py` launcher and works around it; if it persists, disable the alias in
`Settings → Apps → Advanced app settings → App execution aliases` (turn off the `python.exe`
entries) and re-run.

**`claude mcp list` doesn't show `kicad-sch-api`** → Not fatal — the design-block generator
drives the library directly without the MCP. To enable interactive MCP editing, locate the
server command and run `claude mcp add --scope user kicad-sch-api -- <command>`.

**WSL instead of native Windows?** This stack runs fine natively, so you don't need WSL. If
you prefer Linux, use the repo's Linux setup notes instead of this script.

---
