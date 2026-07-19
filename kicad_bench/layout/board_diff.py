"""board-diff — what changed on the board between two git revisions (read-only).

`kb changes` lists commits; nothing said what those commits did to the *board*. Reading
a `.kicad_pcb` diff by eye is hopeless — a nudged footprint rewrites coordinates all
over the file, and a re-pour rewrites megabytes of zone polygon.

This answers the review question instead: which components appeared, vanished, changed
footprint, moved or flipped; which nets appeared, vanished or changed membership; how
the copper totals moved. It reuses the parsers that already exist, so it costs one
extra parse per revision and no new dependency.

Historic revisions are read with `git show <rev>:<path>`, which writes nothing. NOT
`git checkout` — that would clobber uncommitted work in the tree being reviewed — and
not `git worktree`, which creates directories and writes into .git.

The staging directory is `/tmp` on purpose: kicad-cli here is a flatpak granted
`--filesystem=/tmp` only, so a board staged anywhere else is unreadable to the
renderers that build on this.

Read-only. Exit 0 always unless `--strict`, under which any change fails — a design
change is not a defect, so it is reported, not judged.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from ..core import config as cfgmod, pcbgeom
from ..core.report import Result

_GIT_TIMEOUT = 20


def git_root(start: Path) -> Path | None:
    ok, out = _git(start, "rev-parse", "--show-toplevel")
    return Path(out) if ok and out else None


def _git(cwd: Path, *args: str) -> tuple[bool, str]:
    """Run git with a fixed argv (never a shell — board paths contain spaces)."""
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return p.returncode == 0, p.stdout.strip()


def blob_at(root: Path, rev: str, relpath: str) -> bytes | None:
    """A file's bytes at `rev`, without touching the working tree."""
    try:
        p = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{relpath}"],
                           capture_output=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def board_at(root: Path, rev: str, relpath: str) -> pcbgeom.Board | None:
    """Parse the board as it stood at `rev`. Stages under /tmp (flatpak grant)."""
    data = blob_at(root, rev, relpath)
    if data is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        return pcbgeom.parse(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def revisions(root: Path, relpath: str, n: int = 20) -> list[dict]:
    """Recent commits that actually touched this board file."""
    ok, out = _git(root, "log", f"-{n}", "--pretty=%H%x00%h%x00%cr%x00%s",
                   "--", relpath)
    if not ok or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\0")
        if len(parts) == 4:
            rows.append(dict(zip(("sha", "short", "when", "subject"), parts)))
    return rows


def diff_boards(old: pcbgeom.Board, new: pcbgeom.Board, tol_mm: float = 0.01) -> dict:
    """Structural delta between two parsed boards. Pure — no git, no renderer."""
    o_fp, n_fp = old.footprints, new.footprints
    added = sorted(set(n_fp) - set(o_fp))
    removed = sorted(set(o_fp) - set(n_fp))
    common = sorted(set(o_fp) & set(n_fp))

    refootprinted = [{"ref": r, "from": o_fp[r], "to": n_fp[r]}
                     for r in common if o_fp[r] != n_fp[r]]

    moved = []
    for r in common:
        a, b = old.placements.get(r), new.placements.get(r)
        if not a or not b:
            continue
        dx, dy, drot = b.x - a.x, b.y - a.y, b.rot - a.rot
        flipped = a.side != b.side
        if flipped or abs(dx) > tol_mm or abs(dy) > tol_mm or abs(drot) > tol_mm:
            moved.append({"ref": r, "dx": round(dx, 3), "dy": round(dy, 3),
                          "drot": round(drot, 3), "flipped": flipped})

    o_nets = {n: {f"{p.ref}.{p.number}" for p in pads}
              for n, pads in old.pads_by_net().items()}
    n_nets = {n: {f"{p.ref}.{p.number}" for p in pads}
              for n, pads in new.pads_by_net().items()}
    changed_nets = sorted(n for n in set(o_nets) & set(n_nets)
                          if o_nets[n] != n_nets[n])

    return {
        "added_refs": added,
        "removed_refs": removed,
        "changed_footprints": refootprinted,
        "moved": sorted(moved, key=lambda m: m["ref"]),
        "added_nets": sorted(set(n_nets) - set(o_nets)),
        "removed_nets": sorted(set(o_nets) - set(n_nets)),
        "changed_nets": changed_nets,
        "track_delta": len(new.tracks) - len(old.tracks),
        "via_delta": len(new.vias) - len(old.vias),
    }


def summarize(delta: dict, old_rev: str, new_rev: str) -> Result:
    """Render a structural delta as a Result. Every line is informational."""
    res = Result(f"Board diff {old_rev} → {new_rev}")
    counts = {k: len(delta[k]) for k in
              ("added_refs", "removed_refs", "changed_footprints", "moved",
               "added_nets", "removed_nets", "changed_nets")}
    if not any(counts.values()) and not delta["track_delta"] and not delta["via_delta"]:
        res.ok("no change to components, nets or copper")
        res.summary = "no change"
        return res

    if delta["added_refs"]:
        res.info(f"{len(delta['added_refs'])} component(s) added: "
                 f"{', '.join(delta['added_refs'][:12])}")
    if delta["removed_refs"]:
        res.info(f"{len(delta['removed_refs'])} component(s) removed: "
                 f"{', '.join(delta['removed_refs'][:12])}")
    for c in delta["changed_footprints"][:12]:
        res.info(f"{c['ref']} footprint changed", c["ref"], f"{c['from']} → {c['to']}")
    for m in delta["moved"][:20]:
        bits = []
        if m["dx"] or m["dy"]:
            bits.append(f"moved {m['dx']:+.3f}, {m['dy']:+.3f} mm")
        if m["drot"]:
            bits.append(f"rotated {m['drot']:+g}°")
        if m["flipped"]:
            bits.append("flipped side")
        res.info(f"{m['ref']} {'; '.join(bits)}", m["ref"])
    if len(delta["moved"]) > 20:
        res.info(f"...and {len(delta['moved']) - 20} more moved component(s)")
    for net in delta["added_nets"][:12]:
        res.info(f"net {net} appeared on the board", net)
    for net in delta["removed_nets"][:12]:
        res.info(f"net {net} left the board", net)
    for net in delta["changed_nets"][:12]:
        res.info(f"net {net} changed membership", net)
    if delta["track_delta"] or delta["via_delta"]:
        res.info(f"copper: {delta['track_delta']:+d} track segment(s), "
                 f"{delta['via_delta']:+d} via(s)")

    res.summary = (f"{counts['added_refs']} added, {counts['removed_refs']} removed, "
                   f"{counts['moved']} moved, "
                   f"{counts['added_nets'] + counts['removed_nets'] + counts['changed_nets']}"
                   f" net change(s)")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    if not cfg.pcb or not cfg.pcb.exists():
        sys.exit(f"error: board not found: {cfg.pcb}")
    root = git_root(cfg.pcb.parent)
    if not root:
        sys.exit(f"error: not inside a git repository: {cfg.pcb.parent}")
    try:
        rel = cfg.pcb.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        sys.exit(f"error: board is outside the git repo: {cfg.pcb}")

    old = board_at(root, args.old_rev, rel)
    if old is None:
        sys.exit(f"error: no board at revision {args.old_rev!r} (path {rel!r})")
    if args.new_rev:
        new = board_at(root, args.new_rev, rel)
        if new is None:
            sys.exit(f"error: no board at revision {args.new_rev!r}")
        new_label = args.new_rev
    else:
        new = pcbgeom.parse(cfg.pcb)            # the working tree, uncommitted edits and all
        new_label = "working tree"

    res = summarize(diff_boards(old, new), args.old_rev, new_label)
    print(res.render())
    if args.strict and res.summary != "no change":
        return 1
    return 0


def add_parser(sub):
    p = sub.add_parser("board-diff",
                       help="what changed on the PCB between two git revisions")
    p.add_argument("old_rev", help="baseline revision (e.g. HEAD~5, a tag, a SHA)")
    p.add_argument("new_rev", nargs="?",
                   help="revision to compare against (default: the working tree)")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if anything changed (for CI freeze checks)")
    p.add_argument("--board", help="which board (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
