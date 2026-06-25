"""netlist-audit — per-net node-count audit.

ERC cannot see a net that was silently merged into GND (the L-stub bug class):
the nodes still exist, they just landed on the wrong net. This compares every
named net's node count against an expected map and fails on any mismatch or any
expected net missing from the netlist entirely.

Expected counts come from `contracts.expected_nets` in the config (a JSON map
{net_name: count}). With no expected map, the tool just prints the node-count
table (useful for building the baseline). Generalizes validate_sheet.py --expect.

Read-only. Exits 0 if all expected nets match, 1 otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import cli, config as cfgmod
from ..core.report import Result, render_and_exit


def audit(sch: Path, expect: dict[str, int] | None) -> Result:
    res = Result(f"Netlist audit: {sch.name}")
    counts, _ = cli.netlist_nets(sch)
    expect = expect or {}

    for net in sorted(expect):
        got = len(counts.get(net, []))
        if net not in counts:
            res.error(f"{net}: MISSING from netlist (expected {expect[net]})", net)
        elif got != expect[net]:
            res.error(f"{net}: {got} nodes, expected {expect[net]}", net,
                      detail="nodes: " + ", ".join(counts[net]))
        else:
            res.ok(f"{net}: {got} nodes", net)

    if not expect:
        for net in sorted(counts):
            res.info(f"{net}: {len(counts[net])} nodes", net,
                     detail=", ".join(counts[net]))
        res.summary = (f"{len(counts)} nets (no expected_nets configured — "
                       "this is a baseline dump)")
    else:
        res.summary = (f"{res.n_errors} mismatch(es) of {len(expect)} checked "
                       f"({len(counts)} nets total)")
    return res


def _named_live(live_nets) -> set[str]:
    """User-meaningful (labelled) live net names, leading '/' stripped. Excludes KiCad
    'Net-(…)' autonames and 'unconnected-(…)' pseudo-nets (they churn and aren't the
    bug class this audit targets)."""
    return {n["name"].lstrip("/") for n in live_nets if n.get("kind") == "named"}


def audit_live(live_nets: list[dict], expect: dict[str, int] | None,
               file_counts: dict[str, list[str]] | None) -> Result:
    """Ground-truth net audit against the RUNNING editor (reflects unsaved edits).

    The IPC API exposes net *names* as ground truth but not per-pin node membership
    (symbol pins aren't resolvable items), so live mode asserts net **presence**, not
    node counts — node-count assertions stay in the default (file) path. Two checks:
      1. every expected net is present in the live netlist (catches a net merged into
         GND / renamed / deleted — live, before you save);
      2. live-vs-saved-file divergence among named nets (what an unsaved edit changed,
         or a stale saved baseline).
    """
    res = Result("Netlist audit — LIVE (IPC, named nets)")
    real = _named_live(live_nets)
    expect = expect or {}

    for net in sorted(expect):
        if net in real:
            res.ok(f"{net}: present", net)
        else:
            res.error(f"{net}: MISSING from live netlist (expected {expect[net]} nodes)", net)

    if file_counts is not None:
        file_named = {n for n in file_counts if n and not n.startswith("Net-")}
        for net in sorted(real - file_named):
            res.warn(f"{net}: in LIVE editor but not in saved file (unsaved edit?)", net)
        for net in sorted(file_named - real):
            res.warn(f"{net}: in saved file but GONE from live editor (merged/renamed?)", net)

    n_auto = sum(1 for n in live_nets if n.get("kind") == "auto")
    n_unc = sum(1 for n in live_nets if n.get("kind") == "unconnected")
    if not expect:
        for net in sorted(real):
            res.info(f"{net}: present (live)", net)
    res.summary = (f"{len(real)} named nets live (+{n_auto} auto, +{n_unc} unconnected); "
                   + (f"{res.n_errors} expected missing of {len(expect)} checked. "
                      if expect else "presence-only — ")
                   + "per-pin counts need file mode (API can't resolve pins).")
    return res


def emit_baseline(sch: Path, dest: Path) -> int:
    """Write {net: node_count} from the current netlist as a regression baseline."""
    counts, _ = cli.netlist_nets(sch)
    baseline = {n: len(counts[n]) for n in sorted(counts)
                if n and not n.startswith("unconnected-")}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(baseline, indent=2) + "\n")
    print(f"✓ wrote baseline of {len(baseline)} nets -> {dest}")
    print("  point contracts.expected_nets at this file to make the audit assert.")
    return 0


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    sch = Path(args.schematic) if args.schematic else cfg.root_sch

    if args.live:
        # Ground-truth audit against the running editor. The schematic file is optional
        # here (only used for the live-vs-saved diff); presence checks need no file.
        from ..sch_live import fetch_live
        data = fetch_live(args, ["sch", "--nets"])
        live_nets = data.get("nets", [])
        file_counts = None
        if sch and Path(sch).exists():
            try:
                file_counts, _ = cli.netlist_nets(Path(sch))
            except cli.KicadCliError:
                file_counts = None  # diff is best-effort; presence audit still runs
        expect = cfg.expected_nets
        if args.expect:
            expect = json.loads(Path(args.expect).read_text())
        res = audit_live(live_nets, expect, file_counts)
        return render_and_exit(res)

    if not sch:
        sys.exit("error: no schematic given and no project.root_sch in config")
    if not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    if args.emit_baseline:
        dest = Path(args.emit_baseline)
        if dest.is_dir() or str(dest).endswith("/"):
            dest = dest / f"{cfg.path.stem}.expected_nets.json"
        try:
            return emit_baseline(Path(sch), dest)
        except cli.KicadCliError as e:
            sys.exit(f"error: {e}")
    expect = cfg.expected_nets
    if args.expect:
        expect = json.loads(Path(args.expect).read_text())
    try:
        res = audit(Path(sch), expect)
    except cli.KicadCliError as e:
        sys.exit(f"error: {e}")
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("netlist-audit", help="per-net node-count audit (catches GND merges)")
    p.add_argument("schematic", nargs="?", help="sheet to check (default: project.root_sch)")
    p.add_argument("--expect", help="override: JSON {net: count}")
    p.add_argument("--emit-baseline", metavar="PATH",
                   help="write current node counts as a baseline JSON, then exit "
                        "(a dir/trailing-slash auto-names <config>.expected_nets.json)")
    p.add_argument("--live", action="store_true",
                   help="audit the RUNNING editor over IPC (reflects unsaved edits): "
                        "expected-net presence + live-vs-saved diff (needs kb-ipc + KiCad 11)")
    p.add_argument("--socket", help="--live: override API socket (else KICAD_API_SOCKET / config)")
    p.add_argument("--timeout", type=int, default=20, help="--live: kb-ipc timeout seconds")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
