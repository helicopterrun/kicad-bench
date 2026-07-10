"""bom_assembly — assemble an interactive BOM by joining schematic, PCB, and BOM.

The cockpit's BOM tab (and, later, a `kb bom-check` gate) reads this. It joins four
per-board sources on the **reference designator** — the one field they all share — into
grouped BOM line-items, and adds the three things a plain BOM can't give you:

  1. a **pin-1 orientation** picture per placed part (`orient_svg`), to eyeball against
     the datasheet pinout and catch a footprint rotated the wrong way at assembly;
  2. a **value cross-check** (`value_check`) — schematic vs PCB vs BOM value per
     designator, so a value edited in one place but not the others is caught before fab;
  3. **alternate part numbers** per line (`alternates`) — the library second-source
     (`approved_parts.csv` `alt_mpn_1/2`) plus arbitrary user-added alternates persisted
     in `.cockpit/bom_alternates.json`, and folded into the fab-ready CSV export so the
     assembler can populate an alt instead of leaving the part unplaced.

Sources (all optional — the module degrades gracefully if one is missing):
  * schematic value/footprint  — flattened kicadxml netlist (`core.cli.netlist_xml`);
  * PCB value + placement       — `core.pcbgeom.parse().placements`;
  * BOM value/LCSC/status       — the board's BOM xlsx (`cfg.bom`), openpyxl;
  * alternates                  — `approved_parts.csv` + `.cockpit/bom_alternates.json`.

Read-only apart from `write_alternates`, which only ever touches `.cockpit/`.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .core import cli
from .core import pcbgeom
from .core.config import Config

# Designator prefixes whose parts are keyed/polarized, so orientation matters at assembly
# (ICs, ESD arrays, transistors, diodes/LEDs, connectors, crystals, relays, switches).
_ORIENT_PREFIXES = ("U", "ESD", "Q", "D", "J", "Y", "K", "SW", "CN", "P")

COCKPIT_DIR = ".cockpit"
ALTERNATES_NAME = "bom_alternates.json"


# ── value normalization ───────────────────────────────────────────────────────
# RKM / SI multipliers. Case matters: 'M' is mega (1M = 1 megohm), 'm' is milli.
_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "U": 1e-6,
         "m": 1e-3, "r": 1.0, "R": 1.0, "k": 1e3, "K": 1e3,
         "M": 1e6, "g": 1e9, "G": 1e9}
_RKM = re.compile(r"^(\d+)([pnuµmrRkKMgG])(\d+)$")        # 4k7, 1r0, 1m5
_NUMMULT = re.compile(r"^([\d.]+)([pnuµmrRkKMgG])?$")     # 100n, 10k, 0.1u, 1.0, 1M
_UNIT_TAIL = re.compile(r"(f|h|Ω|ohm|ohms)$", re.I)       # farad / henry / ohm unit letters


def normalize_value(s: str) -> str:
    """Canonicalize a component value for comparison, tolerant of R/C/L formatting.

    Collapses equivalent spellings to one token: `1R0`/`1.0R`/`1` → `1`;
    `100nF`/`0.1uF`/`0.1µF` → `1e-07`; `10k`/`10K`/`10000` → `10000`. Trailing spec
    tokens (voltage, tolerance, dielectric) are dropped: `10uF/25V`, `22pF NP0`.
    Non-parseable values (IC part names, connectors) are returned trimmed + lowercased
    so exact matches still compare equal. Empty → "".
    """
    if not s:
        return ""
    # keep only the leading value token (drop " NP0", "/25V", tolerance, …)
    tok = re.split(r"[\s/]", s.strip(), 1)[0]
    if not tok:
        return ""
    # strip a trailing unit letter (F/H/Ω) — but NOT the R/k/M multipliers
    core = _UNIT_TAIL.sub("", tok)
    m = _RKM.fullmatch(core)
    if m:
        whole = f"{m.group(1)}.{m.group(3)}"
        return _canon(float(whole) * _MULT[m.group(2)])
    m = _NUMMULT.fullmatch(core)
    if m:
        try:
            val = float(m.group(1)) * (_MULT[m.group(2)] if m.group(2) else 1.0)
            return _canon(val)
        except ValueError:
            pass
    return tok.strip().lower()


def _canon(v: float) -> str:
    """A stable numeric string (so 1e-7 and 0.0000001 compare equal)."""
    return f"{v:.12g}"


def leaf(fp: str) -> str:
    """Footprint leaf name: `Resistor_SMD:R_0402_1005Metric` → `R_0402_1005Metric`."""
    return (fp or "").split(":")[-1].strip()


# ── source readers ────────────────────────────────────────────────────────────
def _sch_components(cfg: Config) -> dict[str, dict]:
    """ref → {value, footprint(leaf)} from the flattened schematic netlist. Empty if
    there's no schematic or kicad-cli isn't available (value-check just skips the sch
    column then)."""
    if not (cfg.root_sch and cfg.root_sch.exists()):
        return {}
    try:
        root = cli.netlist_xml(cfg.root_sch)
    except Exception:  # noqa: BLE001 — kicad-cli missing / export failure → no sch column
        return {}
    out: dict[str, dict] = {}
    for c in root.iter("comp"):
        ref = c.get("ref")
        if not ref or ref.startswith("#"):
            continue
        out[ref] = {"value": (c.findtext("value") or "").strip(),
                    "footprint": leaf(c.findtext("footprint") or "")}
    return out


def _pcb_placements(cfg: Config) -> dict[str, pcbgeom.Placement]:
    if not (cfg.pcb and cfg.pcb.exists()):
        return {}
    try:
        return pcbgeom.parse(cfg.pcb).placements
    except Exception:  # noqa: BLE001 — a parse failure must not break the whole BOM
        return {}


# BOM column header → our field. Matched by case-insensitive substring, first hit wins.
_BOM_COLS = {
    "value": ("comment", "value", "designation"),
    "designator": ("designator", "reference", "ref"),
    "footprint": ("footprint", "package"),
    "lcsc": ("lcsc",),
    "section": ("section",),
    "qty": ("qty", "quantity"),
    "status": ("status",),
    "notes": ("note",),
}


def _read_bom(path: Path | None) -> list[dict]:
    """Parse the board BOM xlsx into line dicts: {value, footprint(leaf), lcsc, section,
    status, notes, refs:[…]} with comma-separated designator groups expanded. Empty list
    if there's no BOM or it can't be read."""
    if not (path and path.exists()):
        return []
    try:
        import openpyxl  # local import: only needed when a BOM exists
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return []
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(h or "").strip() for h in next(rows)]
    except StopIteration:
        return []
    idx: dict[str, int] = {}
    for field, needles in _BOM_COLS.items():
        for i, h in enumerate(header):
            hl = h.lower()
            if any(n in hl for n in needles) and field not in idx:
                idx[field] = i
    def cell(row, field):
        i = idx.get(field)
        return str(row[i]).strip() if (i is not None and i < len(row) and row[i] is not None) else ""
    lines: list[dict] = []
    for row in rows:
        if not row or all(c is None for c in row):
            continue
        desig = cell(row, "designator")
        refs = [r.strip() for r in re.split(r"[,\s]+", desig) if r.strip()]
        if not refs:
            continue
        lines.append({
            "value": cell(row, "value"),
            "footprint": leaf(cell(row, "footprint")),
            "lcsc": cell(row, "lcsc"),
            "section": cell(row, "section"),
            "status": cell(row, "status"),
            "notes": cell(row, "notes"),
            "refs": refs,
        })
    wb.close()
    return lines


# ── alternates ────────────────────────────────────────────────────────────────
def group_key(value: str, footprint: str) -> str:
    """Stable per-line key used for alternates storage + grouping: `value|fp-leaf`.
    Mirrors how the board BOM groups parts (value + footprint-leaf)."""
    return f"{(value or '').strip()}|{leaf(footprint)}"


def _alternates_path(root: Path) -> Path:
    return root / COCKPIT_DIR / ALTERNATES_NAME


def read_alternates(root: Path) -> dict[str, list[dict]]:
    """User-added alternates from `.cockpit/bom_alternates.json` (group_key → [alt,…]).
    Empty dict if absent/unreadable."""
    p = _alternates_path(root)
    try:
        data = json.loads(p.read_text()) if p.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[k] = [a for a in v if isinstance(a, dict) and a.get("mpn")]
    return out


def write_alternates(root: Path, key: str, alternates: list[dict]) -> dict[str, list[dict]]:
    """Upsert one line's alternates and atomically rewrite the JSON. An empty list
    deletes the key. Each alt is normalized to {mpn, lcsc, reason}. Returns the full map."""
    data = read_alternates(root)
    clean = [{"mpn": str(a.get("mpn", "")).strip(),
              "lcsc": str(a.get("lcsc", "")).strip(),
              "reason": str(a.get("reason", "")).strip()}
             for a in alternates if str(a.get("mpn", "")).strip()]
    if clean:
        data[key] = clean
    else:
        data.pop(key, None)
    p = _alternates_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return data


def _load_approved(cfg: Config) -> dict[str, list[dict]]:
    """Index approved_parts.csv rows by normalized value, for library-alternate lookup.
    Falls back to `<root>/approved_parts.csv` when no `[approved_parts]` config section."""
    path = cfg.approved_parts.csv if cfg.approved_parts else (cfg.root / "approved_parts.csv")
    if not path.is_file():
        return {}
    index: dict[str, list[dict]] = {}
    try:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                mpn = (row.get("mpn") or "").strip()
                if not mpn or mpn.startswith("#"):
                    continue
                index.setdefault(normalize_value(row.get("value") or ""), []).append(row)
    except OSError:
        return {}
    return index


def _library_alternates(value: str, fp: str, approved: dict[str, list[dict]]) -> tuple[str, list[dict]]:
    """(primary_mpn, [alt,…]) for a line, from the approved_parts row matching this
    value (+ package when the value is ambiguous). Returns ("", []) when no confident
    match — better to show nothing than to guess a second-source part."""
    cands = approved.get(normalize_value(value), [])
    if len(cands) > 1:                       # disambiguate by package token vs fp-leaf
        fpl = leaf(fp).lower()
        narrowed = [r for r in cands if (r.get("package") or "").strip().lower()
                    and (r.get("package") or "").strip().lower() in fpl]
        cands = narrowed or cands
    if len(cands) != 1:
        return "", []
    row = cands[0]
    alts = []
    for k in ("alt_mpn_1", "alt_mpn_2"):
        v = (row.get(k) or "").strip()
        if v and not v.startswith("#"):
            alts.append({"mpn": v, "lcsc": "", "reason": (row.get("notes") or "").strip(),
                         "source": "approved"})
    return (row.get("mpn") or "").strip(), alts


def _merge_alternates(library: list[dict], user: list[dict]) -> list[dict]:
    """Library seeds + user alternates, deduped by MPN (user wins on conflict)."""
    out: list[dict] = []
    seen: set[str] = set()
    for a in [*user, *library]:
        mpn = (a.get("mpn") or "").strip()
        key = mpn.lower()
        if not mpn or key in seen:
            continue
        seen.add(key)
        out.append({"mpn": mpn, "lcsc": (a.get("lcsc") or "").strip(),
                    "reason": (a.get("reason") or "").strip(),
                    "source": a.get("source", "user")})
    return out


# ── value cross-check ─────────────────────────────────────────────────────────
def _value_check(v_sch: str | None, v_pcb: str | None, v_bom: str | None) -> dict:
    """Compare the value across the sources that are present. Levels:
       * match       — all present sources agree (raw-equal);
       * format-diff — same value, different spelling (raw differ, normalized equal);
       * mismatch    — a source disagrees on the actual value (normalized differ).
    `sch_pcb_mismatch` singles out the highest-signal case: schematic and PCB disagree,
    i.e. the board wasn't re-annotated from the schematic."""
    present = {k: v for k, v in (("sch", v_sch), ("pcb", v_pcb), ("bom", v_bom))
               if v is not None and v != ""}
    norms = {k: normalize_value(v) for k, v in present.items()}
    raws = set(present.values())
    level = "match"
    if len(set(norms.values())) > 1:
        level = "mismatch"
    elif len(raws) > 1:
        level = "format-diff"
    sch_pcb_mismatch = ("sch" in norms and "pcb" in norms and norms["sch"] != norms["pcb"])
    return {"sch": v_sch or "", "pcb": v_pcb or "", "bom": v_bom or "",
            "level": level, "sch_pcb_mismatch": sch_pcb_mismatch}


