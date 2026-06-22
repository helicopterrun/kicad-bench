"""track-conformance — check AS-ROUTED track & via widths against per-domain floors.

`kicad-cli pcb drc` enforces one global minimum track width; it cannot say "this 12 V
rail was routed at 0.25 mm but its domain floor is 0.5 mm." The per-domain widths live in
`design_rules_config.json` (PWR_12V 0.5, PWR_BL/SWITCH/PWR_3V3 0.4, BIAS/GND 0.3, LOGIC
0.2) — this tool maps every routed net to its domain and reports tracks/vias under floor.

Reality this respects: on this 4-layer board the high-current rails (VIN_12V, GND, the
bias rails) are distributed as copper POURS (zones), not discrete tracks. A net that is
poured and has no under-floor discrete tracks is conformant, not "thin" — poured nets are
reported as info, never flagged for width.

Domain map and floors come from `fab.config`; nets not in any domain use --default-floor
(the project sets the KiCad Default class to 0.2 mm). Read-only. Exits 0 by default;
`--strict` returns 1 if any net has under-floor copper.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import config as cfgmod, pcbgeom
from ..core.report import Result

EPS = 1e-4


def build_maps(fab: dict):
    """Return (net_domain, floor, via_floor): short-net→domain, domain→track floor,
    domain→(via_dia, via_drill)."""
    domains = fab.get("domains", {})
    floor = {d: p.get("track_width") for d, p in domains.items()}
    via_floor = {d: (p.get("via_diameter"), p.get("via_drill")) for d, p in domains.items()}
    net_domain: dict[str, str] = {}
    for dom, names in fab.get("nets", {}).items():
        for nm in names:
            net_domain[nm] = dom
    for g, spec in fab.get("diff_pairs", {}).items():
        floor.setdefault(g, spec.get("track_width"))
        for base in spec.get("members", []):
            for nm in (f"{base}_P", f"{base}_N", f"{base}P", f"{base}N"):
                net_domain[nm] = g
    return net_domain, floor, via_floor


def classify(net: str, net_domain: dict[str, str]) -> str:
    for key in (net, net.lstrip("/"), pcbgeom.leaf(net)):
        if key in net_domain:
            return net_domain[key]
    return "Default"


def analyze(board: pcbgeom.Board, fab: dict, default_floor: float) -> Result:
    res = Result("Track / via width conformance")
    net_domain, floor, via_floor = build_maps(fab)
    zoned = {z.net for z in board.zones}
    vias_by_net = board.vias_by_net()

    # group board nets by domain
    by_domain: dict[str, list[str]] = {}
    for net in sorted(board.net_names):
        by_domain.setdefault(classify(net, net_domain), []).append(net)

    tracks_by_net = board.tracks_by_net()
    n_under = 0
    for dom in sorted(by_domain):
        fl = floor.get(dom)
        fl = default_floor if fl is None else fl
        nets = by_domain[dom]
        vdia, vdrill = via_floor.get(dom, (None, None))

        offenders = []          # per-net warn lines, emitted under the header
        n_tracked = n_poured = 0
        for net in nets:
            ws = board.width_stats(net)[2]
            if ws:
                n_tracked += 1
            elif net in zoned:
                n_poured += 1

            under_w = sorted(w for w in ws if w < fl - EPS)
            bad_vias = [v for v in vias_by_net.get(net, [])
                        if (vdia and v.size < vdia - EPS) or (vdrill and v.drill < vdrill - EPS)]
            if under_w:
                under_len = sum(t.length for t in tracks_by_net.get(net, [])
                                if t.is_copper and t.width < fl - EPS)
                offenders.append(("warn",
                    f"{pcbgeom.leaf(net)}: track width {under_w} mm < {fl} mm floor", net,
                    f"{under_len:.1f} mm of copper under floor (widths on net: {sorted(ws)})"))
                n_under += 1
            if bad_vias:
                offenders.append(("warn",
                    f"{pcbgeom.leaf(net)}: {len(bad_vias)} via(s) under "
                    f"{vdia}/{vdrill} mm dia/drill floor", net, ""))
                n_under += 1

        res.info(f"{dom}  (floor {fl} mm): {len(nets)} net(s) — "
                 f"{n_tracked} tracked, {n_poured} poured, {len(offenders)} under floor")
        for sev, msg, where, det in offenders:
            res.add(sev, msg, where, det)

    res.summary = (f"{n_under} net(s) with under-floor copper"
                   if n_under else "all routed copper meets its domain floor")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if not cfg.pcb or not cfg.pcb.exists():
        sys.exit("error: project.pcb missing or not found")
    if not cfg.fab_config or not cfg.fab_config.exists():
        sys.exit("error: fab.config (design_rules_config.json) missing or not found")
    fab = json.loads(cfg.fab_config.read_text())
    board = pcbgeom.parse(cfg.pcb)
    res = analyze(board, fab, args.default_floor)
    print(res.render())
    if args.strict:
        return 1 if any(f.severity == "warn" for f in res.findings) else 0
    return 0


def add_parser(sub):
    p = sub.add_parser("track-conformance",
                       help="check as-routed track/via widths vs per-domain floors")
    p.add_argument("--default-floor", type=float, default=0.2,
                   help="width floor for nets in no domain (KiCad Default class; mm, default 0.2)")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any net has under-floor copper")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
