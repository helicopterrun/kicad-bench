"""eco-drift — schematic ↔ PCB net cross-reference (read-only).

`netlist-audit` checks node counts against a committed *baseline*; `route-coverage`
checks that what's on the board is connected. Nothing checked that the board still
matches the **schematic** after a post-layout ECO, a hand edit, or a forgotten
"update PCB from schematic". That is a whole uncovered bug class: a footprint added
straight onto the board, a net renamed on one side only, a pad dragged onto the wrong
net. All of it passes ERC, DRC and route-coverage.

The check reduces both domains to the same shape — net -> {"REF.PAD", ...} — and diffs
them per direction. Both inputs are passed in, so `audit.run_all` injects the netlist
export and parsed board it already has rather than paying for them twice.

Net-name normalization is exact, not fuzzy. `cli.netlist_nets` strips the one leading
'/' KiCad puts on sheet-scoped names and keeps interior ones; the board keeps the full
path. Stripping that single leading '/' makes the two sides identical (measured: 194/194
nets on a real hierarchical board). Deliberately NOT `pcbgeom.leaf()` — collapsing
'/video/SW' to 'SW' aliases distinct same-named nets on different sheets and would
report drift as "in sync", which is the exact failure this gate exists to prevent.

Boundaries: it never edits either file, and it cannot see a pad with no net assigned at
all — `pcbgeom` drops those while parsing, so a cleared pad looks like an absent one.
A board with no pads is not-yet-laid-out, not drift.

Read-only. Exit 0 if the two views agree, 1 on divergence, 2 on setup error.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import cli, config as cfgmod, pcbgeom
from ..core.report import Result, render_and_exit


def _norm(net: str) -> str:
    """Board net name -> schematic spelling: strip the one leading '/'."""
    return net[1:] if net.startswith("/") else net


def _real(net: str) -> bool:
    """Drop KiCad's `unconnected-(U7-PA2-Pad12)` pseudo-nets.

    They are per-pad placeholders, not design intent, and the board spells them
    differently anyway — an interior '/' is escaped to '{slash}', so
    `unconnected-(J201-JTDO/SWO-Pad8)` and `...JTDO{slash}SWO-Pad8` would compare as
    divergent on every board. Same filter as `netclass_coverage.real_nets`.
    """
    return not net.startswith("unconnected-")


def crossref(counts: dict[str, list[str]], board: pcbgeom.Board) -> Result:
    """Diff schematic net→pin sets against PCB net→pad sets. Pure logic — no I/O."""
    res = Result("Schematic ↔ PCB net cross-reference")

    sch: dict[str, set[str]] = {
        net: {rp for rp in refpins} for net, refpins in counts.items() if _real(net)
    }
    pcb: dict[str, set[str]] = {}
    for net, pads in board.pads_by_net().items():
        if not _real(_norm(net)):
            continue
        pcb.setdefault(_norm(net), set()).update(f"{p.ref}.{p.number}" for p in pads)

    if not pcb:
        res.info("no pads carry a net — board not laid out yet; nothing to cross-check")
        res.summary = f"{len(sch)} schematic net(s); board not laid out"
        return res

    placed = set(board.footprints)
    sch_only = sorted(set(sch) - set(pcb))
    pcb_only = sorted(set(pcb) - set(sch))
    shared = sorted(set(sch) & set(pcb))

    diverged = 0
    for net in shared:
        missing = sch[net] - pcb[net]      # in the schematic, not on the board
        extra = pcb[net] - sch[net]        # on the board, not in the schematic
        if not missing and not extra:
            continue
        diverged += 1
        # A pin whose component was never placed is normal mid-layout, not drift.
        unplaced = {rp for rp in missing if rp.split(".", 1)[0] not in placed}
        hard = missing - unplaced
        if hard or extra:
            bits = []
            if hard:
                bits.append(f"missing on board: {', '.join(sorted(hard))}")
            if extra:
                bits.append(f"only on board: {', '.join(sorted(extra))}")
            res.error(f"{net} — schematic and board disagree on membership",
                      net, "; ".join(bits))
        else:
            res.warn(f"{net} — {len(unplaced)} pin(s) on unplaced component(s)",
                     net, f"not yet placed: {', '.join(sorted(unplaced))}")

    for net in sch_only:
        refs = {rp.split(".", 1)[0] for rp in sch[net]}
        if refs & placed:
            res.error(f"{net} — in the schematic, absent from the board", net,
                      "the board has these components but not this net; "
                      "run 'Update PCB from Schematic'")
        else:
            res.warn(f"{net} — in the schematic, not yet on the board", net,
                     "none of its components are placed")
    for net in pcb_only:
        res.error(f"{net} — on the board, absent from the schematic", net,
                  "stale net from an earlier revision, or a board-only edit")

    # Symmetric-empty AND non-vacuous: a run that matched nothing at all is not a pass.
    if not sch and not pcb:
        res.info("neither side has any real nets — nothing was compared")
    elif res.n_errors == 0 and not res.findings:
        res.ok(f"all {len(shared)} net(s) agree between schematic and board")

    res.summary = (f"{len(shared)} net(s) compared, {diverged} diverged; "
                   f"{len(sch_only)} schematic-only, {len(pcb_only)} board-only")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch or not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    if not cfg.pcb or not cfg.pcb.exists():
        sys.exit(f"error: board not found: {cfg.pcb} — set project.pcb in the config")
    counts, _ = cli.netlist_nets(sch)
    board = pcbgeom.parse(cfg.pcb)
    return render_and_exit(crossref(counts, board))


def add_parser(sub):
    p = sub.add_parser("eco-drift",
                       help="schematic ↔ PCB net cross-reference (post-ECO drift)")
    p.add_argument("schematic", nargs="?",
                   help="root sheet (default: project.root_sch / active board)")
    p.add_argument("--board", help="which board (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