def _orientation_sensitive(ref: str) -> bool:
    m = re.match(r"[A-Za-z]+", ref)
    return bool(m and m.group(0).upper() in _ORIENT_PREFIXES)


# ── the assembly ──────────────────────────────────────────────────────────────
def build(cfg: Config) -> dict:
    """Assemble the grouped, cross-checked BOM for a board. Returns
    {rows:[…], summary:{…}}; see module docstring for what each source contributes."""
    sch = _sch_components(cfg)
    pcb = _pcb_placements(cfg)
    bom_lines = _read_bom(cfg.bom)
    approved = _load_approved(cfg)
    user_alts = read_alternates(cfg.root)

    # BOM line metadata per designator (LCSC/status/section/BOM-value).
    bom_by_ref: dict[str, dict] = {}
    for ln in bom_lines:
        for ref in ln["refs"]:
            bom_by_ref[ref] = ln

    all_refs = set(sch) | set(pcb) | set(bom_by_ref)
    all_refs = {r for r in all_refs if not r.startswith("#")}

    groups: dict[str, dict] = {}
    for ref in sorted(all_refs, key=_ref_sort):
        s = sch.get(ref, {})
        pl = pcb.get(ref)
        b = bom_by_ref.get(ref, {})
        v_sch = s.get("value") if ref in sch else None
        v_pcb = pl.value if pl else None
        v_bom = b.get("value") if ref in bom_by_ref else None
        # Display value + footprint: prefer the BOM (it's the fab contract), then sch, pcb.
        value = v_bom or v_sch or v_pcb or ""
        fp = b.get("footprint") or s.get("footprint") or (leaf(pl.fpid) if pl else "")
        gk = group_key(value, fp)
        g = groups.get(gk)
        if g is None:
            primary_mpn, lib_alts = _library_alternates(value, fp, approved)
            g = groups[gk] = {
                "group": gk, "value": value, "footprint": fp,
                "lcsc": b.get("lcsc", ""), "section": b.get("section", ""),
                "status": b.get("status", ""), "mpn": primary_mpn,
                "designators": [], "_lib_alts": lib_alts,
            }
        vc = _value_check(v_sch, v_pcb, v_bom)
        g["designators"].append({
            "ref": ref,
            "value_check": vc,
            "orientation_sensitive": _orientation_sensitive(ref),
            "placed": pl is not None,
            "side": pl.side if pl else None,
            "rot": _norm_deg(pl.rot) if pl else None,
            "in_sch": ref in sch, "in_pcb": pl is not None, "in_bom": ref in bom_by_ref,
        })

    rows = []
    for gk, g in groups.items():
        desigs = g["designators"]
        levels = [d["value_check"]["level"] for d in desigs]
        roll = ("mismatch" if "mismatch" in levels else
                "format-diff" if "format-diff" in levels else "match")
        alts = _merge_alternates(g.pop("_lib_alts"), user_alts.get(gk, []))
        rows.append({
            **g,
            "qty": len(desigs),
            "value_check": roll,
            "sch_pcb_mismatch": any(d["value_check"]["sch_pcb_mismatch"] for d in desigs),
            "orientation_sensitive": any(d["orientation_sensitive"] for d in desigs),
            "alternates": alts,
            "missing": _missing_sources(desigs),
        })
    rows.sort(key=lambda r: (r["section"] or "~", r["value"]))

    summary = {
        "parts": sum(r["qty"] for r in rows),
        "lines": len(rows),
        "value_mismatches": sum(1 for r in rows if r["value_check"] == "mismatch"),
        "sch_pcb_mismatches": sum(1 for r in rows if r["sch_pcb_mismatch"]),
        "orientation_sensitive": sum(1 for r in rows if r["orientation_sensitive"]),
        "lines_missing_alternates": sum(1 for r in rows if not r["alternates"]),
        "has_sch": bool(sch), "has_pcb": bool(pcb), "has_bom": bool(bom_lines),
    }
    return {"rows": rows, "summary": summary}


