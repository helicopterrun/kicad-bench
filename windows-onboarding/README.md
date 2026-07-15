# Making PCBs with our KiCad stack — Windows onboarding

Welcome! This guide gets you from a fresh Windows machine to designing PCBs with the
same toolchain we use on Mac/Linux. One PowerShell script installs everything — including
**both** the **Claude Desktop app** and **Claude Code** (the CLI), wired up the same way —
the rest of this doc explains what you got and how to actually use it.

> **TL;DR:** open PowerShell, run the bootstrap, open a new terminal, run `kb doctor`,
> make a board from a template, and keep `kb sidecar` open while you route. Drive it with
> Claude Desktop or Claude Code, whichever you like.

---

## What this stack is (the 30-second mental model)

Designing a board here is **four jobs**, and the AI is available in two flavors:

| Tool | Job | You touch it… |
|---|---|---|
| **KiCad 10** | Draw the schematic, lay out the PCB. The actual EDA app. | …constantly — it's where the design lives. |
| **`kb` (kicad-bench)** | The **gate**. Read-only quality + DFM + lifecycle checks: ERC triage, netlist regressions, footprint resolution, net-class coverage, diff-pair length/skew, DRC, symbol style, release readiness, plus product scaffold/freeze. | …before every commit and while routing. |
| **Your title block + design blocks** | Your own drawing sheet/branding, plus reusable subcircuits — ones you already have *or* ones you generate. | …when you start a board or drop in a known circuit. |
| **Claude** — Desktop **and** Code | The AI assistant, wired with our "skills" + the `kicad-sch-api` MCP so it can drive `kb` and **generate design blocks** for you. | …whenever you want a second pair of hands. |

**Two ways to run Claude, one setup:**

- **Claude Desktop** — the GUI app. Best for chat-style work, viewing, and a graphical MCP.
  The bootstrap registers the `kicad-sch-api` MCP into its config for you.
- **Claude Code** — the terminal CLI. Best for driving `kb`/git in a project folder. The
  bootstrap links our filesystem **skills** and registers the same MCP.

