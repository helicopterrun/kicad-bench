# Codex Audit

Audit date: 2026-07-18

Repository: `helicopterrun/kicad-bench`

Audited commit: `a623acd238a5e87566c46198e0b0b732176831c3`

## Scope

This was a usability and functionality audit of the codebase, focused on how a
code owner or new adopter would experience the project. The review covered the
repository structure, README and onboarding docs, Python package metadata, CLI
entry points, tests, generated CI templates, and the core KiCad workflow modules.

The project presents a strong core idea: a config-driven KiCad 10 workbench with
read-first guardrails, quality gates, datasheet-aware checks, layout/DFM prep,
and release tooling behind one `kb` CLI.

## Validation Notes

Commands and checks used during the audit:

```sh
gh repo view helicopterrun/kicad-bench --json nameWithOwner,description,defaultBranchRef,primaryLanguage,languages,isPrivate,updatedAt,pushedAt
gh api 'repos/helicopterrun/kicad-bench/git/trees/HEAD?recursive=1'
git clone --depth 1 https://github.com/helicopterrun/kicad-bench.git /tmp/kicad-bench-audit
python3 -m kicad_bench.main --help
python3 -m pytest -q
GIT_CONFIG_GLOBAL=/dev/null python3 -m pytest -q
```

Local test result:

```text
220 passed in 1.94s
```

The first plain test run failed 3 `release_freeze` tests on this machine because
the host git config required commit signing through an unavailable 1Password
agent. The tests configure a local git identity, but their helper did not fail
loudly when `git commit` failed during setup. Re-running with
`GIT_CONFIG_GLOBAL=/dev/null` isolated the tests from the host signing config and
all tests passed.

## Feedback

### 1. Fix the exit-code contract first.

The README promises uniform exit codes:

- `0`: pass
- `1`: fail, meaning a real problem was found
- `2`: setup error, such as bad config, missing files, or tool failure

Several setup-error paths currently call `sys.exit("error: ...")`, which Python
reports as exit code `1`, not `2`. I verified this for missing config with
`erc-triage`, `audit`, and `approved-parts`.

Recommendation: add a shared helper such as `die_setup(message)` or a
`SetupError` caught in `main()` so setup failures consistently exit `2`.

### 2. Make `--board` consistent with the docs.

The README says multi-board configs take `--board`, but only some board-sensitive
commands expose it. Examples with `--board` include `audit`, `approved-parts`,
`cap-derating`, `led-current`, `pin-mux`, and `review`. Many other commands that
operate on `cfg.root_sch`, `cfg.pcb`, or board-derived config do not expose it,
including `erc-triage`, `netlist-audit`, `commit-gate`, `netclass-*`,
`release-prep`, `sidecar`, and several layout gates.

Recommendation: use a shared argparse parent for `--config` and `--board`, then
opt out only for commands that are intentionally whole-product or repo-global.

### 3. Align generated CI with KiCad 10.

The package targets KiCad 10, and the README emphasizes KiCad 10 behavior. The
generated hardware CI template uses:

```yaml
container:
  image: kicad/kicad:9.0
```

That mismatch can confuse users and may miss KiCad 10-specific behavior.

Recommendation: move the generated CI template to a KiCad 10 image, or document
explicitly why the template remains pinned to KiCad 9.

### 4. Harden test helpers around git.

`tests/test_release_freeze.py` uses a helper that returns `subprocess.run(...)`
but does not assert return codes while setting up temporary git repositories. On
machines with signed commits enabled, a failed setup commit can masquerade as a
product bug later in the test.

Recommendation: make the helper fail loudly by using `check=True` or asserting
`returncode == 0`. Also set `commit.gpgsign=false` inside temporary test repos so
local developer signing policies do not affect tests.

### 5. Centralize external command handling.

`kicad_bench/core/cli.py` is the right place for KiCad CLI interaction, but
several subprocess calls lack timeouts or return-code checks. For example,
`erc_report()` runs `kicad-cli sch erc` and then reads the report without checking
whether the process failed for setup reasons.

Recommendation: add one subprocess wrapper for KiCad commands that handles
timeouts, stderr, expected output-file existence, and the distinction between
"tool failed" and "tool found violations but produced a valid report".

### 6. Add a small KiCad 10 integration fixture suite.

The pure Python and mocked coverage is good: the repo had 220 passing tests once
host git signing was isolated. The biggest compatibility risks remain in KiCad
file parsing and `kicad-cli` behavior.

Recommendation: add one tiny `.kicad_sch` and `.kicad_pcb` golden fixture, then
run a small set of core commands against them in a KiCad 10 container. Snapshot
the important outputs so parser and CLI compatibility regressions are caught.

### 7. Split the large subsystem modules.

Several modules carry entire subsystems:

- `kicad_bench/datasheet.py`: about 1,400 lines
- `kicad_bench/sidecar.py`: about 960 lines
- `kicad_bench/review.py`: about 710 lines
- `kicad_bench/stage.py`: about 620 lines

They are coherent, but they are large enough that future changes will be harder
to reason about.

Recommendation: split by responsibility: CLI wiring, storage/index, extraction,
download, rendering, web handlers, and domain logic. Keep public entry points
stable while moving implementation into smaller modules.

### 8. Package intentionally.

`pyproject.toml` packages `kicad_bench*` and `kicad_bench/templates/*`, while the
repository also includes top-level assets that the README references:

- `skills/`
- `windows-onboarding/`
- `CLAUDE.md`
- `NATIVE_TOOLS.md`

Those may be intentionally source-only, but the current packaging metadata does
not make that clear.

Recommendation: decide whether these assets should ship in sdists/wheels. If yes,
add them to packaging. If no, document that they are repository-only companion
materials.

### 9. Improve install UX.

The README leads with:

```sh
pip install -e . --break-system-packages
```

That can be practical in managed environments, but it is an intimidating default
for new users.

Recommendation: lead with a virtual environment, `uv`, or `pipx` path. Keep
`--break-system-packages` as a container or distro-managed Python note.

### 10. Keep the core product idea, but add a shorter happy path.

The README is strong and unusually explicit about the read/write boundary, command
tiers, exit codes, and the product workflow. It is also large enough that a new
adopter may struggle to find the first safe action.

Recommendation: add a "first 10 minutes" section:

1. install
2. initialize or point at a minimal `kicad-bench.toml`
3. run `kb doctor`
4. run `kb audit`
5. interpret the most common setup and findings outputs

That would make the tool feel less like a full cockpit checklist and more like
something a new KiCad board can adopt today.

## Overall Assessment

The architecture is promising: one config, one CLI, read-first guardrails, and a
clear separation between regenerable output and frozen release records. The main
opportunity is to tighten the public contract around exit codes, board selection,
generated CI, and setup failure behavior so the many moving parts become easier
to trust in local workflows and automation.
