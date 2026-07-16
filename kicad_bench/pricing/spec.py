"""spec.py — the vendor-neutral board description a price model consumes.

`extract_spec(cfg)` gathers, from files kicad-bench already parses:
  * board **area** (Edge.Cuts bbox) via `pricing.geom`,
  * **layers / thickness / copper / finish** from the fab design-rules JSON (`fab.config`),
  * **placements / unique parts** (best-effort) for assembly models,
and records a `warnings` list for anything it had to guess or skip — a rough estimate that
says where it's soft, rather than inventing numbers. Read-only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..core import config as cfgmod
from . import geom


@dataclass
class PriceSpec:
    board_name: str
    area_in2: float | None
    width_mm: float | None
    height_mm: float | None
    layers: int | None
    thickness_mm: float | None
    copper_weight: str | None
    finish: str | None
    n_placements: int | None = None
    n_unique_parts: int | None = None
    n_joints: int | None = None          # total placed pads ≈ solder joints (per board)
    warnings: list[str] = field(default_factory=list)

    def dims_str(self) -> str:
        if self.width_mm is None:
            return "size ?"
        return f"{self.width_mm:.1f} × {self.height_mm:.1f} mm ({self.area_in2:.2f} in²)"


def _fab_json(cfg: cfgmod.Config) -> dict:
    if cfg.fab_config and cfg.fab_config.exists():
        try:
            return json.loads(cfg.fab_config.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _placement_counts(cfg: cfgmod.Config,
                      warnings: list[str]) -> tuple[int | None, int | None, int | None]:
    """(n_placements, n_unique_parts, n_joints), best-effort. Never raises."""
    n_place = n_uniq = n_joints = None
    # placements + joints from the PCB (public pcbgeom parse)
    if cfg.pcb and cfg.pcb.exists():
        try:
            from ..core import pcbgeom
            board = pcbgeom.parse(cfg.pcb)
            refs = [r for r in board.placements
                    if not r.startswith(("#", "FID", "H", "REF", "TP"))]
            n_place = len(refs) or None
            n_joints = len(board.pads) or None
        except Exception:  # pragma: no cover - defensive; a rough tool must not crash here
            warnings.append("could not read placements from PCB")
    # unique parts from the BOM (needs openpyxl); fall back to distinct footprints
    if cfg.bom and cfg.bom.exists():
        try:
            from .. import bom_assembly as bom
            lines = bom._read_bom(cfg.bom)
            keys = {bom.group_key(l.get("value", ""), l.get("footprint", "")) for l in lines}
            keys.discard("|")
            n_uniq = len(keys) or None
        except Exception:
            warnings.append("could not read unique-part count from BOM")
    return n_place, n_uniq, n_joints


def extract_spec(cfg: cfgmod.Config) -> PriceSpec:
    warnings: list[str] = []
    name = (cfg.active_board or (cfg.pcb.stem if cfg.pcb else None)
            or (cfg.product.name if cfg.product else None) or "board")

    bbox = None
    if cfg.pcb and cfg.pcb.exists():
        bbox = geom.board_outline_bbox(cfg.pcb)
        if bbox is None:
            warnings.append("no Edge.Cuts outline found — area unknown "
                            "(fp_* outlines inside a footprint are not supported)")
    else:
        warnings.append("no .kicad_pcb yet — area unknown (layout deferred?)")

    fab = _fab_json(cfg)
    if not fab:
        warnings.append("no fab design-rules JSON — layers/thickness/copper unknown")

    n_place, n_uniq, n_joints = _placement_counts(cfg, warnings)

    return PriceSpec(
        board_name=name,
        area_in2=bbox.area_in2 if bbox else None,
        width_mm=bbox.w_mm if bbox else None,
        height_mm=bbox.h_mm if bbox else None,
        layers=fab.get("layers"),
        thickness_mm=fab.get("board_thickness"),
        copper_weight=fab.get("copper_weight"),
        finish=fab.get("finish") or fab.get("surface_finish"),
        n_placements=n_place,
        n_unique_parts=n_uniq,
        n_joints=n_joints,
        warnings=warnings,
    )
