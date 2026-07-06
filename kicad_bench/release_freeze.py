"""release_freeze.py — `kb release-freeze <version>`: freeze a hardware release.

Priority-0 lifecycle command. Extends `release-prep`'s readiness gate to the whole
product and turns a clean, releasable state into a frozen record:

  1. validate the version (v<major>.<minor>[.<patch>][-suffix]) and a clean git tree;
  2. run `release_prep.readiness()` on EVERY board — abort if any board isn't releasable;
  3. export the full fab package per board into `releases/<version>/<board>/` via the
     selected FabBackend (kicad-cli-native by default; a KiBot backend can plug in later);
  4. commit `releases/<version>/` and create an annotated git tag `<version>`.

Ports freeze-release.sh. Two deliberate differences: it does NOT push (leave that to the
user / CI, so the tool has no network side effects), and it reuses kicad-bench's gates
and the shared fab backend instead of re-shelling. `releases/<version>/` is the frozen
record; everything under `output/` stays regeneratable — CI enforces that split.

Read-first: writes only into `releases/<version>/` plus a commit + tag. No design file is
touched. Exit 0 on success/clean dry-run, 1 if a board isn't releasable, 2 on setup error.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import fab
from .core import cli, config as cfgmod
from .core.report import style
from .layout import release_prep

# v<major>.<minor>[.<patch>][-suffix] — same shape as freeze-release.sh.
_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9.-]+)?$")


def valid_version(v: str) -> bool:
    return bool(_VERSION_RE.match(v))


# ── git helpers ──────────────────────────────────────────────────────────────
def _is_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def _git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _tree_clean(root: Path) -> bool:
    return _git(root, "diff-index", "--quiet", "HEAD", "--").returncode == 0


def _tag_exists(root: Path, tag: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}").returncode == 0


# ── gating ───────────────────────────────────────────────────────────────────
def gate_boards(cfg: cfgmod.Config) -> tuple[bool, list[str]]:
    """Run release_prep.readiness() for every board, printing each verdict. Returns
    (all_ready, [names of boards that are NOT releasable]). Activates each board so the
    single-board readiness() sees that board's fields."""
    all_ready = True
    not_ready: list[str] = []
    for b in cfg.boards:
        cfg.activate(b.name)
        print(style.bold(f"-- board: {b.name} --"))
        ready, res, _ = release_prep.readiness(cfg)
        print(res.render())
        if not ready:
            all_ready = False
            not_ready.append(b.name)
    return all_ready, not_ready


def _releases_dir(cfg: cfgmod.Config) -> Path:
    if cfg.product and cfg.product.releases:
        return cfg.product.releases
    return cfg.root / "releases"


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)

    if not valid_version(args.version):
        sys.exit(f"error: version {args.version!r} must match "
                 "v<major>.<minor>[.<patch>][-suffix] (e.g. v0.1-eval, v1.0, v2.3.1)")

    try:
        backend = fab.get_backend(args.backend)
    except ValueError as e:
        sys.exit(f"error: {e}")

    root = cfg.product.root if cfg.product else cfg.root

    # git preconditions (skipped when not a repo, or with --allow-dirty).
    # Git preconditions only matter when we're going to commit + tag. With --no-git
    # (e.g. CI exporting artifacts for an already-pushed tag) they're skipped.
    if _is_git_repo(root) and not args.no_git and not args.dry_run:
        if _tag_exists(root, args.version):
            sys.exit(f"error: tag {args.version} already exists")
        if not args.allow_dirty and not _tree_clean(root):
            sys.exit("error: working tree has uncommitted changes — commit or stash "
                     "first (or pass --allow-dirty)")

    # For a real freeze the backend must be usable; a dry-run only gates (readiness
    # itself still needs kicad-cli, and will report that on its own).
    if not args.dry_run and not backend.available():
        sys.exit(f"error: fab backend {backend.name!r} unavailable — needs {backend.requires}")

    try:
        all_ready, not_ready = gate_boards(cfg)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")

    if not all_ready:
        print(style.red(f"== not releasable — freeze refused ({', '.join(not_ready)}) =="))
        return 1
    if args.dry_run:
        print(style.green(f"== all {len(cfg.boards)} board(s) releasable "
                          f"(dry-run — rerun without --dry-run to freeze {args.version}) =="))
        return 0

    # Export the frozen fab package per board.
    rel = _releases_dir(cfg) / args.version
    for b in cfg.boards:
        cfg.activate(b.name)
        out = rel / b.name
        print(style.bold(f"-- freezing {b.name} -> {out} --"))
        res = backend.export(cfg, out, artifacts=fab.FULL)
        print(res.render())
        if not res.passed:
            sys.exit(f"error: fab export failed for {b.name} — release {args.version} aborted")

    # Commit the frozen record + annotated tag (no push — that's the user's / CI's call).
    if not args.no_git and _is_git_repo(root):
        _git(root, "add", str(rel))
        c = _git(root, "commit", "-m", f"Freeze release {args.version}")
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
            sys.exit(f"error: git commit failed:\n{c.stderr.strip()}")
        t = _git(root, "tag", "-a", args.version, "-m", f"Release {args.version}")
        if t.returncode != 0:
            sys.exit(f"error: git tag failed:\n{t.stderr.strip()}")
        print(style.green(f"✓ committed {rel} and tagged {args.version} "
                          "(push the tag to trigger CI / a GitHub Release)"))
    else:
        print(style.dim("· git commit/tag skipped"))

    print(style.green(f"== release {args.version} frozen — "
                      f"{len(cfg.boards)} board(s) in {rel} =="))
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "release-freeze",
        help="freeze a release: gate every board, export fab package, commit + tag")
    p.add_argument("version", help="release tag, e.g. v0.1-eval / v1.0 / v2.3.1")
    p.add_argument("--backend", default="kicad-cli",
                   help="fab backend (default: kicad-cli; 'kibot' is a future extension)")
    p.add_argument("--dry-run", action="store_true",
                   help="gate every board and report; write nothing, no commit/tag")
    p.add_argument("--allow-dirty", action="store_true",
                   help="skip the clean-working-tree check")
    p.add_argument("--no-git", action="store_true",
                   help="export only — skip the clean-tree/tag preconditions and the "
                        "commit + tag (CI uses this to build artifacts for an existing tag)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
