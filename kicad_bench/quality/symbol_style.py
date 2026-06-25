"""symbol-style — audit .kicad_sym libraries against the house symbol style guide.

Enforces the *objective* subset of skills/kicad-symbol-style/SKILL.md — the rules that
can be read straight out of a `.kicad_sym` file:

  * 50 mil (1.27 mm) grid: every field and pin position snaps to the grid ("no off-grid
    endpoints, ever").
  * Typography: Reference and pin name/number at 50 mil regular; Value at 40 mil italic;
    visible secondary fields (Description / Datasheet) at 40 mil italic.
  * ERC hygiene: flag pins left at electrical type `unspecified`.

Grouping, overlap, orientation, and pad↔pin matching are human review (the guide says so)
and are deliberately NOT checked here. Advisory by default (warns, exit 0) because the
guide is a v0.1 draft and existing libraries predate it; `--strict` promotes the style
warnings to errors so a migrated library can be a hard gate. Read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import config as cfgmod
from ..core.report import Result, render_and_exit

GRID_MM = 1.27          # 50 mil
PRIMARY_MM = 1.27       # 50 mil
SECONDARY_MM = 1.016    # 40 mil
EPS = 1e-4

# Fields whose typography we treat as "secondary" (40 mil italic) when visible.
_SECONDARY_FIELDS = {"Description", "Datasheet"}


# ── tiny S-expression parser (KiCad .kicad_sym is plain s-expr) ───────────────
def parse_sexpr(text: str):
    """Parse one S-expression into nested lists; atoms are strings (quotes stripped)."""
    pos = 0
    n = len(text)

    def skip_ws():
        nonlocal pos
        while pos < n and text[pos] in " \t\r\n":
            pos += 1

    def parse_node():
        nonlocal pos
        skip_ws()
        if pos >= n:
            raise ValueError("unexpected end of input")
        if text[pos] == "(":
            pos += 1
            node = []
            while True:
                skip_ws()
                if pos >= n:
                    raise ValueError("unterminated list")
                if text[pos] == ")":
                    pos += 1
                    return node
                node.append(parse_node())
        if text[pos] == '"':
            pos += 1
            buf = []
            while pos < n and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < n:
                    pos += 1
                buf.append(text[pos])
                pos += 1
            pos += 1  # closing quote
            return "".join(buf)
        # bare atom
        start = pos
        while pos < n and text[pos] not in " \t\r\n()\"":
            pos += 1
        return text[start:pos]

    skip_ws()
    return parse_node()


def _tag(node) -> str:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else ""


def _children(node, tag: str):
    return [c for c in node if isinstance(c, list) and _tag(c) == tag]


def _first(node, tag: str):
    for c in node:
        if isinstance(c, list) and _tag(c) == tag:
            return c
    return None


def _on_grid(v: float) -> bool:
    return abs(v / GRID_MM - round(v / GRID_MM)) < EPS


def _font(effects):
    """Return (size, italic) from an (effects (font (size w h) [italic yes])) node."""
    if effects is None:
        return None, False
    font = _first(effects, "font")
    if font is None:
        return None, False
    size = _first(font, "size")
    sz = float(size[2]) if size and len(size) >= 3 else None
    italic = any(_tag(c) == "italic" and len(c) > 1 and c[1] == "yes" for c in font)
    return sz, italic


def _at(node):
    a = _first(node, "at")
    if a and len(a) >= 3:
        try:
            return float(a[1]), float(a[2])
        except ValueError:
            return None
    return None


def _hidden(node) -> bool:
    h = _first(node, "hide")
    return bool(h) and len(h) > 1 and h[1] == "yes"


# ── per-symbol audit ──────────────────────────────────────────────────────────
def _walk_pins(symbol):
    """Yield every (pin node) in a symbol, descending into unit/body-style sub-symbols."""
    for child in symbol:
        if isinstance(child, list):
            if _tag(child) == "pin":
                yield child
            elif _tag(child) == "symbol":   # nested unit, e.g. "C_1_1"
                yield from _walk_pins(child)


def audit_symbol(symbol, res: Result, sev):
    name = symbol[1] if len(symbol) > 1 and isinstance(symbol[1], str) else "?"

    for prop in _children(symbol, "property"):
        pname = prop[1] if len(prop) > 1 else "?"
        at = _at(prop)
        if at and not (_on_grid(at[0]) and _on_grid(at[1])):
            sev(f"{name}: field '{pname}' off 50mil grid at ({at[0]}, {at[1]})mm", name)
        sz, italic = _font(_first(prop, "effects"))
        if pname == "Reference" and sz is not None and abs(sz - PRIMARY_MM) > EPS:
            sev(f"{name}: Reference is {sz}mm, expected 50mil (1.27mm) regular", name)
        if pname == "Value":
            if sz is not None and abs(sz - SECONDARY_MM) > EPS:
                sev(f"{name}: Value is {sz}mm, expected 40mil (1.016mm)", name)
            if not italic:
                sev(f"{name}: Value must be italic", name)
        if pname in _SECONDARY_FIELDS and not _hidden(prop):
            if sz is not None and abs(sz - SECONDARY_MM) > EPS:
                sev(f"{name}: visible {pname} is {sz}mm, expected 40mil (1.016mm)", name)
            if not italic:
                sev(f"{name}: visible {pname} should be italic", name)

    npins = 0
    for pin in _walk_pins(symbol):
        npins += 1
        ptype = pin[1] if len(pin) > 1 else "?"
        at = _at(pin)
        if at and not (_on_grid(at[0]) and _on_grid(at[1])):
            num = _first(pin, "number")
            label = num[1] if num and len(num) > 1 else "?"
            sev(f"{name}: pin {label} off 50mil grid at ({at[0]}, {at[1]})mm", name)
        if ptype == "unspecified":
            num = _first(pin, "number")
            label = num[1] if num and len(num) > 1 else "?"
            sev(f"{name}: pin {label} electrical type is 'unspecified' (set it for ERC)", name)
        for part in ("name", "number"):
            node = _first(pin, part)
            if node:
                sz, _ = _font(_first(node, "effects"))
                if sz is not None and abs(sz - PRIMARY_MM) > EPS:
                    val = node[1] if len(node) > 1 else "?"
                    sev(f"{name}: pin {part} '{val}' is {sz}mm, expected 50mil (1.27mm)", name)
    return name, npins


def audit(libs: list[Path], strict: bool = False) -> Result:
    res = Result("Symbol style" + (" (strict)" if strict else ""))
    emit = res.error if strict else res.warn
    n_sym = 0
    for lib in libs:
        try:
            root = parse_sexpr(Path(lib).read_text())
        except Exception as e:  # noqa: BLE001
            res.error(f"{Path(lib).name}: parse failed: {e}", str(lib))
            continue
        if _tag(root) != "kicad_symbol_lib":
            res.warn(f"{Path(lib).name}: not a kicad_symbol_lib, skipped", str(lib))
            continue
        before = res.n_errors + sum(1 for f in res.findings if f.severity == "warn")
        for symbol in _children(root, "symbol"):
            audit_symbol(symbol, res, emit)
            n_sym += 1
        after = res.n_errors + sum(1 for f in res.findings if f.severity == "warn")
        if after == before:
            res.ok(f"{Path(lib).name}: conforms", str(lib))

    n_dev = res.n_errors + sum(1 for f in res.findings if f.severity == "warn")
    res.summary = (f"{n_sym} symbols in {len(libs)} lib(s); {n_dev} style deviation(s)"
                   + ("" if strict else " (advisory — use --strict to fail)"))
    return res


def _discover_libs(cfg) -> list[Path]:
    root = cfg.path.parent if cfg.path else Path.cwd()
    return sorted(root.rglob("*.kicad_sym"))


def run(args) -> int:
    libs = [Path(p) for p in args.libs] if args.libs else None
    if not libs:
        cfg = cfgmod.load_or_exit(args.config)
        libs = _discover_libs(cfg)
        if not libs:
            sys.exit("error: no .kicad_sym libraries found (pass one explicitly)")
    missing = [p for p in libs if not p.exists()]
    if missing:
        sys.exit(f"error: not found: {', '.join(str(p) for p in missing)}")
    return render_and_exit(audit(libs, strict=args.strict))


def add_parser(sub):
    p = sub.add_parser("symbol-style",
                       help="audit .kicad_sym against the house symbol style guide")
    p.add_argument("libs", nargs="*", help="symbol libs (default: all *.kicad_sym in the project)")
    p.add_argument("--strict", action="store_true",
                   help="promote style warnings to failures (exit 1)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
