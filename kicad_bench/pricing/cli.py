"""cli.py — `kb price`: rough multi-vendor board cost from the project's own data.

P1 scope: board **fab** cost per vendor (OSH Park exact, JLCPCB coarse estimate) with the
landed-cost scaffold (shipping + duty + tax). Assembly and the JLC tier table are P2/P3.

Read-only. Exit 0 always on a successful render (this is an estimator, not a gate) — 2 on a
setup error (bad config). `--json` for the sidecar feed.
"""
from __future__ import annotations

import json as jsonmod

from ..core import config as cfgmod
from ..core.report import style
from . import models
from .catalog import Catalog
from .spec import extract_spec


def _fmt(usd: float) -> str:
    return f"${usd:,.2f}"


def run(args) -> int:
    cfg = cfgmod.load_or_exit(getattr(args, "config", None))
    cat = Catalog.from_config(cfg)
    qty = args.qty or cat.default_qty
    spec = extract_spec(cfg)
    bom_cost = args.bom_cost
    if bom_cost is None and isinstance(getattr(cfg, "raw", {}), dict):
        bom_cost = cfg.raw.get("pricing", {}).get("bom_cost_usd")
    quotes = models.fab_quotes(spec, cat, qty, tier=args.tier)
    asms = models.assembly_quotes(spec, cat, qty, bom_cost)
    combos = models.combo_rows(quotes, asms)

    def _q(q):
        return {"vendor": q.vendor, "service": q.service, "ok": q.ok,
                "partial": q.partial, "total": q.total, "confidence": q.confidence,
                "items": [{"label": i.label, "usd": i.amount_usd,
                           "confidence": i.confidence} for i in q.items],
                "notes": q.notes}

    if args.json:
        payload = {
            "board": spec.board_name,
            "qty": qty,
            "bom_cost_per_board": bom_cost,
            "spec": {
                "area_in2": spec.area_in2, "layers": spec.layers,
                "thickness_mm": spec.thickness_mm, "copper_weight": spec.copper_weight,
                "finish": spec.finish, "n_placements": spec.n_placements,
                "n_unique_parts": spec.n_unique_parts, "n_joints": spec.n_joints,
            },
            "warnings": spec.warnings,
            "fab": [_q(q) for q in quotes],
            "assembly": [_q(q) for q in asms],
            "combos": [{"label": c.label, "total": c.total, "confidence": c.confidence,
                        "partial": c.partial, "note": c.note} for c in combos],
        }
        print(jsonmod.dumps(payload, indent=2))
        return 0

    # human table
    print(style.bold(f"== kb price — {spec.board_name} (qty {qty}) =="))
    facts = [f"{spec.layers}-layer" if spec.layers else None,
             spec.dims_str() if spec.area_in2 else None,
             spec.finish, spec.copper_weight,
             f"{spec.n_placements} placements" if spec.n_placements else None]
    print("  " + " · ".join(f for f in facts if f))
    for w in spec.warnings:
        print("  " + style.yellow(f"! {w}"))
    def _badge(conf):
        return {"exact": style.green("exact"), "estimate": style.yellow("estimate"),
                "manual": style.dim("manual")}.get(conf, conf)

    def _section(title, qlist):
        print(style.bold(f"  {title}"))
        for q in qlist:
            if not q.ok:
                print(f"  {q.vendor:<14} {style.dim('— n/a')}  {style.dim('; '.join(q.notes))}")
                continue
            flag = style.yellow(" ~partial") if q.partial else ""
            print(f"  {q.vendor:<14} {_fmt(q.total):>10}   {_badge(q.confidence)}{flag}")
            for i in q.items:
                print(style.dim(f"                    {_fmt(i.amount_usd):>10}  {i.label}"))
            for n in q.notes:
                print(style.dim(f"                    {n}"))

    print()
    _section("FAB (bare board)", quotes)
    print()
    _section("ASSEMBLY", asms)
    if combos:
        print()
        print(style.bold("  POPULATED (fab + assembly)"))
        for c in combos:
            flag = style.yellow(" ~partial (add --bom-cost)") if c.partial else ""
            print(f"  {c.label:<32} {_fmt(c.total):>10}   {_badge(c.confidence)}{flag}")
            if c.note:
                print(style.dim(f"                                   {c.note}"))
    print()
    if bom_cost is None:
        print(style.dim("  tip: pass --bom-cost <per-board parts $> for full assembly/combo totals"))
    print(style.dim("  rough estimate · rates dated in catalog · landed lines are soft · "
                    "vendor quote engines win"))
    return 0


def add_parser(sub):
    p = sub.add_parser("price", help="rough multi-vendor board cost from project data")
    p.add_argument("--qty", type=int, default=None, help="board quantity (default from config)")
    p.add_argument("--tier", default="proto",
                   choices=["proto", "swift", "medium_run"], help="service tier")
    p.add_argument("--bom-cost", type=float, default=None, dest="bom_cost",
                   help="bare per-board parts cost (USD) for assembly/combo totals")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
