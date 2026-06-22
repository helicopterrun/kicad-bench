"""route-coverage — a headless "what's left to route" dashboard.

The authoritative measure of unrouted copper is KiCad's ratsnest: each
`unconnected_items` entry from `kicad-cli pcb drc` is one missing connection. This tool
counts them per net and rolls the result up by hierarchical block (video / buck / touch /
…) and by net class, so you can see at a glance which blocks are done and which diff pairs
are still open — without opening pcbnew on the Mac. (Poured nets connect their pads through
the zone fill, so a well-poured GND / 12 V rail shows as complete here.)

Net-class resolution reuses netclass-coverage (the same `.kicad_pro` net_settings logic).
Read-only. Exits 0 by default; `--strict` returns 1 if any routable net is still open
(a final pre-fab completeness gate).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from ..core import cli, config as cfgmod, pcbgeom
from ..core.report import Result
from . import netclass_coverage as ncc

_NET_IN_DESC = re.compile(r"\[([^\]]+)\]")


def ratsnest_from_drc(data: dict) -> Counter:
    """net name -> number of open (unrouted) connections, from a DRC result dict.
    Split out so a caller (e.g. the consolidated audit) can share one DRC run."""
    counts: Counter = Counter()
    for item in data.get("unconnected_items", []):
        nets = set()
        for sub in item.get("items", []):
            m = _NET_IN_DESC.search(sub.get("description", ""))
            if m:
                nets.add(m.group(1))
        # an unconnected_item is a single ratsnest line on one net
        for n in nets:
            counts[n] += 1
    return counts


def ratsnest_per_net(pcb: Path) -> Counter:
    """net name -> number of open (unrouted) connections, from DRC unconnected_items."""
    return ratsnest_from_drc(cli.drc_json(pcb))


def block_of(net: str) -> str:
    parts = net.split("/")
    if net.startswith("/") and len(parts) >= 3:
        return parts[1]
    return "(top)"


def analyze(board: pcbgeom.Board, cfg: cfgmod.Config, rats: Counter) -> Result:
    res = Result("Route coverage")
    settings = ncc.read_net_settings(cfg)

    pads = board.pads_by_net()
    routable = sorted(n for n, ps in pads.items() if len(ps) >= 2)
    open_nets = [n for n in routable if rats.get(n, 0) > 0]
    total_open = sum(rats.get(n, 0) for n in routable)

    # by block
    blocks: dict[str, list[int]] = {}     # block -> [done, total]
    for n in routable:
        b = blocks.setdefault(block_of(n), [0, 0])
        b[1] += 1
        if rats.get(n, 0) == 0:
            b[0] += 1
    res.info(f"by block (nets fully connected / routable):")
    for blk in sorted(blocks, key=lambda k: (-(blocks[k][1] - blocks[k][0]), k)):
        done, tot = blocks[blk]
        openc = sum(rats.get(n, 0) for n in routable if block_of(n) == blk)
        tag = "ok" if done == tot else "info"
        getattr(res, tag)(f"  {blk:12s} {done}/{tot} nets"
                          + (f"  ({openc} open connection(s))" if openc else ""))

    # by class
    cls_stat: dict[str, list[int]] = {}
    for n in routable:
        classes = ncc.resolve_classes(n, settings) or {"Default"}
        for c in classes:
            s = cls_stat.setdefault(c, [0, 0])
            s[1] += 1
            if rats.get(n, 0) == 0:
                s[0] += 1
    res.info("by net class (fully connected / routable):")
    for c in sorted(cls_stat):
        done, tot = cls_stat[c]
        getattr(res, "ok" if done == tot else "info")(f"  {c:10s} {done}/{tot}")

    # open diff pairs — the SI-critical ones, surfaced explicitly
    fab = json.loads(cfg.fab_config.read_text()) if cfg.fab_config and cfg.fab_config.exists() else {}
    open_pairs = []
    for g, spec in fab.get("diff_pairs", {}).items():
        for base in spec.get("members", []):
            for suf in ("_P", "_N", "P", "N"):
                for bn in board.match_nets(base + suf):
                    if not board.has_copper(bn) or rats.get(bn, 0) > 0:
                        open_pairs.append(bn)
    if open_pairs:
        res.warn(f"{len(set(open_pairs))} diff-pair net(s) still open: "
                 + ", ".join(sorted(pcbgeom.leaf(n) for n in set(open_pairs))))

    done = len(routable) - len(open_nets)
    res.summary = (f"{done}/{len(routable)} routable nets fully connected; "
                   f"{total_open} ratsnest connection(s) remain on {len(open_nets)} net(s)")
    return res, open_nets


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if not cfg.pcb or not cfg.pcb.exists():
        sys.exit("error: project.pcb missing or not found")
    board = pcbgeom.parse(cfg.pcb)
    try:
        rats = ratsnest_per_net(cfg.pcb)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    res, open_nets = analyze(board, cfg, rats)
    print(res.render())
    if args.strict and open_nets:
        return 1
    return 0


def add_parser(sub):
    p = sub.add_parser("route-coverage",
                       help="ratsnest-based routed/unrouted dashboard by block & net class")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any routable net is still open (pre-fab completeness gate)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
