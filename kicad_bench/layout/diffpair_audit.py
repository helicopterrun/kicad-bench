"""diffpair-audit — verify the routed geometry of every differential pair.

`kicad-cli pcb drc` enforces a pair's gap/width *floor* but never reports the things
that actually decide whether a 100 Ω / 90 Ω pair will work: per-net routed length,
intra-pair skew (P vs N length mismatch), inter-pair (bus) length spread, whether the
as-routed width matches the impedance geometry, and whether the pair stayed on one
outer layer (a drop to an inner/reference layer changes Z). Nothing in kicad-bench or
kicad-cli does this — it needs the actual track geometry, which `core/pcbgeom` parses.

Diff-pair groups, member names and the target width/gap come from the project's
`design_rules_config.json` (`fab.config`). Read-only. Exits 0 by default; `--strict`
returns 1 if any pair has a real geometry problem (skew over budget, width off, or
split across layers).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core import config as cfgmod, ipc, pcbgeom
from ..core.report import Result

# A pair member base (e.g. "TMDS_CLK") maps to these candidate P/N leaf names.
def _pn_candidates(base: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (f"{base}_P", f"{base}P"), (f"{base}_N", f"{base}N")


def _find_net(board: pcbgeom.Board, candidates: tuple[str, ...]) -> str | None:
    for cand in candidates:
        hits = board.match_nets(cand)
        if hits:
            return hits[0]
    return None


def _outer_copper_single(layers: set[str]) -> bool:
    """A clean pair sits entirely on one outer copper layer (F.Cu or B.Cu)."""
    return layers in ({"F.Cu"}, {"B.Cu"})


def _impedance_check(board: pcbgeom.Board, spec: dict, layer: str = "F.Cu"):
    """(computed Zdiff | None, one-line note) for a diff-pair group.

    Needs the board's own stackup plus the group's designed width and gap. Any missing
    term returns None — this is warn-biased physics, it never guesses a default FR4.
    """
    w = spec.get("track_width")
    s = spec.get("gap")
    if not board.stackup or not w or not s:
        return None, ""
    geom = board.stackup.microstrip_geometry(layer)
    if not geom:
        return None, ""
    h, er, t = geom
    if not 0.1 <= w / h <= 3.0:
        # Outside the curve fit's stated validity band — the number would be a guess.
        return None, ""
    coplanar = spec.get("coplanar_clearance")
    if coplanar and coplanar < h:
        # Routed as coplanar waveguide with a ground pour alongside, and that pour is
        # CLOSER than the reference plane — so the pour, not the plane, sets the
        # impedance and this microstrip model does not describe the geometry. (When the
        # pour is farther than the plane, as on a thin-prepreg inner-ground stackup, the
        # plane still dominates and the model holds: that case is left to compute.)
        # Measured: a 2-layer board with h = 1.51 mm and a 0.2 mm pour clearance reads
        # 144 Ω against a 90 Ω target under this model — a model mismatch, not drift.
        return None, ""
    try:
        z0 = ipc.microstrip_z0(w, h, t, er)
        zdiff = ipc.differential_z(z0, s, h)
    except ValueError:
        return None, ""
    return zdiff, f"  [computed {zdiff:.1f} Ω on {h:.4f} mm εr {er:.2f}]"


def analyze(board: pcbgeom.Board, fab: dict, max_skew: float, width_tol: float,
            max_bus_spread: float, impedance_tol: float = 0.10) -> Result:
    res = Result("Diff-pair geometry audit")
    groups = fab.get("diff_pairs", {})
    if not groups:
        res.info("no diff_pairs defined in the fab config")
        return res

    n_real = 0
    no_geometry = 0
    for gname, spec in groups.items():
        target_w = spec.get("track_width")
        z = spec.get("target_impedance_ohm", "?")
        members = spec.get("members", [])

        computed, note = _impedance_check(board, spec)
        if computed is None:
            no_geometry += 1
        res.info(f"{gname}  ({z} Ω, target {target_w} mm){note}  — {len(members)} pair(s)")
        if computed is not None and isinstance(z, (int, float)) and z > 0:
            dev = (computed - z) / z
            if abs(dev) > impedance_tol:
                res.warn(f"  {gname}: computed {computed:.1f} Ω vs {z} Ω target "
                         f"({dev:+.1%}, outside ±{impedance_tol:.0%})", gname,
                         "IPC-2141 closed form on the board's own stackup; "
                         "a coplanar pour beside the pair lowers this a few percent")

        routed_lengths: list[tuple[str, float]] = []
        for base in members:
            pcand, ncand = _pn_candidates(base)
            pnet = _find_net(board, pcand)
            nnet = _find_net(board, ncand)
            where = base
            if not pnet or not nnet:
                res.warn(f"  {base}: P/N nets not found on board "
                         f"(looked for {pcand}/{ncand})", where)
                n_real += 1
                continue

            rp, rn = board.has_copper(pnet), board.has_copper(nnet)
            if not rp and not rn:
                res.info(f"  {base}: not yet routed", where)
                continue
            if rp != rn:
                res.warn(f"  {base}: only the {'P' if rp else 'N'} side is routed "
                         f"— pair is half-done", where)
                n_real += 1

            lp, ln = board.net_length(pnet), board.net_length(nnet)
            skew = abs(lp - ln)
            layers = board.layers_used(pnet) | board.layers_used(nnet)
            nvias = len(board.vias_by_net().get(pnet, [])) + len(board.vias_by_net().get(nnet, []))
            wmin, wmax, ws = board.width_stats(pnet)
            wmin2, wmax2, ws2 = board.width_stats(nnet)
            all_w = ws | ws2

            detail = (f"P={lp:.2f}mm  N={ln:.2f}mm  skew={skew:.3f}mm  "
                      f"w={sorted(all_w)}  layers={sorted(layers)}  vias={nvias}")

            problem = False
            if rp and rn and skew > max_skew:
                res.warn(f"  {base}: intra-pair skew {skew:.3f} mm > {max_skew} mm budget",
                         where, detail)
                problem = True
            if target_w is not None and all_w:
                off = [w for w in all_w if abs(w - target_w) > width_tol]
                if off:
                    res.warn(f"  {base}: width {sorted(off)} mm off target {target_w} mm "
                             f"(±{width_tol})", where, detail)
                    problem = True
            if not _outer_copper_single(layers):
                res.warn(f"  {base}: not on a single outer layer (layers {sorted(layers)}) "
                         f"— impedance discontinuity / inner-layer routing", where, detail)
                problem = True
            if not problem:
                res.ok(f"  {base}: {detail}", where)

            routed_lengths.append((base, max(lp, ln)))
            if problem:
                n_real += 1

        if len(routed_lengths) > 1:
            lo = min(l for _, l in routed_lengths)
            hi = max(l for _, l in routed_lengths)
            spread = hi - lo
            tag = "info" if spread <= max_bus_spread else "warn"
            getattr(res, tag)(f"  {gname} bus length spread {spread:.2f} mm "
                              f"across {len(routed_lengths)} routed pair(s)",
                              detail=f"min {lo:.2f} / max {hi:.2f} mm "
                                     f"(inter-pair budget {max_bus_spread} mm)")

    if no_geometry:
        res.info(f"{no_geometry} group(s) without a computed impedance — needs the "
                 "board's stackup plus track_width/gap, and a geometry the microstrip "
                 "model covers (not coplanar-dominated, w/h within 0.1–3.0)")

    res.summary = (f"{n_real} routed pair(s) flagged for review"
                   if n_real else "all routed pairs within budget")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config)
    if not cfg.pcb or not cfg.pcb.exists():
        sys.exit("error: project.pcb missing or not found")
    if not cfg.fab_config or not cfg.fab_config.exists():
        sys.exit("error: fab.config (design_rules_config.json) missing or not found")
    fab = json.loads(cfg.fab_config.read_text())
    board = pcbgeom.parse(cfg.pcb)
    res = analyze(board, fab, args.max_skew, args.width_tol, args.max_bus_spread)
    print(res.render())
    if args.strict:
        return 1 if any(f.severity in ("error", "warn") for f in res.findings) else 0
    return 0


def add_parser(sub):
    p = sub.add_parser("diffpair-audit",
                       help="verify routed diff-pair length/skew/width/layer geometry")
    p.add_argument("--max-skew", type=float, default=0.15,
                   help="intra-pair length-skew budget in mm (default 0.15)")
    p.add_argument("--width-tol", type=float, default=0.02,
                   help="allowed width deviation from target in mm (default 0.02)")
    p.add_argument("--max-bus-spread", type=float, default=5.0,
                   help="inter-pair length-spread budget within a bus, mm (default 5.0)")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any pair has a geometry problem")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