def _missing_sources(desigs: list[dict]) -> list[str]:
    """Which of sch/pcb/bom a line is entirely absent from (all its designators miss it)."""
    out = []
    for key, present in (("sch", "in_sch"), ("pcb", "in_pcb"), ("bom", "in_bom")):
        if desigs and not any(d[present] for d in desigs):
            out.append(key)
    return out


def _ref_sort(ref: str):
    m = re.match(r"([A-Za-z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def _norm_deg(a: float) -> float:
    return round(a % 360.0, 1)


# ── pin-1 orientation SVG ─────────────────────────────────────────────────────
def orient_svg(pl: pcbgeom.Placement, px: int = 150) -> str:
    """A small SVG of a footprint AS PLACED: pads drawn in the board frame (footprint
    rotation applied, X-mirrored for bottom-side parts), pad "1" highlighted red with a
    "1" label, the origin marked, and the side + rotation annotated. An eyeball aid to
    compare against the datasheet pinout — faithful, not pixel-perfect."""
    a = math.radians(-pl.rot)          # KiCad y-down: negate so the picture matches pcbnew
    ca, sa = math.cos(a), math.sin(a)
    mx = -1.0 if pl.side == "bottom" else 1.0

    def place(lx: float, ly: float) -> tuple[float, float]:
        x = lx * ca - ly * sa
        y = lx * sa + ly * ca
        return mx * x, y

    shapes, xs, ys = [], [], []
    for pad in pl.pads:
        cx, cy = place(pad.lx, pad.ly)
        w, h = max(pad.w, 0.2), max(pad.h, 0.2)
        theta = -pl.rot + pad.lrot
        if pl.side == "bottom":
            theta = 180 - theta        # mirror the pad's own rotation too
        is1 = pad.number == "1"
        fill = "#e5484d" if is1 else "#8a8f98"
        shapes.append(
            f'<g transform="translate({cx:.3f} {cy:.3f}) rotate({theta:.2f})">'
            f'<rect x="{-w/2:.3f}" y="{-h/2:.3f}" width="{w:.3f}" height="{h:.3f}" '
            f'rx="0.1" fill="{fill}" fill-opacity="{0.9 if is1 else 0.55}" '
            f'stroke="{fill}" stroke-width="0.06"/></g>')
        if is1:
            shapes.append(
                f'<text x="{cx:.3f}" y="{cy:.3f}" font-size="0.9" fill="#fff" '
                f'text-anchor="middle" dominant-baseline="central" '
                f'font-family="sans-serif" font-weight="bold">1</text>')
        xs += [cx - w / 2, cx + w / 2]
        ys += [cy - h / 2, cy + h / 2]
    if not xs:
        xs, ys = [-1, 1], [-1, 1]
    pad_m = 0.6
    minx, maxx, miny, maxy = min(xs) - pad_m, max(xs) + pad_m, min(ys) - pad_m, max(ys) + pad_m
    w = max(maxx - minx, 0.5)
    h = max(maxy - miny, 0.5)
    ox, oy = place(0.0, 0.0)            # footprint origin marker
    origin = (f'<circle cx="{ox:.3f}" cy="{oy:.3f}" r="0.12" fill="none" '
              f'stroke="#2472c8" stroke-width="0.07"/>')
    label = (f'<text x="{minx + 0.15:.3f}" y="{maxy - 0.15:.3f}" font-size="0.7" '
             f'fill="#6b7280" font-family="sans-serif">'
             f'{pl.side} · {_norm_deg(pl.rot):g}°</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
            f'viewBox="{minx:.3f} {miny:.3f} {w:.3f} {h:.3f}" '
            f'role="img" aria-label="pin-1 orientation for {pl.ref}">'
            f'<rect x="{minx:.3f}" y="{miny:.3f}" width="{w:.3f}" height="{h:.3f}" '
            f'fill="#0d0f14"/>{"".join(shapes)}{origin}{label}</svg>')


# ── fab-ready CSV export ──────────────────────────────────────────────────────
def export_csv(cfg: Config) -> str:
    """The assembly BOM as CSV, one line per group, with an **Approved Alternates**
    column ("; "-joined MPNs) so the fab can populate an alternate instead of leaving a
    part unplaced."""
    data = build(cfg)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Designator", "Value", "Footprint", "LCSC", "MPN",
                "Approved Alternates", "Section", "Qty", "Status", "Value Check"])
    for r in data["rows"]:
        desigs = ",".join(d["ref"] for d in r["designators"])
        alts = "; ".join(a["mpn"] for a in r["alternates"])
        w.writerow([desigs, r["value"], r["footprint"], r["lcsc"], r["mpn"],
                    alts, r["section"], r["qty"], r["status"], r["value_check"]])
    return buf.getvalue()