The script sets up whichever you keep (skip either with a flag). They share the same MCP
server; the only difference is *how skills are loaded* — see
[Skills: Desktop vs. Code](#skills-desktop-vs-code).

**The golden rule of `kb`: it never edits your design.** Every quality check is
read-and-report. The only files it ever writes are regenerable outputs under `output/`
(and, if you ask, one git pre-commit hook, or a brand-new scaffolded product tree / frozen
`releases/` dir). KiCad is the only thing that changes your `.kicad_sch` / `.kicad_pcb`. So
you can run any `kb` command at any time without fear.

> **Bring your own branding & blocks.** This kit deliberately ships *no* shared title
> blocks or a shared design-block library — you use **your own**. The bootstrap prompts
> you for a title block and scans for any design-block libraries you already have, and
> the design-block generator lets you make new ones. Nothing proprietary is cloned.

---

## Prerequisites

- **Windows 10 (1709 or newer) or Windows 11**, 64-bit.
- An internet connection and ~5 GB free disk.
- `winget` (the "App Installer", ships with modern Windows). If you don't have it, grab
  **App Installer** from the Microsoft Store first.
- You do **not** need admin rights for most of it — but winget may show a UAC prompt when
  it installs KiCad / Python / Git / the Claude apps. Click yes.

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
.\bootstrap.ps1 -SkipClaudeDesktop   # KiCad + kb + Claude Code only (no GUI app)
.\bootstrap.ps1 -SkipClaudeCode      # KiCad + kb + Claude Desktop only (no CLI)
.\bootstrap.ps1 -SkipClaudeCode -SkipClaudeDesktop   # KiCad + kb only, no AI at all
.\bootstrap.ps1 -NonInteractive      # don't prompt for a title block (unattended)
.\bootstrap.ps1 -Workspace D:\kicad  # put the repos somewhere other than $HOME\kicad-stack
```

When it finishes, **open a brand-new PowerShell window** (so PATH changes take effect) and
run `kb doctor`. The script is **idempotent** — safe to re-run any time to pull repo
updates and re-sync everything (skills, MCP config, deps).

---

## What the script installs and where

| Installed | How | Notes |
|---|---|---|
| KiCad 10 | winget `KiCad.KiCad` | The installer doesn't add `kicad-cli` to PATH — **the script does that for you.** |
| Python 3.12 | winget `Python.Python.3.12` | Runs `kb` and the design-block generator. |
| Git | winget `Git.Git` | For cloning/updating the repos. |
| **Claude Desktop** | winget `Anthropic.Claude` | The GUI app. *(skipped with `-SkipClaudeDesktop`)* |
| **Claude Code** | winget `Anthropic.ClaudeCode` | The `claude` CLI — native install, **no Node needed**. *(skipped with `-SkipClaudeCode`)* |
| `kb` (kicad-bench) | `pipx install --editable` | Isolated venv, on PATH, auto-tracks `git pull`. `openpyxl` injected for BOM/xlsx. |
| `kicad-sch-api` + `jsonschema` | `pip install --user` | Powers the design-block generator **and** the `kicad-sch-api` MCP server. |

> **No more Node.** Earlier versions installed Node LTS just to `npm i -g` Claude Code.
> The native winget package removes that dependency. (If you *prefer* the npm install, it
> still works — install Node yourself and run `npm i -g @anthropic-ai/claude-code`.)

**Repos cloned (both public):** `kicad-bench` and `design-block-generator` into your
workspace (`%USERPROFILE%\kicad-stack\` by default).

**Locations:**

- **Workspace:** `%USERPROFILE%\kicad-stack\` (or your `-Workspace`).
- **Your templates:** `%USERPROFILE%\kicad-stack\my-templates\` — pointed to by the
  `KICAD_USER_TEMPLATE_DIR` env var. Drop your own project templates / drawing sheet here.
- **Claude Code skills:** linked into `%USERPROFILE%\.claude\skills\` — `kicad-quality-gate`,
  `kicad-layout-prep`, `kicad-product-workflow`, `kicad-symbol-style`, `design-block-generator`.
- **Claude Desktop skill bundles:** `%USERPROFILE%\kicad-stack\claude-desktop-skills\*.zip`
  — the same skills, zipped for upload (Desktop can't read filesystem skills; see below).
- **MCP (both surfaces):** `kicad-sch-api` registered at user scope for Claude Code
  (`claude mcp list`) and written into Claude Desktop's `claude_desktop_config.json`.

---

## Skills: Desktop vs. Code

The two Claude surfaces load skills differently, so the script wires each the right way:

- **Claude Code** loads **filesystem skills** — the script links our five skill folders
  into `%USERPROFILE%\.claude\skills\`. A `git pull` of the repo refreshes them
  automatically (they're junctions). Nothing else to do.
- **Claude Desktop can't load filesystem skills.** Instead the script packages the four
  in-repo skills as `.zip`s in `%USERPROFILE%\kicad-stack\claude-desktop-skills\`. Add them
  once via **Settings → Capabilities / Skills → add a skill**, and pick each `.zip`. (The
  exact menu label moves around between app versions; look under Settings for "Skills" or
  "Capabilities".) `design-block-generator` is a repo of local Python tooling rather than a
  tidy upload, so it's a Claude Code skill only.

Both surfaces get the **`kicad-sch-api` MCP** so Claude can read/write schematic symbols
and nets directly.

> **What runs where.** The `kb`-driving skills (quality-gate, layout-prep,
> product-workflow) actually *execute* `kb`, which lives on **your machine** — so Claude
> Code (running locally in your terminal) is what runs the gates. On Claude **Desktop** the
> same skills act as workflow *guidance*, and the real power is the **`kicad-sch-api` MCP**
> for live schematic edits — run the `kb` gates yourself in a terminal (`kb audit`) beside
> it. `kicad-symbol-style` is pure house-style guidance and is useful in either.

### Turning the MCP on in Claude Desktop

The script writes the server into `%APPDATA%\Claude\claude_desktop_config.json` (merging,
not clobbering, anything you already have). To activate it:

1. Open **Claude Desktop → Settings → Developer** and enable **Developer Mode**.
2. **Fully quit** Claude Desktop (right-click the tray icon → Quit — closing the window
   isn't enough on Windows) and reopen it.
3. In a chat, the tools/plug icon should list **`kicad-sch-api`**.

Claude Code needs no such toggle — `claude mcp list` should already show it.

---

## Your title block & design blocks (bring your own)

This kit doesn't impose anyone else's branding or parts library. During bootstrap:

- **Title block** — you're asked for an optional `.kicad_wks` drawing sheet and a company /
  owner name. Your sheet is copied into `my-templates\`; apply it per project via
  **File → Page Settings → Drawing sheet file**, and set the company name there too.
  (KiCad has no global title-block setting, so it's a quick per-project field — or bake it
  into a template you save in `my-templates\`.)
- **Design blocks** — the script scans `Documents\KiCad`, `Documents`, and your workspace
  for any existing `*.kicad_blocks` libraries and lists what it finds. Register them in
  **Preferences → Manage Design Block Libraries → Add**. Got none? Make your own — see below.

---

## Your first board

1. **New project from a template:** `File → New Project from Template`. Use one of KiCad's
   built-in templates, or one you've saved into your `my-templates\` folder (it shows up
   under **User Templates**). Set your title block in **File → Page Settings**.
   *(Or, for a whole product monorepo, ask Claude to run `kb scaffold <product> <board…>`.)*
2. **Design the schematic.** Drop in design blocks via `Place → Design Block` (from any
   library you registered), or **ask Claude to generate one** (see below).
3. **Gate it before you commit.** In a terminal, from the project folder:
   ```powershell
   kb audit          # runs every read-only check at once; start here
   kb commit-gate    # footprints + ERC triage + netlist regression -> one verdict
   ```
4. **Lay out the PCB.** Keep the live dashboard open beside KiCad — it re-runs the audit
   automatically every time you save the board:
   ```powershell
   kb sidecar        # -> http://127.0.0.1:8765
   ```
5. **Ship it.** When ERC/DRC are clean and the BOM has no unsourced rows:
   ```powershell
   kb release-prep            # checks readiness
   kb release-prep --export   # writes gerbers + drill + position files to output\release\
   kb release-freeze v1.0     # (product monorepos) tag + snapshot a frozen releases\ dir
   ```

> Each project carries its own `kicad-bench.toml` at its root, so `kb` finds its config
> automatically — you rarely need `--config`. Just run `kb` commands from inside the
> project folder. (Run `kb init` to create one for a new board, or `kb scaffold` for a new
> product.)

---

## The `kb` command cheat-sheet

`kb audit` runs all the read-only checks at once — **start there.** The rest are for
targeted use. Full docs for every command are in [`../README.md`](../README.md).

**Start here / always handy:**

| Command | Answers |
|---|---|
| `kb audit` | "Show me everything, one report." |
| `kb sidecar` | Live web dashboard beside KiCad (re-audits on save). |
| `kb doctor` | "Is my environment (kicad-cli, config) sane?" |
| `kb init` / `kb scaffold` | Bring up a new board / scaffold a whole product monorepo. |
| `kb stage` | Lock-aware job queue (run gates without stepping on a live edit). |
| `kb lib-search <term>` | FTS search over your installed symbol/footprint libraries. |
| `kb datasheet …` | Datasheet viewer + pin-table scaffolding helper. |
| `kb sch-live` | Live schematic summary over the KiCad IPC API. |

**Quality gates (read-only):**

| Command | Answers |
|---|---|
| `kb erc-triage [sheet]` | "Is this ERC noise real, or a known false-positive?" |
| `kb netlist-audit [sheet]` | "Did a net get silently merged (e.g. into GND)?" |
| `kb block-review <sheet>` | "Review this block: net-name typos, designator range, footprints." |
| `kb approved-parts` | "Does every part have an approved MPN?" |
| `kb commit-gate [--install-hook]` | "Gate this commit." `--install-hook` makes it automatic. |
| `kb ksir-sync` | "Has an adopted `.ksir` sheet drifted from its `.kicad_sch`?" |
| `kb symbol-style` / `kb sch-readability` | "Does this symbol / sheet follow the house layout rules?" |

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

**Product lifecycle:**

| Command | Answers |
|---|---|
| `kb scaffold <product> [boards…]` | "Start a new product monorepo (hardware/firmware/mechanical/releases)." |
| `kb release-freeze <tag>` | "Freeze + tag a fabrication release." |

Exit codes are uniform everywhere: `0` pass, `1` real problem, `2` setup error — so they
drop straight into git hooks and CI.

---

## Using Claude (Desktop or Code)

- **Claude Code:** run `claude` in a project folder. First launch: `/login` with your
  Anthropic account. `claude mcp list` should show `kicad-sch-api`.
- **Claude Desktop:** open the app, sign in, enable Developer Mode + restart (see above),
  and add the skill `.zip`s via Settings.

Either way, Claude works the way it does on our other machines through these **skills**:

- **`kicad-quality-gate`** — drives the read-only `kb` quality checks (ERC triage, netlist
  audit, block review, commit-gate). *"Run the quality gate on this sheet."*
- **`kicad-layout-prep`** — drives the layout/DFM `kb` tooling (net classes, stackup, DRC,
  diff-pairs, release prep).
- **`kicad-product-workflow`** — the product lifecycle: `kb scaffold` a new product, gate
  every board, `kb release-freeze` a release. *"Scaffold a new product called foo with two boards."*
- **`kicad-symbol-style`** — the house style for how library symbols are drawn (the
  objective subset is enforced by `kb symbol-style`).
- **`design-block-generator`** — turns a screenshot, a description, or an existing
  schematic into a reusable KiCad design block, with approved-parts + ERC gates on both
  ends. **This is how you build your own block library.** *"Make a design block for an AMS1117 3.3 V LDO."*

Verify the AI plumbing any time: `claude mcp list` (Code) or the tools/plug icon (Desktop)
should show `kicad-sch-api`.

---

## Keeping the stack up to date

Re-run the bootstrap whenever — it pulls every repo and re-syncs skills, MCP config, and
deps:

```powershell
powershell -ExecutionPolicy Bypass -File $env:TEMP\kb-bootstrap.ps1
```

Or update one repo by hand: `git -C $HOME\kicad-stack\kicad-bench pull`. Because `kb` is an
**editable** install, a `git pull` of `kicad-bench` updates the tool with no reinstall, and
the linked Claude Code skills refresh with it. (Claude Desktop skills are uploaded copies —
re-run the bootstrap to regenerate the `.zip`s, then re-upload the ones that changed.)

winget installs don't auto-update the apps; refresh them with
`winget upgrade Anthropic.Claude` / `winget upgrade Anthropic.ClaudeCode` now and then.

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

**`python` opens the Microsoft Store** → That's the Store alias stub. The script prefers the
`py` launcher and works around it; if it persists, disable the alias in
`Settings → Apps → Advanced app settings → App execution aliases` (turn off the `python.exe`
entries) and re-run.

**Claude Desktop doesn't show `kicad-sch-api`** → Three things to check: (1) **Developer
Mode** is enabled under Settings → Developer; (2) you **fully quit** and reopened the app
(tray → Quit, not just the window); (3) the config actually points at the server —
Settings → Developer → **Edit Config** opens the file the app really reads. If it's missing
the entry, re-run the bootstrap, or add it by hand under `mcpServers`:
```json
{
  "mcpServers": {
    "kicad-sch-api": { "command": "C:\\full\\path\\to\\kicad-sch-mcp.exe" }
  }
}
```
On MSIX-packaged installs the app sometimes reads a *virtualized* copy of that file; the
script writes both locations, but the **Edit Config** button always opens the real one.

**`claude mcp list` doesn't show `kicad-sch-api`** → Not fatal — the design-block generator
drives the library directly without the MCP. To enable it, locate the server exe and run
`claude mcp add --scope user kicad-sch-api -- <path-to-exe>`.

**Claude Desktop skill upload — where?** → Settings, then look for **Skills** or
**Capabilities** (the label moves between versions). Upload the `.zip`s from
`%USERPROFILE%\kicad-stack\claude-desktop-skills\`. Uploaded skills are per-user copies, so
after a repo update re-run the bootstrap and re-upload the changed ones.

**I'd rather use the npm Claude Code** → Fine. Run `-SkipClaudeCode`, install Node yourself
(`winget install OpenJS.NodeJS.LTS`), then `npm i -g @anthropic-ai/claude-code`, and re-run
the bootstrap (with Claude Code no longer skipped) to finish the MCP + skill wiring.

**WSL instead of native Windows?** This stack runs fine natively, so you don't need WSL. If
you prefer Linux, use the repo's Linux setup notes instead of this script.

---
