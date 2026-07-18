"""conspec.py — the per-datasheet extracted-constraints store.

One `constraints.json` per ingested datasheet, living inside the committed
`.datasheet-index/<slug>/` dir next to `index.json` — so constraints are
content-addressed by the same slug/sha machinery, colocated with the pages they
cite, and travel with the repo. Cross-board sharing is a *path* concern: the
`[constraints] extra_index_dirs` config lists additional index roots (e.g. the
shared kicad-parts Datasheets index) searched after the board's own.

Schema (``"schema": 1``):

    {
      "schema": 1,
      "mpns": ["SN75LVDS83B", ...],          # every MPN this datasheet covers
      "datasheet_sha256": "...",             # of the source PDF (staleness check)
      "extracted_by": "llm" | "heuristic",
      "model": "claude-...",                 # llm extractions only
      "package": "TSSOP-56" | null,
      "pins": [{"number": "10", "name": "PA2",
                "functions": ["UART5_RX", "TIM2_CH3"], "type": "bidirectional"}],
      "abs_max":     [{"param": "...", "value": "...", "unit": "", "pins": "", "page": 6}],
      "recommended": [ same shape ],
      "spec": {"v_rating": 16.0, "dielectric": "X7R", "vf": 2.0, "if_max": 0.02,
               "vin_max": 6.0, "vout": 3.3, "other": [{"param": ..., "value": ...}]},
      "source_pages": {"pins": [3,4], "abs_max": [6], "recommended": [7], "spec": []}
    }

Pure store logic — no LLM calls here (those live in `constraints.py`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from . import config as cfgmod

SCHEMA_VERSION = 1
FILENAME = "constraints.json"

_MIN_PREFIX = 6   # shortest normalized-MPN prefix that counts as a family match


def norm_mpn(mpn: str) -> str:
    """Normalize an MPN for matching: uppercase, alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", mpn.upper())


def mpn_matches(bom_mpn: str, cons_mpn: str) -> bool:
    """True when a BOM MPN and a constraints MPN refer to the same part family.

    Exact normalized match, or one is a prefix of the other (a datasheet's base
    part number vs the BOM's fully-suffixed orderable number, e.g.
    ``SN75LVDS83B`` vs ``SN75LVDS83BDGG``) — with a minimum prefix length so
    short generic strings don't cross-match.
    """
    a, b = norm_mpn(bom_mpn), norm_mpn(cons_mpn)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(a, b, key=len)
    return len(shorter) >= _MIN_PREFIX and (a.startswith(b) or b.startswith(a))


def index_dirs(cfg: cfgmod.Config) -> list[Path]:
    """Constraint search roots: the board's own index first, then extra dirs."""
    out: list[Path] = []
    if cfg.root:
        out.append(Path(cfg.root) / ".datasheet-index")
    raw = cfg.raw.get("constraints", {}) if isinstance(cfg.raw, dict) else {}
    for d in raw.get("extra_index_dirs", []) or []:
        out.append(Path(d).expanduser())
    return out


def save(index_dir: Path, data: dict) -> Path:
    data.setdefault("schema", SCHEMA_VERSION)
    path = index_dir / FILENAME
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def load_dir(index_dir: Path) -> dict | None:
    path = index_dir / FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("schema") != SCHEMA_VERSION:
        return None
    data["_dir"] = str(index_dir)
    return data


def load_all(cfg: cfgmod.Config) -> list[dict]:
    """Every valid constraints.json across all index roots (board's own first)."""
    out: list[dict] = []
    for root in index_dirs(cfg):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            data = load_dir(d)
            if data:
                out.append(data)
    return out


def load_for_mpn(cfg: cfgmod.Config, mpn: str) -> dict | None:
    """Best constraints match for a BOM MPN — longest normalized MPN wins."""
    if not mpn:
        return None
    best: dict | None = None
    best_len = -1
    for data in load_all(cfg):
        for cm in data.get("mpns", []):
            if mpn_matches(mpn, cm):
                if len(norm_mpn(cm)) > best_len:
                    best, best_len = data, len(norm_mpn(cm))
                break
    return best


def lookup(cfg: cfgmod.Config) -> Callable[[str], dict | None]:
    """MPN -> constraints lookup with a one-shot load (for a gate run)."""
    cache = load_all(cfg)

    def _find(mpn: str) -> dict | None:
        best, best_len = None, -1
        for data in cache:
            for cm in data.get("mpns", []):
                if mpn_matches(mpn, cm):
                    if len(norm_mpn(cm)) > best_len:
                        best, best_len = data, len(norm_mpn(cm))
                    break
        return best

    return _find


def pin_tables(cfg: cfgmod.Config, components: dict) -> dict[str, dict[str, list[str]]]:
    """ref -> {pin -> [functions]} for every component whose MPN has an
    extracted pin table with alternate functions (feeds the pin-mux gate).

    `components` is DesignGraph.components (ref -> Component with .mpn).
    """
    find = lookup(cfg)
    out: dict[str, dict[str, list[str]]] = {}
    for ref, comp in components.items():
        mpn = getattr(comp, "mpn", "") or ""
        if not mpn:
            continue
        cons = find(mpn)
        if not cons:
            continue
        table = {p["number"]: p.get("functions") or []
                 for p in cons.get("pins", []) if p.get("number")}
        if any(fns for fns in table.values()):
            out[ref] = table
    return out
